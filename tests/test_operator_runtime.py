import threading
import time

import pytest

from sdd.db import Database
from sdd.evaluators import ProviderError
from sdd.operators.budget import Limits, expansion
from sdd.operators.service import OperatorService
from sdd.operators.types import Decision, OutputState as Out, logical


class Model:
    model = "jev-1.13.0"

    def __init__(self, probabilities=None, failure=None):
        self.probabilities = probabilities or {}
        self.failure = failure
        self.calls = self.active = self.peak = 0
        self.lock = threading.Lock()
        self.payloads = []

    def ask(self, tenant, state, questions):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.payloads.append((tenant, state, questions))
        try:
            time.sleep(0.005)
            if self.failure:
                raise self.failure
            answers = {}
            for key, q in questions.items():
                instructions = q["instructions"]
                if q["type"] == "noul":
                    p = (
                        self.probabilities.get(instructions, 0.95)
                        if isinstance(instructions, str)
                        else 0.95
                    )
                    answers[key] = {"type": "noul", "noul": p}
                elif q["type"] == "choice":
                    selected = next(iter(q["criteria"]))
                    answers[key] = {
                        "type": "choice",
                        "choice": selected,
                        "confidence": 1.0,
                        "probabilities": {k: float(k == selected) for k in q["criteria"]},
                    }
                else:
                    answers[key] = {
                        "type": "score",
                        "score": 1.0,
                        "confidence": 1.0,
                        "legend": {str(i): v for i, v in enumerate(q["criteria"])},
                        "probabilities": {str(i): float(i == 1) for i in range(len(q["criteria"]))},
                    }
            return {
                "model": self.model,
                "answers": answers,
                "usage": {"input_tokens": 20, "output_tokens": 5},
            }
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def operators():
    db = Database("sqlite:///:memory:")
    db.initialize()
    model = Model({"no": 0.05, "ambiguous": 0.5})
    yield db, model, OperatorService(db, model, "tenant-a", "reviewer-a", "reviewer")
    db.engine.dispose()


def test_false_unknown_and_unexecuted_are_different_and_not_truthy(operators):
    _, model, service = operators
    no = service.call("JEV.NOUL", {"state": "record", "proposition": "no"})
    d = next(iter(no["observations"].values()))
    assert d["output_state"] == "VALUE" and d["value"] is False
    unknown = service.call("JEV.NOUL", {"state": "record", "proposition": "ambiguous"})
    d = next(iter(unknown["observations"].values()))
    assert d["output_state"] == "UNKNOWN" and d["operation_state"] == "SUCCEEDED"
    blocked = service.call(
        "JEV.NOUL", {"state": "new", "proposition": "yes"}, limits={"max_judgments": 0}
    )
    d = next(iter(blocked["observations"].values()))
    assert d["output_state"] == "NOT_EVALUATED" and d["operation_state"] == "BLOCKED_BY_BUDGET"
    assert model.calls == 2
    with pytest.raises(TypeError):
        bool(Decision.known(False))
    assert logical("not", [Decision.unknown("unknown")]).output_state == Out.UNKNOWN
    assert logical("not", [Decision.unexecuted("skipped")]).output_state == Out.NOT_EVALUATED
    assert logical("and", [Decision.known(False), Decision.unexecuted("skipped")]).value is False


def test_conditional_branch_preserves_skipped_stage_and_blocks_dependents(operators):
    _, model, service = operators
    result = service.call(
        "JEV.WORKFLOW",
        {
            "stages": [
                {
                    "id": "gate",
                    "state": "source",
                    "question": {"type": "noul", "instructions": "no"},
                },
                {
                    "id": "stage2",
                    "when": {"stage": "gate", "equals": True},
                    "question": {"type": "noul", "instructions": "yes"},
                },
                {
                    "id": "stage3",
                    "depends_on": ["stage2"],
                    "question": {"type": "noul", "instructions": "yes"},
                },
            ]
        },
    )
    assert model.calls == 1
    stages = result["partial_value"]["stages"]
    assert stages["gate"]["value"] is False
    assert stages["stage2"]["output_state"] == "NOT_EVALUATED"
    assert stages["stage2"]["operation_state"] == "SKIPPED"
    assert stages["stage2"]["value"] is None
    assert stages["stage3"]["operation_state"] == "BLOCKED_BY_DEPENDENCY"


def test_cached_negatives_and_uncertainty_reuse_raw_evidence_with_new_policy(operators):
    _, model, service = operators
    args = {"state": {"text": "含糊的取消请求", "speaker": "customer"}, "proposition": "ambiguous"}
    first = service.call("NOUL", args)
    warm = service.call(
        "NOUL",
        args,
        limits={"max_judgments": 0},
        policy={"accept": 0.5, "reject": 0.1, "revision": "human-policy-2"},
    )
    assert first["output_state"] == "UNKNOWN"
    assert next(iter(warm["observations"].values()))["value"] is True
    assert model.calls == 1 and warm["manifest"]["reserved_requests"] == 0
    service.call("NOUL", {**args, "state": {"text": "含糊的取消请求", "speaker": "agent"}})
    assert model.calls == 2


def test_shared_state_batching_and_independent_context_concurrency(operators):
    _, model, service = operators
    items = [
        {
            "id": str(i),
            "subject_id": "s",
            "state": {"text": "shared"},
            "source_revisions": ["v1"],
            "question": {"type": "noul", "instructions": "q" + str(i)},
        }
        for i in range(4)
    ]
    result = service.call("EVALUATE", {"work_items": items})
    assert model.calls == 1 and result["manifest"]["evaluated"] == 4
    for i, w in enumerate(items):
        w["state"] = {"text": str(i)}
    service.call("EVALUATE", {"work_items": items})
    assert model.peak > 1 and model.peak <= 4


def test_failed_and_invalid_responses_never_become_negative_labels(operators):
    db, _, _ = operators
    for backend in (Model(failure=ProviderError("HTTP_503", True)), Model({"bad": float("nan")})):
        service = OperatorService(db, backend, "failed-" + str(id(backend)), "u")
        result = service.call(
            "NOUL", {"state": "record", "proposition": "bad"}, limits={"retries": 0}
        )
        d = next(iter(result["observations"].values()))
        assert d["output_state"] == "NOT_EVALUATED" and d["operation_state"] == "FAILED"
        assert d["value"] is None


def test_partial_budget_and_tenant_cache_isolation(operators):
    db, model, service = operators
    result = service.call(
        "TAG",
        {"subjects": ["a", "b", "c"], "concept_revs": ["p"]},
        limits={"max_judgments": 1, "concurrency": 1},
    )
    assert result["manifest"]["decided"] == 1
    assert result["manifest"]["not_evaluated"] == 2
    other = OperatorService(db, model, "other-tenant", "u")
    other.call("TAG", {"subjects": ["a"], "concept_revs": ["p"]})
    assert model.calls == 2


def test_payload_and_pair_bounds_prevent_dispatch(operators):
    _, model, service = operators
    result = service.call("NOUL", {"state": "中" * 12000, "proposition": "p"})
    assert result["operation_state"] == "BLOCKED_BY_POLICY" and model.calls == 0
    result = service.call(
        "JOIN",
        {
            "left": [str(i) for i in range(1100)],
            "right": [str(i) for i in range(1000)],
            "predicate": "related",
        },
    )
    assert result["operation_state"] == "BLOCKED_BY_BUDGET" and model.calls == 0
    assert expansion("EVIDENCE_JOIN", 1, 8, bundle_size=2) == 36
    assert expansion("EVIDENCE_JOIN", 1, 8, bundle_size=3) == 92
    with pytest.raises(ValueError):
        Limits(batch_size=33)


def test_literal_catalog_named_fields_are_data_not_scope_references(operators):
    _, model, service = operators
    result = service.call(
        "NOUL", {"state": {"dataset_id": "external-reference"}, "proposition": "yes"}
    )
    assert result["output_state"] == "VALUE" and model.calls == 1


def test_long_retry_after_is_deferred_without_retrying_early(operators):
    db, _, _ = operators
    model = Model(failure=ProviderError("HTTP_429", True, retry_after=120))
    service = OperatorService(db, model, "retry-test", "u")
    result = service.call("NOUL", {"state": "x", "proposition": "p"})
    assert model.calls == 1
    assert result["output_state"] == "NOT_EVALUATED"
    assert result["operation_state"] == "BLOCKED_BY_BUDGET"
    assert result["manifest"]["reserved_requests"] == 1


def test_cancellation_during_inference_does_not_publish_a_label(operators):
    from sqlalchemy import update
    from sdd.operators import schema

    db, model, service = operators
    original = model.ask

    def cancel(*args):
        reply = original(*args)
        with db.transaction(service.tenant) as connection:
            connection.execute(
                update(schema.runs)
                .where(schema.runs.c.id == service.runtime.run_id)
                .values(state="CANCELLED")
            )
        return reply

    model.ask = cancel
    result = service.call("NOUL", {"state": "x", "proposition": "yes"})
    assert result["operation_state"] == "CANCELLED" and result["output_state"] == "NOT_EVALUATED"
    stored = service.store.get(schema.runs, result["run_id"])
    assert stored["state"] == stored["result"]["operation_state"] == "CANCELLED"
    assert all(d["value"] is None for d in result["observations"].values())
