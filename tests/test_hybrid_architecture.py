import pytest

from test_hybrid_planner import LLM, Reviewer, system as system, plan_or_hold
from test_parallel_planning import ConcurrentModel
from sdd.evaluators import ProviderError
from sdd.generic.jev import choice
from sdd.generic.planning_review import ReviewDecisions, PlanReviewRequired
from sdd.generic.planner import Planner


def test_identical_parallel_jobs_share_raw_response_without_override_leak():
    model = ConcurrentModel()
    review = ReviewDecisions(model)
    job = ({"delay": 0.02}, {"pick": choice("Choose", {"a": "A", "b": "B"})})
    answers = review.ask_many("tenant-a", [job, job, job])
    assert model.calls == 1 and review.requests == 1
    assert sum(a["usage"].get("input_tokens", 0) for a in answers) == 1
    answers[0]["answers"]["pick"]["_selection"] = "b"
    assert "_selection" not in review.ask("tenant-a", *job)["answers"]["pick"]


def test_correction_cannot_move_to_changed_sql():
    question = {"pick": choice("Choose", {"a": "A", "b": "B"})}
    model = ConcurrentModel()
    first = ReviewDecisions(model)
    first.ask(
        "tenant-a",
        {"delay": 0, "option_sql": {"pick": {"a": "SELECT 1", "b": "SELECT 2"}}},
        question,
    )
    plan = first.attach({"logical_sql": "SELECT 1", "operation": "select"})
    old_id = first.decisions[0]["id"]
    revised = ReviewDecisions(model, plan["_review_state"], {old_id: "b"})
    result = revised.ask(
        "tenant-a",
        {"delay": 0, "option_sql": {"pick": {"a": "SELECT 3", "b": "SELECT 4"}}},
        question,
    )
    assert "_selection" not in result["answers"]["pick"]
    with pytest.raises(PlanReviewRequired) as caught:
        revised.attach({"logical_sql": "SELECT 3", "operation": "select"})
    assert caught.value.plan["review"]["reason"] == "stale_correction"


def test_failed_branch_preserves_completed_parallel_evidence():
    class Partial(ConcurrentModel):
        def ask(self, tenant, state, questions):
            if state.get("fail"):
                raise ProviderError("FixtureFailure", False)
            return super().ask(tenant, state, questions)

    review = ReviewDecisions(Partial())
    question = {"pick": choice("Choose", {"a": "A", "b": "B"})}
    answers = review.ask_many(
        "tenant-a", [({"delay": 0}, question), ({"fail": True}, question)], allow_partial=True
    )
    assert answers[0]["answers"]["pick"]["choice"] == "a"
    assert answers[1]["failure"]["output_state"] == "NOT_EVALUATED"
    assert len(review.decisions) == 1


def test_compact_generation_derives_shared_dag_and_never_repeats_sql_in_prompt(system):
    from sdd.generic.hybrid_contract import materialize

    packet = {"request": "Show the counts", "catalog": [], "business_knowledge": []}
    sql = "WITH base AS (SELECT id, amount FROM readings), a AS (SELECT COUNT(*) n FROM base), b AS (SELECT SUM(amount) s FROM base) SELECT n,s FROM a CROSS JOIN b"
    draft = materialize(
        {"candidates": [{"sql": sql, "label": "Counts", "assumptions": []}], "preferred_index": 0},
        packet,
    )
    scans = [s for s in draft.steps if s.operator == "scan"]
    assert len(scans) == 1
    assert any(sum(s.name in step.depends_on for step in draft.steps) > 1 for s in draft.steps)
    assert draft.data_constraints[0].source == "readings"


def test_unknown_reviews_do_not_trigger_llm_repair(system):
    db, _ = system
    llm = LLM(["SELECT SUM(amount) AS total FROM readings"])
    plan = plan_or_hold(Planner(db, Reviewer(probability=0.4), "hybrid", llm), "Total amount")
    assert llm.calls == 1
    assert plan["logical_sql"]
    assert plan["review"]["requires_confirmation"]
    assert plan["hybrid"]["repair"]["operation_state"] == "SKIPPED"
