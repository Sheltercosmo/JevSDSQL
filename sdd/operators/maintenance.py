"""Bounded change subscriptions; workers refresh only reviewer-approved populations."""

import time

from sqlalchemy import select, update

from . import schema
from .service import OperatorService
from .store import Store


def work_one(db, decisions, tenant):
    now = time.time()
    with db.transaction(tenant) as connection:
        row = (
            connection.execute(
                select(schema.subscriptions)
                .where(
                    schema.subscriptions.c.tenant == tenant,
                    schema.subscriptions.c.remaining > 0,
                    schema.subscriptions.c.next_check <= now,
                    schema.subscriptions.c.lease_until <= now,
                )
                .order_by(schema.subscriptions.c.next_check)
                .limit(1)
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        row = dict(row)
        claimed = connection.execute(
            update(schema.subscriptions)
            .where(
                schema.subscriptions.c.id == row["id"],
                schema.subscriptions.c.tenant == tenant,
                schema.subscriptions.c.lease_until <= now,
            )
            .values(lease_until=now + 900, next_check=now + row["interval_seconds"])
        )
        if not claimed.rowcount:
            return False
    store = Store(db, tenant, row["owner"])
    service = OperatorService(db, decisions, tenant, row["owner"], "reviewer")
    generation = store.get(schema.generations, row["generation_id"])
    state, next_generation = "UNCHANGED", row["generation_id"]
    charged = False
    try:
        source = generation["target_scope"]
        dataset = service.catalog.get(tenant, source["dataset_id"])
        snapshot = service.population_hash(service.catalog.rows(tenant, dataset, limit=5001))
        if [snapshot] == generation["snapshot"]:
            return True
        if decisions.model != generation["coverage"]["model"]:
            state = "MODEL_CHANGED"
            return True
        with db.transaction(tenant) as connection:
            connection.execute(
                update(schema.generations)
                .where(
                    schema.generations.c.id == generation["id"],
                    schema.generations.c.tenant == tenant,
                )
                .values(state="STALE")
            )
            connection.execute(
                update(schema.subscriptions)
                .where(
                    schema.subscriptions.c.id == row["id"],
                    schema.subscriptions.c.tenant == tenant,
                    schema.subscriptions.c.lease_until == now + 900,
                )
                .values(remaining=schema.subscriptions.c.remaining - 1)
            )
        charged = True
        result = service.call(
            "MATERIALIZE",
            {
                "concept_rev": generation["definition_id"],
                "target_scope": source,
                "refresh_policy": {"mode": "explicit"},
            },
            row["limits"],
            row["policy"],
        )
        payload = result.get("value") or result.get("partial_value") or {}
        replacement = payload.get("generation")
        if replacement:
            next_generation, state = replacement["id"], replacement["state"]
        else:
            state = result["operation_state"]
    except Exception:
        state = "FAILED"
    finally:
        with db.transaction(tenant) as connection:
            connection.execute(
                update(schema.subscriptions)
                .where(
                    schema.subscriptions.c.id == row["id"],
                    schema.subscriptions.c.tenant == tenant,
                    schema.subscriptions.c.lease_until == now + 900,
                )
                .values(
                    generation_id=next_generation,
                    lease_until=0,
                    last_state=state,
                    remaining=max(0, row["remaining"] - 1) if charged else row["remaining"],
                )
            )
    return True
