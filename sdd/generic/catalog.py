"""Generic typed dataset import and reflection. No business-domain names or concepts."""

import json
from contextlib import nullcontext
import re
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import (
    MetaData,
    Table,
    Column,
    String,
    Text,
    BigInteger,
    Numeric,
    Boolean,
    Date,
    DateTime,
    JSON,
    select,
    insert,
    update,
    text,
    ForeignKeyConstraint,
    func,
    literal as bound_literal,
)
from sqlalchemy.schema import AddConstraint
from ..ledger import Ledger, uid, now
from . import schema as s

TYPES = {
    "text": Text,
    "integer": BigInteger,
    "number": lambda: Numeric(38, 10),
    "boolean": Boolean,
    "date": Date,
    "datetime": lambda: DateTime(timezone=True),
    "json": JSON,
}


def serial(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    return value


def infer(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return "text"
    if all(isinstance(v, bool) for v in vals):
        return "boolean"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
        return "integer"
    if all(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in vals):
        return "number"
    if all(isinstance(v, (dict, list)) for v in vals):
        return "json"
    if all(isinstance(v, str) for v in vals):
        # Infer only strict ISO forms; explicit column definitions always override inference.
        if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in vals):
            try:
                for v in vals:
                    date.fromisoformat(v)
                return "date"
            except ValueError:
                pass
        if all(re.match(r"^\d{4}-\d{2}-\d{2}T", v) for v in vals):
            try:
                if all(
                    datetime.fromisoformat(v.replace("Z", "+00:00")).tzinfo is not None
                    for v in vals
                ):
                    return "datetime"
            except ValueError:
                pass
        return "text"
    raise ValueError("Mixed data types require an explicit column type and normalized input values")


def coerce(value, kind):
    if value is None:
        return None
    if kind == "text":
        if not isinstance(value, str):
            raise ValueError("Text values must be strings")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("Boolean values must be true/false")
        return value
    if kind in ("integer", "number"):
        if isinstance(value, bool):
            raise ValueError("Boolean is not a numeric literal")
        n = Decimal(str(value))
        if not n.is_finite():
            raise ValueError("Numbers must be finite")
        if kind == "integer":
            if n != n.to_integral_value() or not -(2**63) <= n < 2**63:
                raise ValueError("Invalid 64-bit integer")
            return int(n)
        return n
    if kind == "date":
        return date.fromisoformat(value) if isinstance(value, str) else value
    if kind == "datetime":
        dt = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if dt.tzinfo is None:
            raise ValueError("Datetime values require an explicit timezone")
        return dt
    if kind == "json":
        json.dumps(value, allow_nan=False)
        return value
    raise ValueError("Unknown column type")


class Catalog:
    def __init__(self, db):
        self.db, self.ledger = db, Ledger(db)

    def list(self, tenant):
        return self.ledger.list(tenant, s.datasets)

    def get(self, tenant, identity):
        matches = [d for d in self.list(tenant) if identity in (d["id"], d["name"])]
        if len(matches) != 1:
            raise ValueError("Unknown or ambiguous dataset in this tenant")
        return matches[0]

    def table(self, dataset, conn):
        return Table(
            dataset["table_name"], MetaData(), schema=dataset["schema_name"], autoload_with=conn
        )

    def create(
        self,
        tenant,
        name,
        rows,
        columns=None,
        primary_key=None,
        description="",
        writable=True,
        *,
        connection=None,
    ):
        if not name.strip() or len(name) > 120 or name.startswith("_sdd"):
            raise ValueError("Invalid dataset name")
        if len(rows) > 10000:
            raise ValueError("Import at most 10,000 rows per request")
        if any(not isinstance(r, dict) for r in rows):
            raise ValueError("Rows must be objects")
        names = list(dict.fromkeys(k for row in rows for k in row))
        if columns:
            definitions = [dict(c) for c in columns]
            names = [c["name"] for c in definitions]
        else:
            definitions = [
                {
                    "name": k,
                    "type": infer([r.get(k) for r in rows]),
                    "nullable": True,
                    "description": "",
                }
                for k in names
            ]
        if not 1 <= len(names) <= 64 or len(set(names)) != len(names):
            raise ValueError("Datasets require 1–64 unique columns")
        for c in definitions:
            if (
                not c["name"]
                or len(c["name"].encode("utf-8")) > 63
                or c["name"].startswith("_sdd")
                or "\x00" in c["name"]
            ):
                raise ValueError("Invalid or reserved column name")
            if c["type"] not in TYPES:
                raise ValueError("Unsupported type")
            c.setdefault("nullable", True)
            c.setdefault("description", "")
        primary_key = primary_key or ["_sdd_row_id"]
        if set(primary_key) - set(names) - {"_sdd_row_id"}:
            raise ValueError("Unknown primary key columns")
        if "_sdd_row_id" in primary_key and primary_key != ["_sdd_row_id"]:
            raise ValueError("Invalid generated primary key")
        if any(set(row) - set(names) for row in rows):
            raise ValueError("Input contains undeclared columns")
        identity = uid()
        physical = "d_" + identity
        pg = self.db.engine.dialect.name == "postgresql"
        schema = "sdd_data" if pg else None
        sqlcols = []
        if primary_key == ["_sdd_row_id"]:
            default = (
                text("gen_random_uuid()::text") if pg else text("(lower(hex(randomblob(16))))")
            )
            sqlcols.append(
                Column("_sdd_row_id", String(64), primary_key=True, server_default=default)
            )
        for c in definitions:
            sqlcols.append(
                Column(
                    c["name"],
                    TYPES[c["type"]](),
                    primary_key=c["name"] in primary_key,
                    nullable=False if c["name"] in primary_key else c["nullable"],
                )
            )
        table = Table(physical, MetaData(), *sqlcols, schema=schema)
        normalized = [
            {c["name"]: coerce(r.get(c["name"]), c["type"]) for c in definitions} for r in rows
        ]
        record = dict(
            id=identity,
            tenant=tenant,
            name=name,
            description=description,
            schema_name=schema,
            table_name=physical,
            columns=definitions,
            primary_key=primary_key,
            writable=int(writable),
            links=[],
            created_at=now(),
        )
        with (
            nullcontext(connection) if connection is not None else self.db.transaction(tenant)
        ) as cx:
            if self.db.engine.dialect.name == "postgresql":
                cx.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                    {"key": tenant + ":datasets"},
                )
            if any(
                existing.casefold() == name.casefold()
                for existing in cx.execute(
                    select(s.datasets.c.name).where(s.datasets.c.tenant == tenant)
                ).scalars()
            ):
                raise ValueError("Dataset names must be unique ignoring letter case")
            if cx.execute(
                select(s.datasets.c.id).where(
                    s.datasets.c.tenant == tenant, s.datasets.c.name == name
                )
            ).first():
                raise ValueError("A dataset with this name already exists")
            table.create(cx)
            if pg:
                quoted = cx.dialect.identifier_preparer.format_table(table)
                cx.execute(text(f"ALTER TABLE {quoted} ENABLE ROW LEVEL SECURITY"))
                cx.execute(text(f"ALTER TABLE {quoted} FORCE ROW LEVEL SECURITY"))
                owner = str(tenant).replace("'", "''")
                cx.execute(
                    text(
                        f"CREATE POLICY tenant_access ON {quoted} USING (current_setting('sdd.tenant',true) = '{owner}') WITH CHECK (current_setting('sdd.tenant',true) = '{owner}')"
                    )
                )
            if normalized:
                cx.execute(insert(table), normalized)
            cx.execute(insert(s.datasets).values(**record))
        return record

    def rows(self, tenant, dataset, conn=None, limit=None):
        if conn is not None:
            table = self.table(dataset, conn)
            query = select(table).order_by(*(table.c[c] for c in dataset["primary_key"]))
            if limit is not None:
                query = query.limit(limit)
            return [dict(r) for r in conn.execute(query).mappings()]
        with self.db.transaction(tenant) as cx:
            return self.rows(tenant, dataset, cx, limit=limit)

    def link(self, tenant, source, target, source_column, target_column):
        left, right = self.get(tenant, source), self.get(tenant, target)
        if right["primary_key"] != [target_column]:
            raise ValueError("Relationship target must be a single-column primary key")
        if source_column not in {c["name"] for c in left["columns"]}:
            raise ValueError("Unknown source column")
        link = dict(
            target_id=right["id"],
            source_column=source_column,
            target_column=target_column,
            cardinality="many_to_one",
        )
        if link in left["links"]:
            return left
        with self.db.transaction(tenant) as cx:
            lt, rt = self.table(left, cx), self.table(right, cx)
            missing = cx.execute(
                select(lt.c[source_column])
                .where(
                    lt.c[source_column].is_not(None),
                    ~lt.c[source_column].in_(select(rt.c[target_column])),
                )
                .limit(1)
            ).first()
            if missing:
                raise ValueError("Existing rows violate the declared relationship")
            if self.db.engine.dialect.name == "postgresql":
                # The constraint references a Table bound into the same MetaData.
                rt = Table(
                    right["table_name"], lt.metadata, schema=right["schema_name"], autoload_with=cx
                )
                constraint = ForeignKeyConstraint(
                    [lt.c[source_column]], [rt.c[target_column]], name="fk_" + uid()[:20]
                )
                lt.append_constraint(constraint)
                cx.execute(AddConstraint(constraint))
            cx.execute(
                update(s.datasets)
                .where(s.datasets.c.id == left["id"], s.datasets.c.tenant == tenant)
                .values(links=[*left["links"], link])
            )
        return self.get(tenant, left["id"])

    def model_catalog(
        self,
        tenant,
        selected=None,
        include_values=True,
        include_features=True,
        question="",
        max_datasets=20,
    ):
        datasets = self.list(tenant)
        if selected:
            wanted = {self.get(tenant, x)["id"] for x in selected}
            datasets = [d for d in datasets if d["id"] in wanted]
        if not datasets:
            raise ValueError("Import a dataset before querying")
        if max_datasets is not None and len(datasets) > max_datasets:
            raise ValueError("Select at most 20 datasets for one planning request")
        result = []
        with self.db.transaction(tenant) as cx:
            if self.db.engine.dialect.name == "postgresql":
                cx.execute(text("SET LOCAL statement_timeout = '3000ms'"))
            for d in datasets:
                columns = []
                table = self.table(d, cx) if include_values else None
                for c in d["columns"]:
                    info = dict(c)
                    if include_values and c["type"] in ("text", "boolean", "date"):
                        info["values"] = [
                            serial(v)
                            for v in cx.execute(
                                select(table.c[c["name"]])
                                .distinct()
                                .order_by(table.c[c["name"]])
                                .limit(12)
                            ).scalars()
                            if v is not None and len(str(v)) <= 80
                        ]
                    if include_values and question and c["type"] == "text":
                        source = table.c[c["name"]]
                        locate = (
                            func.strpos
                            if self.db.engine.dialect.name == "postgresql"
                            else func.instr
                        )
                        matches = (
                            cx.execute(
                                select(source)
                                .where(
                                    func.length(source).between(2, 80),
                                    locate(func.lower(bound_literal(question)), func.lower(source))
                                    > 0,
                                )
                                .distinct()
                                .order_by(source)
                                .limit(12)
                            )
                            .scalars()
                            .all()
                        )
                        info["values"] = list(dict.fromkeys([*matches, *info.get("values", [])]))[
                            :20
                        ]
                    columns.append(info)
                result.append({**d, "columns": columns})
        if include_features:
            from .features import FeatureRegistry

            registry = FeatureRegistry(self.db)
            for dataset in result:
                for feature in registry.list(tenant, dataset["id"], active=True):
                    definition = feature["definition"]
                    dataset["columns"].append(
                        {
                            "name": feature["name"],
                            "type": {
                                "noul": "boolean",
                                "score": "number",
                                "choice": "text",
                                "extract": "text",
                            }[definition["kind"]],
                            "description": definition["text"],
                            "aliases": definition["aliases"],
                            "feature_id": feature["id"],
                            "source_column": definition["column"],
                            "semantic_kind": definition["kind"],
                            "nullable": True,
                            "values": list(definition["criteria"])
                            if definition["kind"] == "choice"
                            else [],
                        }
                    )
        return result
