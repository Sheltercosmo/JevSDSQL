"""Durable leased work. Inference happens outside database transactions."""

import time
import os
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, update, insert, or_
from . import schema as s
from .ledger import Ledger, digest, uid, now
from .evaluators import ProviderError


class Workers:
    def __init__(self, db, backends):
        self.db, self.backends, self.ledger = db, backends, Ledger(db)

    def enqueue(self, tenant, version, concept, evaluator_id, conn):
        missing = set(concept["context_fields"]) - version["context"].keys()
        if missing:
            return None
        context_hash = digest({k: version["context"][k] for k in concept["context_fields"]})
        key = digest([tenant, version["id"], context_hash, concept["id"], evaluator_id])
        dialect_insert = __import__(
            "sqlalchemy.dialects." + self.db.engine.dialect.name, fromlist=["insert"]
        ).insert
        conn.execute(
            dialect_insert(s.jobs)
            .values(
                id=key,
                tenant=tenant,
                version_id=version["id"],
                concept_id=concept["id"],
                evaluator_id=evaluator_id,
                context_hash=context_hash,
                state="pending",
                lease_until=0,
                attempts=0,
                available_at=0,
                max_attempts=3,
            )
            .on_conflict_do_nothing()
        )
        return key

    def work_one(self, tenant, job_ids=None):
        stamp, token = time.time(), uid()
        with self.db.transaction(tenant) as cx:
            stmt = select(s.jobs).where(
                s.jobs.c.tenant == tenant,
                s.jobs.c.state.in_(["pending", "running"]),
                s.jobs.c.available_at <= stamp,
                or_(s.jobs.c.state == "pending", s.jobs.c.lease_until < stamp),
            )
            if job_ids is not None:
                stmt = stmt.where(s.jobs.c.id.in_(job_ids))
            row = (
                cx.execute(
                    stmt.order_by(s.jobs.c.available_at, s.jobs.c.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .first()
            )
            if not row:
                return False
            row = dict(row)
            # Expired attempts remain in history, even if a former worker never returned.
            if row["state"] == "running":
                cx.execute(
                    update(s.attempts)
                    .where(
                        s.attempts.c.tenant == tenant,
                        s.attempts.c.job_id == row["id"],
                        s.attempts.c.status == "running",
                    )
                    .values(status="lease_expired", finished_at=stamp)
                )
            if row["attempts"] >= row["max_attempts"]:
                cx.execute(
                    update(s.jobs)
                    .where(s.jobs.c.id == row["id"], s.jobs.c.tenant == tenant)
                    .values(state="dead", error="Retry limit reached after lease expiry")
                )
                return True
            # Reserve a per-tenant daily inference allowance atomically before making a call.
            day = datetime.now(timezone.utc).date()
            allowance = int(os.getenv("SDD_DAILY_EVALUATIONS", "1000"))
            usage_id = digest([tenant, str(day)])
            dialect_insert = __import__(
                "sqlalchemy.dialects." + self.db.engine.dialect.name, fromlist=["insert"]
            ).insert
            cx.execute(
                dialect_insert(s.usage_budgets)
                .values(id=usage_id, tenant=tenant, day=str(day), calls=0)
                .on_conflict_do_nothing()
            )
            reserved = cx.execute(
                update(s.usage_budgets)
                .where(
                    s.usage_budgets.c.id == usage_id,
                    s.usage_budgets.c.tenant == tenant,
                    s.usage_budgets.c.calls < allowance,
                )
                .values(calls=s.usage_budgets.c.calls + 1)
            )
            if not reserved.rowcount:
                tomorrow = datetime.combine(
                    day + timedelta(days=1), datetime.min.time(), timezone.utc
                ).timestamp()
                cx.execute(
                    update(s.jobs)
                    .where(s.jobs.c.id == row["id"], s.jobs.c.tenant == tenant)
                    .values(
                        state="pending",
                        error="DailyBudgetExceeded",
                        lease_until=0,
                        available_at=tomorrow,
                    )
                )
                return True
            claimed = cx.execute(
                update(s.jobs)
                .where(
                    s.jobs.c.id == row["id"],
                    s.jobs.c.tenant == tenant,
                    s.jobs.c.attempts == row["attempts"],
                    s.jobs.c.lease_until == row["lease_until"],
                )
                .values(
                    state="running",
                    lease_token=token,
                    lease_until=stamp + 120,
                    attempts=row["attempts"] + 1,
                )
            )
            if not claimed.rowcount:
                return False
            attempt_id = uid()
            cx.execute(
                insert(s.attempts).values(
                    id=attempt_id,
                    tenant=tenant,
                    job_id=row["id"],
                    lease_token=token,
                    status="running",
                    started_at=stamp,
                    usage={},
                )
            )
            version = self.ledger.get(tenant, s.versions, row["version_id"], cx)
            concept = self.ledger.get(tenant, s.concepts, row["concept_id"], cx)
            evaluator = self.ledger.get(tenant, s.evaluators, row["evaluator_id"], cx)
        result, error, retryable = None, None, True
        try:
            if concept["status"] == "deprecated":
                raise ValueError("Concept is deprecated")
            result = self.backends[evaluator["provider"]].evaluate(version, concept, evaluator)
            result.validate(evaluator["model"])
        except Exception as exc:
            # Do not store provider bodies, secrets, or copies of source text in errors.
            error = exc.code if isinstance(exc, ProviderError) else type(exc).__name__
            retryable = (
                exc.retryable
                if isinstance(exc, ProviderError)
                else not isinstance(exc, (ValueError, KeyError, TypeError))
            )
        with self.db.transaction(tenant) as cx:
            # Deletion locks the record first too. A deleted source cannot be resurrected.
            alive = cx.execute(
                select(s.records.c.id)
                .where(s.records.c.id == version["record_id"], s.records.c.tenant == tenant)
                .with_for_update()
            ).first()
            if not alive:
                return True
            current = (
                cx.execute(
                    select(s.jobs)
                    .where(s.jobs.c.id == row["id"], s.jobs.c.tenant == tenant)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if not current:
                return True
            owns_lease = current["state"] == "running" and current["lease_token"] == token
            cx.execute(
                update(s.attempts)
                .where(s.attempts.c.id == attempt_id, s.attempts.c.tenant == tenant)
                .values(
                    status=("failed" if error else "succeeded") if owns_lease else "superseded",
                    error=error,
                    finished_at=time.time(),
                    usage=result.usage if result else {},
                )
            )
            if not owns_lease:
                return True
            if error:
                state = (
                    "dead"
                    if not retryable or current["attempts"] >= current["max_attempts"]
                    else "pending"
                )
                cx.execute(
                    update(s.jobs)
                    .where(s.jobs.c.id == row["id"], s.jobs.c.tenant == tenant)
                    .values(
                        state=state,
                        error=error,
                        lease_until=0,
                        available_at=time.time() + 2 ** current["attempts"],
                    )
                )
            else:
                cx.execute(
                    insert(s.observations).values(
                        id=uid(),
                        tenant=tenant,
                        job_id=row["id"],
                        version_id=row["version_id"],
                        concept_id=row["concept_id"],
                        evaluator_id=row["evaluator_id"],
                        context_hash=row["context_hash"],
                        probability=result.probability,
                        distribution={"true": result.probability, "false": 1 - result.probability},
                        responder=result.responder,
                        evidence=result.evidence,
                        usage=result.usage,
                        created_at=now(),
                    )
                )
                cx.execute(
                    update(s.jobs)
                    .where(s.jobs.c.id == row["id"], s.jobs.c.tenant == tenant)
                    .values(state="done", error=None, lease_until=0)
                )
        return True

    def cancel(self, tenant, job_id):
        with self.db.transaction(tenant) as cx:
            self.ledger.get(tenant, s.jobs, job_id, cx)
            cx.execute(
                update(s.jobs)
                .where(
                    s.jobs.c.id == job_id,
                    s.jobs.c.tenant == tenant,
                    s.jobs.c.state.in_(["pending", "running"]),
                )
                .values(state="cancelled", lease_token=None)
            )
