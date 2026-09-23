import json
import pytest
from sqlalchemy import update
from fastapi.testclient import TestClient
from sdd.db import Database
from sdd.ledger import Ledger
from sdd.execution import Executor
from sdd.evaluators import FixtureBackend, Evaluation, JevBackend
from sdd.ir import Plan, Predicate
from sdd import schema as s


@pytest.fixture
def env(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "test.db"))
    db.initialize()
    ledger = Ledger(db)
    concept = ledger.concept("a", "Expresses intent to cancel", "owner")
    evaluator = ledger.evaluator("a", "fixture", "test-v1")
    policy = ledger.policy("a")
    backend = FixtureBackend(
        {"yes": 0.95, "no": 0.05, "maybe": 0.5, "fail": RuntimeError("outage")}
    )
    executor = Executor(db, {"fixture": backend})
    plan = Plan(
        predicate=Predicate(op="semantic", concept_id=concept["id"]),
        evaluator_id=evaluator["id"],
        policy_id=policy["id"],
    )
    return db, ledger, backend, executor, plan


def ingest(ledger, text, key=None, customer=None, tenant="a", **kw):
    return ledger.ingest(
        tenant,
        key or text,
        text,
        customer or key or text,
        kw.pop("segment", "enterprise"),
        "delivery",
        kw.pop("event_time", "2026-08-15T00:00:00Z"),
        **kw,
    )


def test_cold_warm_and_distinct_grain(env):
    _, ledger, b, e, p = env
    ingest(ledger, "yes", "1", "same")
    ingest(ledger, "yes", "2", "same")
    ingest(ledger, "no")
    first = e.execute("a", p)
    second = e.execute("a", p)
    assert first["result"] == second["result"] == [{"count": 1}]
    assert b.calls == 3 and second["manifest"]["reused_observations"] == 3
    assert first["manifest"]["complete"]


def test_update_new_and_context_invalidation(env):
    _, ledger, b, e, p = env
    old = ingest(ledger, "yes", "1", context={"thread": "old"})
    unchanged = ingest(ledger, "yes", "1", context={"thread": "old"})
    assert old["id"] == unchanged["id"]
    e.execute("a", p)
    new = ingest(ledger, "no", "1", context={"thread": "new"})
    ingest(ledger, "yes", "2")
    result = e.execute("a", p)
    assert b.calls == 3 and new["id"] != old["id"]
    assert result["result"] == [{"count": 1}]
    assert len(ledger.list("a", s.versions)) == 3


def test_threshold_change_reuses_probabilities(env):
    _, ledger, b, e, p = env
    ingest(ledger, "maybe")
    assert not e.execute("a", p)["manifest"]["complete"]
    p.policy_id = ledger.policy("a", accept=0.5, reject=0.1)["id"]
    assert e.execute("a", p)["result"] == [{"count": 1}]
    assert b.calls == 1


def test_negation_preserves_unknown_and_failure(env):
    _, ledger, b, e, p = env
    for text in ("yes", "no", "maybe", "fail"):
        ingest(ledger, text)
    p.predicate = Predicate(op="not", args=[p.predicate])
    result = e.execute("a", p)
    assert result["result"] == [{"count": 1}]
    assert result["manifest"]["unresolved_subjects"] == 2
    assert result["manifest"]["observation_states"]["failed"] == 1
    assert not result["manifest"]["complete"]


def test_or_keeps_population_and_sql_unknown_shortcuts(env):
    _, ledger, _, e, p = env
    ingest(ledger, "maybe", segment="enterprise")
    ingest(ledger, "no", segment="small")
    p.predicate = Predicate(
        op="or", args=[p.predicate, Predicate(op="eq", field="segment", value="enterprise")]
    )
    assert e.execute("a", p)["result"] == [{"count": 1}]
    assert e.execute("a", p)["manifest"]["complete"]


def test_limit_after_semantics_and_grouping(env):
    _, ledger, _, e, p = env
    ingest(ledger, "no")
    ingest(ledger, "yes")
    ingest(ledger, "yes", "two")
    p.operation, p.grain, p.limit = "list", "message", 1
    result = e.execute("a", p)
    assert len(result["result"]) == 1 and result["manifest"]["resolved_positive"] == 2
    p.operation, p.group_by = "group", "segment"
    assert e.execute("a", p)["result"] == [{"segment": "enterprise", "count": 2}]


def test_time_boundaries_and_budget(env):
    _, ledger, _, e, p = env
    ingest(ledger, "yes", "inside", event_time="2026-08-01T00:00:00Z")
    ingest(ledger, "yes", "outside", event_time="2026-09-01T00:00:00Z")
    p.start, p.end, p.max_evaluations = "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00", 0
    r = e.execute("a", p)
    assert r["manifest"]["eligible_subjects"] == 1
    assert r["manifest"]["observation_states"] == {"budget_exhausted": 1}
    assert not r["manifest"]["complete"]


def test_tenant_boundary_and_api_auth(env):
    _, ledger, _, e, p = env
    from sdd.api import create_app

    ingest(ledger, "yes")
    ingest(ledger, "yes", tenant="b")
    with pytest.raises(ValueError):
        e.execute("b", p)
    app = create_app(
        e,
        {
            "reader": {"tenant": "a", "name": "reader", "role": "reader"},
            "other": {"tenant": "b", "name": "reader", "role": "reader"},
        },
    )
    client = TestClient(app)
    assert client.get("/catalog").status_code in (401, 403)
    assert (
        client.post(
            "/query", json=p.model_dump(), headers={"Authorization": "Bearer other"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/reviews",
            json={"version_id": "x", "concept_id": "x", "decision": "true", "reason": "test"},
            headers={"Authorization": "Bearer reader"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/query",
            json={**p.model_dump(), "tenant": "b"},
            headers={"Authorization": "Bearer reader"},
        ).status_code
        == 422
    )


def test_human_correction_and_old_run_preserved(env):
    _, ledger, b, e, p = env
    version = ingest(ledger, "no")
    first = e.execute("a", p)
    assertion = ledger.review(
        "a", version["id"], p.predicate.concept_id, "true", "reviewer", "Reviewed original message"
    )
    assert e.execute("a", p)["result"] == [{"count": 1}]
    assert ledger.get("a", s.runs, first["run_id"])["result"]["rows"] == [{"count": 0}]
    next_assertion = ledger.review(
        "a", version["id"], p.predicate.concept_id, "unknown", "reviewer", "Need context"
    )
    assert next_assertion["supersedes"] == assertion["id"]
    assert not e.execute("a", p)["manifest"]["complete"] and b.calls == 1


def test_missing_context_never_submitted(env):
    _, ledger, b, e, p = env
    p.predicate.concept_id = ledger.concept(
        "a", "Uses conversation context", "owner", context_fields=["conversation"]
    )["id"]
    ingest(ledger, "yes")
    r = e.execute("a", p)
    assert r["manifest"]["observation_states"] == {"missing_context": 1} and b.calls == 0


def test_revisions_invalidate(env):
    _, ledger, b, e, p = env
    ingest(ledger, "yes")
    e.execute("a", p)
    p.evaluator_id = ledger.evaluator("a", "fixture", "test-v2")["id"]
    e.execute("a", p)
    p.predicate.concept_id = ledger.concept("a", "Actually requests cancellation", "owner")["id"]
    e.execute("a", p)
    assert b.calls == 3


def test_delete_cascades_and_redacts(env):
    _, ledger, _, e, p = env
    v = ingest(ledger, "yes")
    r = e.execute("a", p)
    ledger.review("a", v["id"], p.predicate.concept_id, "true", "r", "checked")
    ledger.delete_record("a", v["record_id"])
    for table in (s.records, s.versions, s.observations, s.jobs, s.attempts, s.assertions):
        assert ledger.list("a", table) == []
    saved = ledger.get("a", s.runs, r["run_id"])
    assert saved["snapshot"] == [] and saved["result"] == {}


def test_retry_limit_cancellation_and_lease_recovery(env):
    db, ledger, b, e, p = env
    ingest(ledger, "fail")
    e.execute("a", p)
    job = ledger.list("a", s.jobs)[0]
    for _ in range(2):
        with db.transaction("a") as cx:
            cx.execute(update(s.jobs).where(s.jobs.c.id == job["id"]).values(available_at=0))
        assert e.workers.work_one("a")
    assert ledger.get("a", s.jobs, job["id"])["state"] == "dead"
    assert len(ledger.list("a", s.attempts)) == 3 and b.calls == 3


def test_inflight_delete_cannot_resurrect(env):
    _, ledger, _, e, p = env
    v = ingest(ledger, "yes")

    class DeleteBackend:
        def evaluate(self, *args):
            ledger.delete_record("a", v["record_id"])
            return Evaluation(0.99, "test-v1")

    e.workers.backends["fixture"] = DeleteBackend()
    with pytest.raises(ValueError, match="deleted"):
        e.execute("a", p)
    assert ledger.list("a", s.observations) == []


def test_governed_maintenance(env):
    _, ledger, b, e, p = env
    from sdd.maintenance import create_policy, refresh

    cid = p.predicate.concept_id
    with pytest.raises(ValueError):
        ledger.promote("a", cid, "active", "owner", "skip review")
    with pytest.raises(ValueError):
        ledger.promote("a", cid, "validated", "owner", "no evidence")
    ledger.promote(
        "a",
        cid,
        "validated",
        "owner",
        "reviewed",
        examples=[{"text": "yes", "decision": True}],
        quality={"precision": 1, "recall": 1, "sample_size": 1},
    )
    ledger.promote("a", cid, "active", "owner", "approved pilot")
    m = create_policy(ledger, "a", cid, p.evaluator_id, p.policy_id, "owner")
    ingest(ledger, "yes")
    refresh(e, "a", m["id"])
    refresh(e, "a", m["id"])
    assert b.calls == 1
    ingest(ledger, "no")
    refresh(e, "a", m["id"])
    assert b.calls == 2
    ledger.promote("a", cid, "deprecated", "owner", "retired")
    with pytest.raises(ValueError):
        refresh(e, "a", m["id"])


def test_planner_consumes_entire_request(env):
    _, ledger, _, _, p = env
    from sdd.planner import preview

    args = (ledger, "a", "owner", p.evaluator_id, p.policy_id)
    assert preview('count customers where "Threatens to leave"', *args)["supported"]
    assert not preview('count customers where "Threatens to leave" and revenue > 100', *args)[
        "supported"
    ]


def test_jev_contract_and_version_pin():
    import httpx

    def handler(request):
        payload = json.loads(request.content)
        assert payload["questions"]["predicate"]["type"] == "noul"
        assert payload["state"] == {"message": "hello", "context": {"thread": "context"}}
        return httpx.Response(
            200,
            json={
                "model": "jev-test",
                "answers": {"predicate": {"type": "noul", "noul": 0.91}},
                "usage": {"input_tokens": 20},
            },
        )

    backend = JevBackend("test", httpx.Client(transport=httpx.MockTransport(handler)))
    result = backend.evaluate(
        {"id": "v", "text": "hello", "context": {"thread": "context", "secret": "omit"}},
        {"context_fields": ["thread"], "definition": "test", "inclusion": "", "exclusion": ""},
        {"model": "jev-test", "instructions": "classify"},
    )
    assert result.probability == 0.91
    for value in (float("nan"), -1, 2, True):
        with pytest.raises(ValueError):
            Evaluation(value, "x").validate("x")
    with pytest.raises(ValueError):
        Evaluation(0.5, "new-model").validate("pinned-model")


def test_explore_never_claims_completeness(env):
    _, ledger, _, e, p = env
    ingest(ledger, "yes")
    ingest(ledger, "no")
    p.mode, p.candidate_limit = "explore", 1
    r = e.execute("a", p)
    assert r["manifest"]["eligible_subjects"] == 2 and r["manifest"]["selected_subjects"] == 1
    assert not r["manifest"]["complete"]


def test_customer_without_matches_is_not_message_negation(env):
    _, ledger, _, e, p = env
    ingest(ledger, "yes", "1", "mixed")
    ingest(ledger, "no", "2", "mixed")
    ingest(ledger, "no", "3", "negative")
    ingest(ledger, "maybe", "4", "uncertain")
    p.quantifier = "not_exists"
    r = e.execute("a", p)
    assert r["result"] == [{"count": 1}]
    assert r["manifest"]["unresolved_entities"] == 1
    assert not r["manifest"]["complete"]


def test_cancelled_job_stays_unknown(env):
    _, ledger, b, e, p = env
    ingest(ledger, "yes")
    e.execute("a", p, inline=False)
    job = ledger.list("a", s.jobs)[0]
    e.workers.cancel("a", job["id"])
    result = e.execute("a", p)
    assert b.calls == 0
    assert result["manifest"]["observation_states"] == {"cancelled": 1}


def test_expired_worker_lease_is_recovered(env):
    db, ledger, b, e, p = env
    ingest(ledger, "yes")
    e.execute("a", p, inline=False)
    job = ledger.list("a", s.jobs)[0]
    with db.transaction("a") as cx:
        cx.execute(
            update(s.jobs)
            .where(s.jobs.c.id == job["id"])
            .values(state="running", lease_until=0, lease_token="expired")
        )
    assert e.workers.work_one("a")
    assert len(ledger.list("a", s.observations)) == 1 and b.calls == 1


def test_daily_tenant_budget_blocks_inference(env, monkeypatch):
    _, ledger, b, e, p = env
    monkeypatch.setenv("SDD_DAILY_EVALUATIONS", "1")
    ingest(ledger, "yes")
    ingest(ledger, "no")
    result = e.execute("a", p)
    assert b.calls == 1
    assert not result["manifest"]["complete"]
    assert ledger.list("a", s.usage_budgets)[0]["calls"] == 1
    assert (
        result["manifest"]["count_bounds"]["possible_maximum"]
        >= result["manifest"]["count_bounds"]["confirmed"]
    )
