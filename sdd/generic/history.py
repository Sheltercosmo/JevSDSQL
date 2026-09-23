"""Durable query drafts and outcomes; reopening never executes saved SQL."""

from sqlalchemy import and_, insert, or_, select, update

from ..ledger import digest, now, uid
from . import schema
from .catalog import Catalog
from .planning_review import PlanReviewRequired, public_plan


class QueryHistory:
    def __init__(self, db):
        self.db, self.catalog = db, Catalog(db)

    def import_owned_runs(self, tenant, actor):
        """Import attributable pre-history plans; anonymous legacy SQL stays unassigned."""
        with self.db.transaction(tenant) as connection:
            runs = [
                dict(r)
                for r in connection.execute(
                    select(schema.runs).where(schema.runs.c.tenant == tenant)
                ).mappings()
            ]
            saved = (
                connection.execute(
                    select(schema.query_history.c.id, schema.query_history.c.review_id).where(
                        schema.query_history.c.tenant == tenant
                    )
                )
                .mappings()
                .all()
            )
            existing = {r["id"] for r in saved}
            existing_reviews = {r["review_id"] for r in saved}
            reviews = {
                r["id"]: r
                for r in runs
                if r["plan"].get("_actor") == actor
                and r["manifest"].get("operation") == "planning_review"
            }
            completed = {}
            for run in sorted(runs, key=lambda r: r["created_at"]):
                review_id = run["plan"].get("review_id") or run["plan"].get(
                    "human_confirmation", {}
                ).get("review_id")
                if review_id in reviews and run["manifest"].get("operation") != "planning_review":
                    completed[review_id] = run
            added = 0
            for review_id, source in reviews.items():
                identity = digest(["query-history", tenant, actor, review_id])
                if identity in existing or review_id in existing_reviews:
                    continue
                run = completed.get(review_id)
                plan = source["plan"]
                dataset_ids = plan.get("_selected_dataset_ids", plan["dataset_ids"])
                status = (
                    (
                        "committed"
                        if run["manifest"].get("committed")
                        else "complete"
                        if run["manifest"].get("complete")
                        else "partial"
                    )
                    if run
                    else "review"
                    if plan["review"]["reason"]
                    else "plan"
                )
                connection.execute(
                    insert(schema.query_history).values(
                        id=identity,
                        tenant=tenant,
                        actor=actor,
                        input={
                            "mode": "natural",
                            "text": source["request"],
                            "planner_mode": "hybrid" if plan.get("_planner_strategy") == "hybrid" else "jev",
                            "dataset_ids": dataset_ids,
                            "max_evaluations": 100,
                        },
                        dataset_ids=dataset_ids,
                        status=status,
                        logical_sql=(run or source)["logical_sql"],
                        review_id=review_id,
                        run_id=run["id"] if run else None,
                        created_at=source["created_at"],
                        updated_at=(run or source)["created_at"],
                    )
                )
                added += 1
        return added

    def get(self, tenant, actor, identity):
        with self.db.transaction(tenant) as connection:
            record = (
                connection.execute(
                    select(schema.query_history).where(
                        schema.query_history.c.tenant == tenant,
                        schema.query_history.c.actor == actor,
                        schema.query_history.c.id == identity,
                    )
                )
                .mappings()
                .first()
            )
        if record is None:
            raise ValueError("Query history is unavailable to this actor")
        return dict(record)

    def recent(self, tenant, actor, limit=20, before=None):
        table = schema.query_history
        query = select(table).where(table.c.tenant == tenant, table.c.actor == actor)
        if before:
            cursor = self.get(tenant, actor, before)
            query = query.where(
                or_(
                    table.c.created_at < cursor["created_at"],
                    and_(table.c.created_at == cursor["created_at"], table.c.id < cursor["id"]),
                )
            )
        with self.db.transaction(tenant) as connection:
            records = (
                connection.execute(
                    query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(limit + 1)
                )
                .mappings()
                .all()
            )
        items = [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "status": r["status"],
                "mode": r["input"].get("mode", "natural"),
                "title": r["input"].get("text", "")[:180],
                "parent_id": r["parent_id"],
            }
            for r in records[:limit]
        ]
        return {"items": items, "next_cursor": items[-1]["id"] if len(records) > limit else None}

    def detail(self, tenant, actor, identity):
        record = self.get(tenant, actor, identity)
        if record["status"] == "redacted":
            raise ValueError("Source data was deleted; this history entry was redacted")
        output = {"logical_sql": record["logical_sql"], "executed": False}
        if record["run_id"]:
            run = self.catalog.ledger.get(tenant, schema.runs, record["run_id"])
            output.update(
                result=run["result"], manifest=run["manifest"], plan=public_plan(run["plan"])
            )
        return {
            "id": identity,
            "input": record["input"],
            "dataset_ids": record["dataset_ids"],
            "status": record["status"],
            "created_at": record["created_at"],
            "parent_id": record["parent_id"],
            "output": output,
            "has_decisions": bool(record["review_id"]),
            "error": record["error"],
        }

    def capture(self, tenant, actor, draft, operation, *, parent_id=None, continue_review=None):
        parent = self.get(tenant, actor, parent_id) if parent_id else None
        if parent and parent["status"] == "redacted":
            raise ValueError("Source data was deleted; this history entry was redacted")
        identity = uid()
        # Confirming the immediately preceding unexecuted plan completes that entry.
        continuing = (
            parent
            and continue_review
            and parent["review_id"] == continue_review
            and parent["status"] in ("plan", "review")
        )
        if continuing:
            identity = parent_id
        else:
            catalog = self.catalog.list(tenant)
            selected = draft.get("dataset_ids")
            dataset_ids = [
                d["id"]
                for d in catalog
                if selected is None or d["id"] in selected or d["name"] in selected
            ]
            if selected:
                known = {key for d in catalog for key in (d["id"], d["name"])}
                dataset_ids.extend(key for key in selected if key not in known)
            with self.db.transaction(tenant) as connection:
                connection.execute(
                    insert(schema.query_history).values(
                        id=identity,
                        tenant=tenant,
                        actor=actor,
                        input=draft,
                        dataset_ids=dataset_ids,
                        parent_id=parent_id,
                        status="running",
                        logical_sql=draft["text"] if draft["mode"] == "sql" else "",
                        created_at=now(),
                        updated_at=now(),
                    )
                )
        try:
            result = operation()
        except PlanReviewRequired as exc:
            self.finish(tenant, actor, identity, {"plan": exc.plan}, "review")
            exc.history_id = identity
            raise
        except Exception as exc:
            if getattr(exc, "query_plan", None):
                self.finish(tenant, actor, identity, {"plan": exc.query_plan}, "error")
            # Error classes are useful in history; DB exception text may contain source values.
            self.update(tenant, actor, identity, status="error", error=type(exc).__name__)
            raise
        status = (
            "preview"
            if result.get("mutation_preview")
            else "committed"
            if result.get("manifest", {}).get("committed")
            else "complete"
            if result.get("manifest", {}).get("complete")
            else "partial"
            if result.get("manifest")
            else "plan"
        )
        self.finish(tenant, actor, identity, result, status)
        result["history_id"] = identity
        return result

    def update(self, tenant, actor, identity, **values):
        with self.db.transaction(tenant) as connection:
            connection.execute(
                update(schema.query_history)
                .where(
                    schema.query_history.c.tenant == tenant,
                    schema.query_history.c.actor == actor,
                    schema.query_history.c.id == identity,
                    schema.query_history.c.status != "redacted",
                )
                .values(**values, updated_at=now())
            )

    def finish(self, tenant, actor, identity, result, status):
        plan = result.get("plan", {})
        self.update(
            tenant,
            actor,
            identity,
            status=status,
            logical_sql=result.get("logical_sql") or plan.get("logical_sql", ""),
            run_id=result.get("run_id"),
            review_id=plan.get("review_id"),
            preview_id=result.get("preview_token"),
            error=None,
        )

    def committed(self, tenant, actor, token, result):
        with self.db.transaction(tenant) as connection:
            records = (
                connection.execute(
                    select(schema.query_history).where(
                        schema.query_history.c.tenant == tenant,
                        schema.query_history.c.actor == actor,
                        schema.query_history.c.preview_id == token,
                        schema.query_history.c.status == "preview",
                    )
                )
                .mappings()
                .all()
            )
        for record in records:
            self.update(tenant, actor, record["id"], status="committed", run_id=result["run_id"])
            result["history_id"] = record["id"]
        return result

    @staticmethod
    def redact_dataset(connection, tenant, dataset_id):
        records = connection.execute(
            select(schema.query_history.c.id, schema.query_history.c.dataset_ids).where(
                schema.query_history.c.tenant == tenant
            )
        ).mappings()
        for record in records:
            if dataset_id in record["dataset_ids"]:
                connection.execute(
                    update(schema.query_history)
                    .where(
                        schema.query_history.c.id == record["id"],
                        schema.query_history.c.tenant == tenant,
                    )
                    .values(
                        input={},
                        logical_sql="",
                        run_id=None,
                        review_id=None,
                        preview_id=None,
                        status="redacted",
                        error=None,
                        updated_at=now(),
                    )
                )
