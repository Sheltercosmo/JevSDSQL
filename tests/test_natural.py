"""Language corpus asserts meaning, abstention, absence scope, and end-to-end reuse."""

from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sdd.db import Database
from sdd.ledger import Ledger
from sdd.execution import Executor
from sdd.evaluators import FixtureBackend
from sdd.natural import preview, ask, BUILTINS
from sdd.api import create_app
from sdd import schema as s


@pytest.fixture
def nl(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "nl.db"))
    db.initialize()
    ledger = Ledger(db)
    ledger.evaluator("a", "fixture", "test-v1")
    ledger.policy("a")
    for key, segment in [("yes", "enterprise"), ("no", "small_business"), ("maybe", "enterprise")]:
        ledger.ingest("a", key, key, key, segment, "delivery", "2026-08-15T00:00:00Z")
    e = Executor(db, {"fixture": FixtureBackend({"yes": 0.95, "no": 0.05, "maybe": 0.5})})
    return ledger, e


@pytest.mark.parametrize(
    "question,operation,grain,quantifier,group,limit",
    [
        ("How many customers might cancel?", "count", "customer", "exists", None, 100),
        (
            "Count distinct customers where cancellation intent",
            "count",
            "customer",
            "exists",
            None,
            100,
        ),
        ("Number of messages about late deliveries", "count", "message", "exists", None, 100),
        ("Show me 5 messages about delivery delays", "list", "message", "exists", None, 5),
        ("List 2 tickets where cancellation intent", "list", "message", "exists", None, 2),
        ("Find customers who might cancel", "list", "customer", "exists", None, 100),
        (
            "Group messages by product where late deliveries",
            "group",
            "message",
            "exists",
            "product",
            100,
        ),
        (
            "Count customers where cancellation intent by segment",
            "group",
            "customer",
            "exists",
            "segment",
            100,
        ),
        (
            "Top 3 products by customers where cancellation intent",
            "rank",
            "customer",
            "exists",
            "product",
            3,
        ),
        (
            "Rank segments by messages where delivery delays",
            "rank",
            "message",
            "exists",
            "segment",
            10,
        ),
        (
            "Count customers without messages about cancellation intent",
            "count",
            "customer",
            "not_exists",
            None,
            100,
        ),
        (
            "Show customers without messages where delivery delays",
            "list",
            "customer",
            "not_exists",
            None,
            100,
        ),
        ("Count messages", "count", "message", "exists", None, 100),
        ("Show customers", "list", "customer", "exists", None, 100),
        (
            "Count messages where “The author asks for a refund.”",
            "count",
            "message",
            "exists",
            None,
            100,
        ),
    ],
)
def test_supported_shapes(nl, question, operation, grain, quantifier, group, limit):
    ledger, _ = nl
    r = preview(question, ledger, "a", "owner")
    assert r["supported"], r
    assert [r["plan"][k] for k in ("operation", "grain", "quantifier", "group_by", "limit")] == [
        operation,
        grain,
        quantifier,
        group,
        limit,
    ]


@pytest.mark.parametrize(
    "question",
    [
        "How much revenue did we lose?",
        "Count customers who might cancel and revenue > 100",
        'Count customers where "cancel" and revenue > 100',
        'Count customers where "cancel"; DROP TABLE source_records',
        'Count customers where "cancel',
        'Count customers where "cancel" trailing words',
        "Count customers where cancellation intent OR",
        "Count customers where (delivery delays AND cancellation intent",
        "Count customers where delivery delays) AND cancellation intent",
        'Count customers where ""',
        "Count customers without cancellation intent",
        "Count messages without messages about delivery delays",
        "Count gold customers where cancellation intent",
        "Count customers where cancellation intent in August",
        "Count customers where cancellation intent in Smarch 2026",
        "Count customers where cancellation intent from 2026-09-01 to 2026-08-01",
        "Count customers where cancellation intent from 2026-08-01T00:00:00 to 2026-09-01T00:00:00",
        "Count customers where cancellation intent between 01/08/26 and 31/08/26",
        "Count customers who might cancel last year",
        "Show 0 messages about late deliveries",
        "Show 1001 messages about late deliveries",
        "Count 5 customers where cancellation intent",
        "Top 3 products by customers without messages about late deliveries",
        "Count customers where NOT",
        "Count customers where delivery delays AND OR cancellation intent",
        "Count customers where segment = enterprise",
        "Delete all messages",
    ],
)
def test_unsupported_has_no_concept_or_job_side_effect(nl, question):
    ledger, _ = nl
    before = ledger.list("a", s.concepts)
    r = preview(question, ledger, "a", "owner")
    assert not r["supported"], r
    assert ledger.list("a", s.concepts) == before
    assert not ledger.list("a", s.jobs)


def test_boolean_precedence_quotes_and_causal_predicate(nl):
    ledger, _ = nl
    r = preview(
        'Count messages where delivery delays OR cancellation intent AND NOT product = "billing"',
        ledger,
        "a",
        "owner",
    )
    p = r["plan"]["predicate"]
    assert (
        p["op"] == "or" and p["args"][1]["op"] == "and" and p["args"][1]["args"][1]["op"] == "not"
    )
    quoted = preview('Count messages where "A AND B, NOT C in August 2026"', ledger, "a", "owner")
    assert len(quoted["concepts"]) == 1 and quoted["plan"]["start"] is None
    causal = preview(
        "How many customers might cancel because deliveries were late?", ledger, "a", "owner"
    )
    assert len(causal["concepts"]) == 1
    assert (
        causal["concepts"][0]["definition"]
        == BUILTINS["current_leave_due_to_delivery"]["definition"]
    )


def test_months_dates_and_scope(nl):
    ledger, _ = nl
    r = preview("How many enterprise customers might cancel in August 2026?", ledger, "a", "owner")
    assert r["plan"]["start"] == "2026-08-01T00:00:00+00:00"
    assert r["plan"]["end"] == "2026-09-01T00:00:00+00:00"
    assert r["plan"]["scope"][0]["value"] == "enterprise"
    r = preview(
        "Count messages last month",
        ledger,
        "a",
        "owner",
        today=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )
    assert (
        r["plan"]["start"] == "2025-12-01T00:00:00+00:00"
        and r["plan"]["end"] == "2026-01-01T00:00:00+00:00"
    )
    r = preview("Count messages from 2026-08-01T01:00:00+01:00 to 2026-09-01", ledger, "a", "owner")
    assert r["plan"]["start"] == "2026-08-01T00:00:00+00:00"


def test_aliases_reuse_and_threshold_policy(nl):
    ledger, e = nl
    first = ask(e, "a", "owner", "Count messages about late deliveries")
    second = ask(e, "a", "owner", "How many messages mentioned late deliveries?")
    assert first["result"] == second["result"] == [{"count": 1}]
    assert second["manifest"]["reused_observations"] == 3
    assert len(ledger.list("a", s.concepts)) == 1
    assert second["manifest"]["count_bounds"] == {"confirmed": 1, "possible_maximum": 2}


def test_without_messages_population_scope(nl):
    _, e = nl
    r = ask(
        e, "a", "owner", "Count enterprise customers without messages about cancellation intent"
    )
    # The small-business negative customer must not leak into the enterprise count.
    assert r["result"] == [{"count": 0}] and r["manifest"]["eligible_subjects"] == 2
    assert r["manifest"]["count_bounds"] == {"confirmed": 0, "possible_maximum": 1}


def test_ambiguous_defaults_and_foreign_revisions(nl):
    ledger, _ = nl
    ledger.policy("a", accept=0.9, reject=0.1)
    r = preview("Count messages", ledger, "a", "owner")
    assert not r["supported"] and "Select a decision policy" in r["reason"]
    ev = ledger.evaluator("b", "fixture", "test-v1")
    assert not preview("Count messages", ledger, "a", "owner", evaluator_id=ev["id"])["supported"]


def test_natural_api_shortcut_and_web(nl):
    ledger, e = nl
    client = TestClient(
        create_app(e, {"token": {"tenant": "a", "name": "owner", "role": "reviewer"}})
    )
    assert client.get("/ask").status_code == 200
    assert "default-src 'self'" in client.get("/ask").headers["content-security-policy"]
    assert client.post("/legacy/ask", json={"question": "Count messages"}).status_code in (401, 403)
    headers = {"Authorization": "Bearer token"}
    r = client.post(
        "/legacy/ask",
        json={"question": "Count messages about late deliveries", "execute": False},
        headers=headers,
    )
    assert r.json()["supported"] and not ledger.list("a", s.jobs)
    r = client.post(
        "/legacy/ask", json={"question": "Count messages about late deliveries"}, headers=headers
    )
    assert r.status_code == 200 and r.json()["result"] == [{"count": 1}]
    assert (
        client.post(
            "/legacy/ask", json={"question": "Count messages", "tenant": "b"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/legacy/ask",
            json={"question": "Count messages", "max_evaluations": -1},
            headers=headers,
        ).status_code
        == 422
    )


def test_no_match_empty_population_and_no_semantics(nl):
    _, e = nl
    assert ask(e, "a", "owner", "Count messages")["result"] == [{"count": 3}]
    r = ask(e, "a", "owner", 'Count messages where product = "not-present"')
    assert r["result"] == [{"count": 0}] and r["manifest"]["scheduled_pairs"] == 0
    r = ask(
        e,
        "a",
        "owner",
        "Count customers without messages about cancellation intent in January 2020",
    )
    assert r["result"] == [{"count": 0}] and r["manifest"]["complete"]


def test_pathological_boolean_input_abstains_without_recursion(nl):
    ledger, _ = nl
    r = preview(
        "Count messages where " + "NOT " * 800 + "cancellation intent", ledger, "a", "owner"
    )
    assert not r["supported"] and "limit" in r["reason"]
    assert not ledger.list("a", s.concepts)


def test_retired_concept_does_not_leave_partial_provisionals(nl):
    from sqlalchemy import update

    ledger, _ = nl
    old = ledger.concept("a", "retired definition", "owner")
    with ledger.db.transaction("a") as cx:
        cx.execute(
            update(s.concepts).where(s.concepts.c.id == old["id"]).values(status="deprecated")
        )
    count = len(ledger.list("a", s.concepts))
    r = preview(
        'Count messages where "new definition" AND "retired definition"', ledger, "a", "owner"
    )
    assert not r["supported"] and len(ledger.list("a", s.concepts)) == count


def test_message_lists_include_source_text_and_interpretation(nl):
    _, e = nl
    r = ask(e, "a", "owner", "Show 2 messages about late deliveries")
    assert r["result"][0]["text"] == "yes"
    assert r["interpretation"]["condition"] == BUILTINS["delivery_delay"]["definition"]
