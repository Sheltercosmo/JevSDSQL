"""Persist extraction previews and append their verified values atomically."""

import time

from sqlalchemy import insert, select, update

from ..ledger import digest, now, uid
from ..operators.service import OperatorService
from .catalog import Catalog, coerce, serial
from .extraction_schema import columns_from
from . import schema


def definition_signature(dataset):
    return digest(
        {
            key: dataset[key]
            for key in ("id", "columns", "primary_key", "writable", "schema_name", "table_name")
        }
    )


class TextImporter:
    def __init__(self, db, decisions):
        self.db, self.decisions, self.catalog = db, decisions, Catalog(db)

    def preview(
        self,
        tenant,
        actor,
        *,
        text,
        row_description,
        columns,
        dataset_id=None,
        name=None,
        primary_key=None,
        record_mode="auto",
        max_rows=200,
        limits=None,
    ):
        if bool(dataset_id) == bool(name):
            raise ValueError("Choose an existing dataset or a name for a new dataset")
        fields = columns_from(columns)
        target = self.catalog.get(tenant, dataset_id) if dataset_id else None
        if target:
            if not target["writable"]:
                raise ValueError("This dataset is read-only")
            definitions = {c["name"]: c for c in target["columns"]}
            for field in fields:
                if field.name not in definitions or definitions[field.name]["type"] != field.type:
                    raise ValueError("Extraction fields must match existing column names and types")
                if field.on_missing == "null" and not definitions[field.name]["nullable"]:
                    raise ValueError("A required destination column cannot receive missing values")
            required = {c["name"] for c in target["columns"] if not c["nullable"]} | set(
                target["primary_key"]
            )
            required.discard("_sdd_row_id")
            if required - {c.name for c in fields}:
                raise ValueError("Describe every required destination column")
            if primary_key is not None:
                raise ValueError("Primary keys belong to the existing dataset")
        else:
            if (
                not isinstance(name, str)
                or not name.strip()
                or len(name) > 120
                or name.startswith("_sdd")
            ):
                raise ValueError("Invalid dataset name")
            if any(d["name"].casefold() == name.casefold() for d in self.catalog.list(tenant)):
                raise ValueError("This dataset already exists; select it to append records")
            if primary_key and (
                len(set(primary_key)) != len(primary_key)
                or set(primary_key) - {c.name for c in fields}
            ):
                raise ValueError("Primary key columns must belong to the extraction schema")
        service = OperatorService(self.db, self.decisions, tenant, actor, "reviewer")
        extraction = service.call(
            "EXTRACT_TABLE",
            {
                "text": text,
                "columns": [c.model_dump() for c in fields],
                "row_description": row_description,
                "record_mode": record_mode,
                "max_rows": max_rows,
            },
            limits={"max_stages": 4, **(limits or {})},
        )
        table = extraction.get("value") or extraction.get("partial_value") or {"rows": []}
        rows = [r["values"] for r in table["rows"]]
        ready = extraction["output_state"] == "VALUE" and bool(rows)
        blockers = [] if ready else ["No complete import: inspect record scope and cell states"]
        keys = target["primary_key"] if target else primary_key
        if keys and keys != ["_sdd_row_id"] and ready:
            values = [tuple(row.get(key) for key in keys) for row in rows]
            if any(None in value for value in values) or len(set(values)) != len(values):
                ready = False
                blockers.append("Extracted primary keys are missing or duplicated")
        token = uid()
        options = {
            "kind": "text_import",
            "extraction_run_id": extraction["run_id"],
            "target_id": target["id"] if target else None,
            "target_signature": definition_signature(target) if target else None,
            "name": target["name"] if target else name,
            "columns": [c.catalog_definition() for c in fields],
            "primary_key": primary_key,
            "description": row_description,
            "rows": rows,
            "source_sha256": extraction["source_sha256"],
            "ready": ready,
        }
        with self.db.transaction(tenant) as connection:
            connection.execute(
                insert(schema.previews).values(
                    id=token,
                    tenant=tenant,
                    logical_sql="",
                    dataset_ids=[target["id"]] if target else [],
                    snapshot_hash=digest(options),
                    affected_rows=len(rows),
                    options=serial(options),
                    expires_at=time.time() + 3600,
                    state="READY" if ready else "HOLD",
                    actor=actor,
                    created_at=now(),
                )
            )
        return {
            "extraction": extraction,
            "preview_token": token,
            "can_import": ready,
            "rows": len(rows),
            "dataset_name": options["name"],
            "mode": "append" if target else "create",
            "blockers": blockers,
            "committed": False,
        }

    def commit(self, tenant, actor, token):
        with self.db.transaction(tenant) as connection:
            record = (
                connection.execute(
                    select(schema.previews)
                    .where(schema.previews.c.tenant == tenant, schema.previews.c.id == token)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if (
                not record
                or record["actor"] != actor
                or record["options"].get("kind") != "text_import"
            ):
                raise ValueError("Text import preview is unavailable to this actor")
            options = record["options"]
            if record["state"] == "COMMITTED":
                return {**options["committed_result"], "replayed": True}
            if (
                record["expires_at"] < time.time()
                or record["state"] != "READY"
                or not options["ready"]
            ):
                raise ValueError("Import requires a complete, unexpired extraction preview")
            claimed = connection.execute(
                update(schema.previews)
                .where(
                    schema.previews.c.id == token,
                    schema.previews.c.tenant == tenant,
                    schema.previews.c.state == "READY",
                )
                .values(state="COMMITTING")
            )
            if claimed.rowcount != 1:
                raise ValueError("This preview is already being imported")
            if options["target_id"]:
                row = (
                    connection.execute(
                        select(schema.datasets).where(
                            schema.datasets.c.tenant == tenant,
                            schema.datasets.c.id == options["target_id"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if not row or definition_signature(row) != options["target_signature"]:
                    raise ValueError("Destination schema changed; extract again")
                target = dict(row)
            else:
                target = self.catalog.create(
                    tenant,
                    options["name"],
                    [],
                    columns=options["columns"],
                    primary_key=options["primary_key"],
                    description=options["description"],
                    connection=connection,
                )
            table = self.catalog.table(target, connection)
            definitions = {c["name"]: c for c in target["columns"]}
            for row in options["rows"]:
                values = {
                    name: coerce(value, definitions[name]["type"]) for name, value in row.items()
                }
                written = (
                    connection.execute(
                        insert(table)
                        .values(**values)
                        .returning(*(table.c[name] for name in values))
                    )
                    .mappings()
                    .one()
                )
                if any(written[name] != value for name, value in values.items()):
                    raise ValueError(
                        "Destination would change an extracted value's precision; use text storage or PostgreSQL"
                    )
            result = {
                "committed": True,
                "dataset_id": target["id"],
                "dataset_name": target["name"],
                "inserted_rows": len(options["rows"]),
                "extraction_run_id": options["extraction_run_id"],
                "replayed": False,
            }
            connection.execute(
                update(schema.previews)
                .where(schema.previews.c.tenant == tenant, schema.previews.c.id == token)
                .values(state="COMMITTED", options={**options, "committed_result": result})
            )
        return result
