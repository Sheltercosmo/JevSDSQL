"""Integration tests against an explicitly supplied isolated PostgreSQL runtime role."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select, update, text
from sqlalchemy.exc import DBAPIError
from sdd.db import Database
from sdd.ledger import Ledger
from sdd.execution import Executor
from sdd.evaluators import FixtureBackend
from sdd.ir import Predicate, Plan
from sdd import schema as s

pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_POSTGRES_URL"),
    reason="Set SDD_TEST_POSTGRES_URL for PostgreSQL integration",
)


@pytest.fixture
def pg():
    db = Database(os.environ["SDD_TEST_POSTGRES_URL"])
    tenant = "test-" + uuid.uuid4().hex
    ledger = Ledger(db)
    c = ledger.concept(tenant, "Test cancellation", "owner")
    ev = ledger.evaluator(tenant, "fixture", "test-v1")
    po = ledger.policy(tenant)
    backend = FixtureBackend({"yes": 0.95, "no": 0.05})
    e = Executor(db, {"fixture": backend})
    p = Plan(
        predicate=Predicate(op="semantic", concept_id=c["id"]),
        evaluator_id=ev["id"],
        policy_id=po["id"],
    )
    return db, tenant, ledger, backend, e, p


def put(ledger, tenant, key="1", text="yes"):
    return ledger.ingest(
        tenant, key, text, "customer", "enterprise", "delivery", "2026-08-01T00:00:00Z"
    )


def test_pg_runtime_role_and_rls(pg):
    db, tenant, ledger, _, _, _ = pg
    put(ledger, tenant)
    with db.transaction(tenant) as cx:
        role = cx.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        assert not role.rolsuper and not role.rolbypassrls
        assert len(cx.execute(select(s.versions)).all()) == 1
    with db.transaction(tenant + "-other") as cx:
        assert cx.execute(select(s.versions)).all() == []
    with db.engine.begin() as cx:
        assert cx.execute(select(s.versions)).all() == []


def test_pg_cold_warm_and_immutability(pg):
    db, tenant, ledger, b, e, p = pg
    v = put(ledger, tenant)
    a = e.execute(tenant, p)
    z = e.execute(tenant, p)
    assert a["result"] == z["result"] == [{"count": 1}] and b.calls == 1
    with pytest.raises(DBAPIError):
        with db.transaction(tenant) as cx:
            cx.execute(update(s.versions).where(s.versions.c.id == v["id"]).values(text="tamper"))
    with pytest.raises(DBAPIError):
        with db.transaction(tenant) as cx:
            cx.execute(
                update(s.concepts)
                .where(s.concepts.c.id == p.predicate.concept_id)
                .values(definition="tamper")
            )


def test_pg_concurrent_workers_one_observation(pg):
    _, tenant, ledger, b, e, p = pg
    put(ledger, tenant)
    e.execute(tenant, p, inline=False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: e.workers.work_one(tenant), range(4)))
    assert len(ledger.list(tenant, s.observations)) == 1 and b.calls == 1


def test_pg_concurrent_queries_share_work(pg):
    _, tenant, ledger, b, e, p = pg
    put(ledger, tenant)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: e.execute(tenant, p), range(2)))
    assert len(ledger.list(tenant, s.observations)) == 1 and b.calls == 1
    assert e.execute(tenant, p)["result"] == [{"count": 1}]


def test_pg_concurrent_natural_queries_register_one_concept(pg):
    from sdd.natural import ask

    _, tenant, ledger, b, e, _ = pg
    put(ledger, tenant)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: ask(e, tenant, "owner", "Count messages about late deliveries"), range(4)
            )
        )
    assert all(r["supported"] for r in results)
    assert (
        len([c for c in ledger.list(tenant, s.concepts) if c["concept_key"] == "delivery_delay"])
        == 1
    )
    assert len(ledger.list(tenant, s.observations)) == 1 and b.calls == 1
