"""Dataset-independent semantic feature revisions, reviews, and maintenance."""

import time
from sqlalchemy import select, insert, update

from ..ledger import uid, now, digest
from . import schema
from .catalog import Catalog, serial
from .semantic_types import SemanticSpec, UNKNOWN_OPTION


class FeatureRegistry:
    def __init__(self, db):
        self.db, self.catalog = db, Catalog(db)

    def list(self, tenant, dataset_id=None, active=False):
        query = select(schema.features).where(schema.features.c.tenant == tenant)
        if dataset_id:
            dataset = self.catalog.get(tenant, dataset_id)
            query = query.where(schema.features.c.dataset_id == dataset["id"])
        if active:
            query = query.where(schema.features.c.status == "active")
        with self.db.transaction(tenant) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    query.order_by(schema.features.c.name, schema.features.c.revision.desc())
                ).mappings()
            ]

    def get(self, tenant, identity):
        return self.catalog.ledger.get(tenant, schema.features, identity)

    def spec(self, feature):
        definition = feature["definition"]
        return SemanticSpec(
            column=definition["column"],
            definition=definition["text"],
            kind=definition["kind"],
            criteria=definition["criteria"],
            context_columns=None
            if definition["context_columns"] is None
            else tuple(definition["context_columns"]),
            feature_id=feature["id"],
            confidence=definition["confidence"],
            aliases=tuple(definition["aliases"]),
        )

    def resolve(self, tenant, dataset_id, reference):
        matches = [
            feature
            for feature in self.list(tenant, dataset_id, active=True)
            if reference.casefold()
            in {
                value.casefold()
                for value in (feature["id"], feature["name"], *feature["definition"]["aliases"])
            }
        ]
        if len(matches) != 1:
            raise ValueError("Unknown, inactive or ambiguous reviewed semantic feature")
        return matches[0]

    def create(
        self,
        tenant,
        owner,
        dataset_id,
        name,
        column,
        definition,
        kind="noul",
        criteria=None,
        context_columns=(),
        aliases=(),
        confidence=0.55,
        maintain=False,
    ):
        dataset = self.catalog.get(tenant, dataset_id)
        columns = {item["name"]: item for item in dataset["columns"]}
        if (
            not name.strip()
            or len(name) > 120
            or "\x00" in name
            or name.casefold() in {key.casefold() for key in columns}
        ):
            raise ValueError("Feature requires a distinct, nonempty name")
        if column not in columns or columns[column]["type"] != "text":
            raise ValueError("Semantic features require a text source column")
        if not definition.strip() or len(definition) > 4000 or not owner.strip():
            raise ValueError("A definition and owner are required")
        if kind not in ("noul", "choice", "score", "extract") or not 0 <= confidence <= 1:
            raise ValueError("Invalid semantic output type or confidence threshold")
        if context_columns is not None and (
            set(context_columns) - set(columns) or len(set(context_columns)) != len(context_columns)
        ):
            raise ValueError("Context columns must name distinct source fields")
        if len(aliases) > 20 or any(not value.strip() or len(value) > 120 for value in aliases):
            raise ValueError("Use at most 20 nonempty aliases")
        if {value.casefold() for value in aliases} & {value.casefold() for value in columns}:
            raise ValueError("Feature aliases must not shadow physical columns")
        if len({value.casefold() for value in aliases}) != len(aliases):
            raise ValueError("Feature aliases must be distinct")
        if kind == "choice":
            if (
                not isinstance(criteria, dict)
                or not 1 <= len(criteria) <= 120
                or UNKNOWN_OPTION in criteria
            ):
                raise ValueError("Choice requires 1–120 named categories")
            if any(
                not isinstance(key, str) or not key or not isinstance(value, str) or not value
                for key, value in criteria.items()
            ):
                raise ValueError("Choice category keys and descriptions must be strings")
        elif kind == "score":
            if (
                not isinstance(criteria, list)
                or not 2 <= len(criteria) <= 10
                or any(not isinstance(value, str) or not value for value in criteria)
            ):
                raise ValueError("Score requires 2–10 ordered level descriptions")
        elif criteria is not None:
            raise ValueError("Noul and extraction do not take category criteria")
        payload = {
            "column": column,
            "text": definition,
            "kind": kind,
            "criteria": criteria,
            "context_columns": None if context_columns is None else list(context_columns),
            "aliases": list(aliases),
            "confidence": confidence,
        }
        with self.db.transaction(tenant) as connection:
            connection.execute(
                select(schema.datasets.c.id)
                .where(schema.datasets.c.id == dataset["id"], schema.datasets.c.tenant == tenant)
                .with_for_update()
            ).first()
            existing = (
                connection.execute(
                    select(schema.features).where(
                        schema.features.c.tenant == tenant,
                        schema.features.c.dataset_id == dataset["id"],
                    )
                )
                .mappings()
                .all()
            )
            previous = [item for item in existing if item["name"].casefold() == name.casefold()]
            canonical_name = previous[0]["name"] if previous else name
            revision = max((item["revision"] for item in previous), default=0) + 1
            feature = dict(
                id=uid(),
                tenant=tenant,
                dataset_id=dataset["id"],
                name=canonical_name,
                revision=revision,
                definition=payload,
                status="candidate",
                owner=owner,
                review=[],
                maintain=int(maintain),
                materialization={},
                created_at=now(),
            )
            connection.execute(insert(schema.features).values(**feature))
        return feature

    def review(self, tenant, identity, actor, status, reason, examples=None):
        if status not in ("active", "deprecated") or not actor.strip() or not reason.strip():
            raise ValueError("Review requires an actor, reason and active/deprecated status")
        feature = self.get(tenant, identity)
        if status == "active" and (
            not examples or any(not item.get("text") or "expected" not in item for item in examples)
        ):
            raise ValueError("Activation requires reviewed examples with expected values")
        with self.db.transaction(tenant) as connection:
            connection.execute(
                select(schema.datasets.c.id)
                .where(
                    schema.datasets.c.id == feature["dataset_id"],
                    schema.datasets.c.tenant == tenant,
                )
                .with_for_update()
            ).first()
            current = (
                connection.execute(
                    select(schema.features)
                    .where(schema.features.c.id == identity, schema.features.c.tenant == tenant)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if status == "active":
                names = {
                    name.casefold() for name in (feature["name"], *feature["definition"]["aliases"])
                }
                for other in connection.execute(
                    select(schema.features).where(
                        schema.features.c.tenant == tenant,
                        schema.features.c.dataset_id == feature["dataset_id"],
                        schema.features.c.status == "active",
                    )
                ).mappings():
                    if other["name"] == feature["name"]:
                        connection.execute(
                            update(schema.features)
                            .where(
                                schema.features.c.id == other["id"],
                                schema.features.c.tenant == tenant,
                            )
                            .values(status="deprecated")
                        )
                    elif names & {
                        name.casefold() for name in (other["name"], *other["definition"]["aliases"])
                    }:
                        raise ValueError("Feature aliases overlap another active definition")
            history = [
                *current["review"],
                {
                    "actor": actor,
                    "status": status,
                    "reason": reason,
                    "examples": examples or [],
                    "at": now(),
                },
            ]
            connection.execute(
                update(schema.features)
                .where(schema.features.c.id == identity, schema.features.c.tenant == tenant)
                .values(status=status, review=history)
            )
            if status == "active":
                self.enqueue(tenant, feature["dataset_id"], connection)
        return self.get(tenant, identity)

    def assert_value(self, tenant, identity, primary_key, value, actor, reason):
        feature = self.get(tenant, identity)
        dataset = self.catalog.get(tenant, feature["dataset_id"])
        spec = self.spec(feature)
        if set(primary_key) != set(dataset["primary_key"]) or not reason.strip():
            raise ValueError("A complete primary key and review reason are required")
        if value is not None:
            if spec.kind == "noul" and not isinstance(value, bool):
                raise ValueError("Expected a Boolean review value")
            if spec.kind == "choice" and (not isinstance(value, str) or value not in spec.criteria):
                raise ValueError("Review must select a declared category")
            if spec.kind == "score" and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= len(spec.criteria) - 1
            ):
                raise ValueError("Review score is outside the rubric")
        with self.db.transaction(tenant) as connection:
            table = self.catalog.table(dataset, connection)
            row = (
                connection.execute(
                    select(table)
                    .where(*(table.c[key] == val for key, val in primary_key.items()))
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("Source row does not exist")
            row = dict(row)
            if (
                spec.kind == "extract"
                and value is not None
                and (
                    not isinstance(value, str)
                    or not value
                    or not isinstance(row[spec.column], str)
                    or value not in row[spec.column]
                )
            ):
                raise ValueError("Reviewed extraction must be copied from the source text")
            row_key = digest(serial([row[key] for key in dataset["primary_key"]]))
            version_id = digest([tenant, dataset["id"], row_key, digest(serial(row))])
            dialect_insert = __import__(
                "sqlalchemy.dialects." + self.db.engine.dialect.name, fromlist=["insert"]
            ).insert
            connection.execute(
                dialect_insert(schema.row_versions)
                .values(
                    id=version_id,
                    tenant=tenant,
                    dataset_id=dataset["id"],
                    row_key=row_key,
                    row_hash=digest(serial(row)),
                    value=serial(row),
                    created_at=now(),
                )
                .on_conflict_do_nothing()
            )
            record = dict(
                id=uid(),
                tenant=tenant,
                feature_id=identity,
                row_key=row_key,
                dependency_hash=digest(spec.context(row)),
                source_version_id=version_id,
                value=value,
                actor=actor,
                reason=reason,
                created_at=now(),
            )
            connection.execute(insert(schema.feature_reviews).values(**record))
        return record

    def preview(self, tenant, identity, decisions, max_evaluations=100):
        from .semantics import Semantics

        feature = self.get(tenant, identity)
        dataset = self.catalog.get(tenant, feature["dataset_id"])
        rows = self.catalog.rows(tenant, dataset, limit=50001)
        if len(rows) > 50000:
            raise ValueError("Feature preview supports at most 50,000 rows")
        spec = self.spec(feature)
        values, details, metrics = Semantics(self.db, decisions).ensure_many(
            tenant, dataset, rows, [spec], [max_evaluations]
        )
        if digest(serial(rows)) != digest(serial(self.catalog.rows(tenant, dataset, limit=50001))):
            raise ValueError("Source changed during feature preview")
        return {
            "feature": feature,
            "coverage": metrics,
            "evidence": details[spec.key],
            "result": [
                {
                    **{key: row[key] for key in dataset["primary_key"]},
                    "value": values[spec.key][
                        digest(serial([row[key] for key in dataset["primary_key"]]))
                    ],
                }
                for row in rows[:1000]
            ],
        }

    def refresh(
        self,
        tenant,
        dataset_id,
        decisions,
        max_evaluations=1000,
        *,
        maintained_only=False,
        publish=True,
        progress=None,
    ):
        from sqlglot import exp
        from .sql import SQLService, col, literal

        dataset = self.catalog.get(tenant, dataset_id)
        features = self.list(tenant, dataset_id, active=True)
        if maintained_only:
            features = [feature for feature in features if feature["maintain"]]
        if not features:
            return {"manifest": {"complete": True}, "run_id": None}
        columns = [col(key) for key in dataset["primary_key"]]
        columns += [
            exp.alias_(
                exp.Anonymous(
                    this="SEMANTIC_FEATURE",
                    expressions=[col(feature["definition"]["column"]), literal(feature["id"])],
                ),
                feature["name"],
                quoted=True,
            )
            for feature in features
        ]
        query = exp.select(*columns).from_(
            exp.Table(this=exp.to_identifier(dataset["name"], quoted=True))
        )
        result = SQLService(self.db, decisions).execute(
            tenant,
            query.sql(dialect="postgres"),
            max_evaluations=max_evaluations,
            progress=progress,
        )
        if publish and result["manifest"]["complete"]:
            with self.db.transaction(tenant) as connection:
                connection.execute(
                    update(schema.features)
                    .where(
                        schema.features.c.tenant == tenant,
                        schema.features.c.id.in_([feature["id"] for feature in features]),
                        schema.features.c.status == "active",
                    )
                    .values(
                        materialization={
                            "run_id": result["run_id"],
                            "source_snapshot": result["manifest"]["source_snapshot"],
                            "refreshed_at": now(),
                        }
                    )
                )
        return result

    def enqueue(self, tenant, dataset_id, connection):
        maintained = connection.execute(
            select(schema.features.c.id).where(
                schema.features.c.tenant == tenant,
                schema.features.c.dataset_id == dataset_id,
                schema.features.c.status == "active",
                schema.features.c.maintain == 1,
            )
        ).first()
        if not maintained:
            return None
        pending = connection.execute(
            select(schema.maintenance_jobs.c.id).where(
                schema.maintenance_jobs.c.tenant == tenant,
                schema.maintenance_jobs.c.dataset_id == dataset_id,
                schema.maintenance_jobs.c.state == "pending",
            )
        ).scalar()
        if pending:
            return pending
        identity = uid()
        connection.execute(
            insert(schema.maintenance_jobs).values(
                id=identity,
                tenant=tenant,
                dataset_id=dataset_id,
                state="pending",
                lease_until=0,
                attempts=0,
                available_at=0,
                created_at=now(),
            )
        )
        return identity

    def work_one(self, tenant, decisions, stop_event=None):
        with self.db.transaction(tenant) as connection:
            job = (
                connection.execute(
                    select(schema.maintenance_jobs)
                    .where(
                        schema.maintenance_jobs.c.tenant == tenant,
                        schema.maintenance_jobs.c.state.in_(["pending", "running"]),
                        schema.maintenance_jobs.c.lease_until < time.time(),
                        schema.maintenance_jobs.c.available_at <= time.time(),
                    )
                    .order_by(schema.maintenance_jobs.c.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if not job:
                return False
            if job["attempts"] >= 3:
                connection.execute(
                    update(schema.maintenance_jobs)
                    .where(
                        schema.maintenance_jobs.c.id == job["id"],
                        schema.maintenance_jobs.c.tenant == tenant,
                    )
                    .values(state="failed", error="RetryLimitReached")
                )
                return True
            token = uid()
            claimed = connection.execute(
                update(schema.maintenance_jobs)
                .where(
                    schema.maintenance_jobs.c.id == job["id"],
                    schema.maintenance_jobs.c.tenant == tenant,
                    schema.maintenance_jobs.c.lease_until == job["lease_until"],
                    schema.maintenance_jobs.c.attempts == job["attempts"],
                    schema.maintenance_jobs.c.state == job["state"],
                )
                .values(
                    state="running",
                    lease_token=token,
                    lease_until=time.time() + 120,
                    attempts=job["attempts"] + 1,
                )
            )
            if not claimed.rowcount:
                return True
        error, result = None, None
        last_check = [0.0]

        def progress():
            if stop_event is not None and stop_event.is_set():
                return False
            if time.monotonic() - last_check[0] < 1:
                return True
            last_check[0] = time.monotonic()
            with self.db.transaction(tenant) as connection:
                changed = connection.execute(
                    update(schema.maintenance_jobs)
                    .where(
                        schema.maintenance_jobs.c.id == job["id"],
                        schema.maintenance_jobs.c.tenant == tenant,
                        schema.maintenance_jobs.c.lease_token == token,
                        schema.maintenance_jobs.c.state == "running",
                    )
                    .values(lease_until=time.time() + 120)
                )
                return bool(changed.rowcount)

        try:
            result = self.refresh(
                tenant,
                job["dataset_id"],
                decisions,
                maintained_only=True,
                publish=False,
                progress=progress,
            )
            if not result["manifest"]["complete"]:
                error = "UnresolvedFeatureValues"
        except Exception as exc:
            error = getattr(exc, "code", type(exc).__name__)
        with self.db.transaction(tenant) as connection:
            changed = connection.execute(
                update(schema.maintenance_jobs)
                .where(
                    schema.maintenance_jobs.c.id == job["id"],
                    schema.maintenance_jobs.c.tenant == tenant,
                    schema.maintenance_jobs.c.lease_token == token,
                    schema.maintenance_jobs.c.state == "running",
                )
                .values(
                    state=("failed" if job["attempts"] >= 2 else "pending")
                    if error
                    else "succeeded",
                    lease_until=0,
                    available_at=time.time() + 2 ** (job["attempts"] + 1),
                    error=error,
                    run_id=result.get("run_id") if result else None,
                )
            )
            if changed.rowcount and not error and result.get("run_id"):
                feature_ids = [
                    item["feature_id"]
                    for item in result["manifest"].get("evidence", [])
                    if item.get("feature_id")
                ]
                connection.execute(
                    update(schema.features)
                    .where(
                        schema.features.c.tenant == tenant,
                        schema.features.c.id.in_(feature_ids),
                        schema.features.c.status == "active",
                    )
                    .values(
                        materialization={
                            "run_id": result["run_id"],
                            "source_snapshot": result["manifest"]["source_snapshot"],
                            "refreshed_at": now(),
                        }
                    )
                )
        return True
