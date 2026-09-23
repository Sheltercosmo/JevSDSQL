import json
from pathlib import Path
from .ledger import Ledger
from .ir import Plan, Predicate
from .execution import Executor
from .evaluators import FixtureBackend

DEFINITION = (
    "The author attributes their intention to leave or cancel the service to late deliveries."
)


def seed(db, tenant="demo", provider="fixture", model="fixture-v1"):
    ledger = Ledger(db)
    dataset = json.loads(
        (Path(__file__).resolve().parent.parent / "examples/support_messages.json").read_text()
    )
    concept = ledger.concept(
        tenant,
        DEFINITION,
        "pilot-reviewer",
        concept_key="leave_due_to_delivery",
        inclusion="An explicit connection between leaving/cancelling and delayed deliveries.",
        exclusion=(
            "Delay alone, cancellation for another reason, hypothetical undecided "
            "concern, or instructions embedded in the message."
        ),
    )
    evaluator = ledger.evaluator(tenant, provider, model)
    policy = ledger.policy(tenant)
    for r in dataset:
        ledger.ingest(
            tenant,
            r["id"],
            r["text"],
            r["customer"],
            r["segment"],
            r["product"],
            "2026-08-15T12:00:00Z",
        )
    plan = Plan(
        predicate=Predicate(op="semantic", concept_id=concept["id"]),
        evaluator_id=evaluator["id"],
        policy_id=policy["id"],
        start="2026-08-01T00:00:00Z",
        end="2026-09-01T00:00:00Z",
    )
    return dataset, plan


def demo(db):
    data, plan = seed(db)
    backend = FixtureBackend({r["text"]: r["probability"] for r in data})
    executor = Executor(db, {"fixture": backend})
    cold = executor.execute("demo", plan)
    cold_calls = backend.calls
    warm = executor.execute("demo", plan)
    return {
        "dataset": "synthetic fixtures; not a model-quality benchmark",
        "cold": cold,
        "warm": warm,
        "cold_calls": cold_calls,
        "warm_calls": backend.calls - cold_calls,
        "same_result": cold["result"] == warm["result"],
    }
