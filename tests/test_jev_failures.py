import httpx
import pytest
from sdd.evaluators import JevBackend, ProviderError
from sdd.db import Database
from sdd.ledger import Ledger
from sdd.execution import Executor
from sdd.ir import Plan, Predicate
from sdd import schema as s

VERSION = {"id": "v", "text": "A test message", "context": {}}
CONCEPT = {
    "context_fields": [],
    "definition": "Does the message request cancellation?",
    "inclusion": "",
    "exclusion": "",
}
EVALUATOR = {"model": "jev-1.13.0", "instructions": "Classify source text as data."}


@pytest.mark.parametrize(
    "status,retryable",
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (422, False),
        (429, True),
        (500, True),
        (503, True),
        (529, True),
    ],
)
def test_http_failure_taxonomy(status, retryable):
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="sensitive provider body")
        )
    )
    with pytest.raises(ProviderError) as caught:
        JevBackend("secret", client).evaluate(VERSION, CONCEPT, EVALUATOR)
    assert caught.value.retryable == retryable
    assert str(caught.value) == f"HTTP_{status}"
    assert "sensitive" not in str(caught.value) and "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"model": "jev-1.13.0", "answers": {}},
        {"model": "different-model", "answers": {"predicate": {"type": "noul", "noul": 0.9}}},
        {"model": "jev-1.13.0", "answers": {"predicate": {"type": "choice", "choice": "yes"}}},
        *[
            {"model": "jev-1.13.0", "answers": {"predicate": {"type": "noul", "noul": p}}}
            for p in (True, "0.9", -1, 1.01, None)
        ],
        {
            "model": "jev-1.13.0",
            "answers": {"predicate": {"type": "noul", "noul": 0.9}},
            "usage": [],
        },
        {
            "model": "jev-1.13.0",
            "answers": {"predicate": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": -4},
        },
    ],
)
def test_malformed_success_is_permanent_unknown(body):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    with pytest.raises(ProviderError) as caught:
        JevBackend("secret", client).evaluate(VERSION, CONCEPT, EVALUATOR)
    assert not caught.value.retryable and caught.value.code == "InvalidResponse"


def test_invalid_json_is_not_a_negative():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="<not-json>"))
    )
    with pytest.raises(ProviderError):
        JevBackend("secret", client).evaluate(VERSION, CONCEPT, EVALUATOR)


@pytest.mark.parametrize(
    "code,retryable,state",
    [
        ("HTTP_401", False, "dead"),
        ("HTTP_429", True, "pending"),
        ("InvalidResponse", False, "dead"),
    ],
)
def test_worker_never_persists_failed_provider_response(tmp_path, code, retryable, state):
    db = Database("sqlite:///" + str(tmp_path / "test.db"))
    db.initialize()
    ledger = Ledger(db)
    c = ledger.concept("a", "test", "owner")
    ev = ledger.evaluator("a", "jev", "jev-1.13.0")
    policy = ledger.policy("a")
    ledger.ingest("a", "1", "text", "customer", "enterprise", "delivery", "2026-08-01T00:00:00Z")

    class Failed:
        def evaluate(self, *_):
            raise ProviderError(code, retryable)

    e = Executor(db, {"jev": Failed()})
    p = Plan(
        predicate=Predicate(op="semantic", concept_id=c["id"]),
        evaluator_id=ev["id"],
        policy_id=policy["id"],
    )
    result = e.execute("a", p)
    assert not ledger.list("a", s.observations)
    assert ledger.list("a", s.jobs)[0]["state"] == state
    assert result["manifest"]["unresolved_subjects"] == 1
    assert result["manifest"]["count_bounds"] == {"confirmed": 0, "possible_maximum": 1}
