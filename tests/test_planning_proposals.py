import pytest
from test_planning_review import Model, system as review_system

from sdd.generic import schema
from sdd.generic.planner import Planner
from sdd.generic.planning_review import PlanReviewRequired, PlanReviews
from sdd.generic.sql import SQLService

system = review_system


class ProposalModel(Model):
    def __init__(self, adjustments):
        super().__init__(uncertain=False)
        self.adjustments = adjustments

    def ask(self, tenant, state, questions):
        result = super().ask(tenant, state, questions)
        for key, value in self.adjustments.items():
            if key not in questions:
                continue
            answer = result["answers"][key]
            if questions[key]["type"] == "noul":
                answer["noul"] = value
            else:
                probabilities = value if isinstance(value, dict) else {value: 1.0}
                answer["choice"] = max(probabilities, key=probabilities.get)
                answer["probabilities"] = {
                    option: probabilities.get(option, 0.0) for option in questions[key]["criteria"]
                }
        return result


def proposed(system, adjustments, request="Count readings", datasets=None):
    db, _, _, _, _ = system
    model = ProposalModel(adjustments)
    reviews = PlanReviews(db, Planner(db, model))
    with pytest.raises(PlanReviewRequired) as caught:
        reviews.begin("a", "owner", request, datasets or ["readings"])
    return caught.value.plan, reviews, model


def test_uncertain_boolean_keeps_candidate_and_raw_probability_until_confirmation(system):
    db, catalog, _, _, _ = system
    plan, reviews, model = proposed(system, {"count_rows": 0.67})
    decision = next(d for d in plan["review"]["decisions"] if d["key"] == "count_rows")
    assert decision["selected"] is True and decision["probability"] == 0.67
    assert plan["logical_sql"] and "COUNT(*)" in plan["logical_sql"]
    assert plan["proposal"]["status"] == "candidate"
    assert plan["review"]["requires_confirmation"] and plan["review"]["can_confirm_sql"]
    assert any(i["decision_id"] == decision["id"] for i in plan["review"]["unresolved"])
    stored = catalog.ledger.get("a", schema.runs, plan["review_id"])
    assert stored["manifest"]["executed"] is False and stored["result"] == []
    calls = model.calls
    confirmed = reviews.confirm("a", "owner", plan["review_id"])
    assert model.calls == calls
    assert (
        next(d for d in confirmed["review"]["decisions"] if d["key"] == "count_rows")["probability"]
        == 0.67
    )
    assert SQLService(db).execute("a", confirmed["logical_sql"])["result"] == [{"metric_1": 3}]


def test_correction_is_separate_from_original_model_distribution(system):
    db, _, _, model, reviews = system
    with pytest.raises(PlanReviewRequired) as caught:
        reviews.begin("a", "owner", "Count readings with amount above 10", ["readings"])
    plan = caught.value.plan
    decision = next(d for d in plan["review"]["decisions"] if d["key"] == "filter_c1")
    corrected = reviews.resume("a", "owner", plan["review_id"], {decision["id"]: "none"})
    changed = next(d for d in corrected["review"]["decisions"] if d["key"] == "filter_c1")
    assert changed["selected"] == "none" and changed["overridden"]
    assert changed["probability"] == decision["probability"] == 0.5
    assert changed["options"] == decision["options"]
    assert changed["model_selected"] == decision["model_selected"]
    answer = next(
        batch["answers"]["filter_c1"]
        for batch in corrected["decision_trace"]
        if "filter_c1" in batch["answers"]
    )
    assert answer["_selection"] == "none"
    assert answer["probabilities"]["none"] == 0.5
    assert SQLService(db).execute("a", corrected["logical_sql"])["result"] == [{"metric_1": 3}]


def test_uncertain_operation_and_root_produce_held_read_candidate(system):
    plan, _, _ = proposed(
        system, {"action": {"select": 0.51, "update": 0.49}, "root": {"t0": 0.53, "none": 0.47}}
    )
    assert plan["operation"] == "select" and plan["logical_sql"].startswith("SELECT")
    assert plan["review"]["requires_confirmation"]
    assert {d["key"] for d in plan["review"]["decisions"] if d["uncertain"]} == {"action", "root"}


def test_unsupported_root_exposes_pipeline_without_inventing_sql(system):
    plan, reviews, _ = proposed(system, {"root": {"none": 0.6, "t0": 0.4}})
    assert plan["logical_sql"] == "" and plan["proposal"]["status"] == "unresolved"
    assert plan["pipeline"]["stage"] == "operation"
    assert plan["review"]["decisions"] and not plan["review"]["can_confirm_sql"]
    assert plan["review"]["unresolved"][0]["code"] == "unsupported_interpretation"
    with pytest.raises(ValueError, match="incomplete"):
        reviews.confirm("a", "owner", plan["review_id"])


def test_empty_output_becomes_bounded_explicit_partial_read(system):
    db, _, _, _, _ = system
    plan, reviews, _ = proposed(system, {"count_rows": 0.01}, "Help me understand these readings")
    assert plan["proposal"]["status"] == "partial"
    assert "LIMIT 100" in plan["logical_sql"]
    assert any(i["code"] == "output_unresolved" for i in plan["review"]["unresolved"])
    assert plan["review"]["requires_confirmation"] and plan["review"]["can_confirm_sql"]
    confirmed = reviews.confirm("a", "owner", plan["review_id"])
    assert len(SQLService(db).execute("a", confirmed["logical_sql"])["result"]) == 3


def test_grouping_conflict_retains_coherent_partial_aggregate(system):
    db, _, _, _, _ = system
    plan, _, _ = proposed(system, {"count_rows": 0.01, "show_c0": 0.95, "metric_c1_sum": 0.95})
    assert plan["proposal"]["status"] == "partial"
    assert any(i["code"] == "grouping_conflict" for i in plan["review"]["unresolved"])
    assert SQLService(db).execute("a", plan["logical_sql"])["result"] == [{"metric_1": 55}]


def test_missing_mutation_assignment_has_no_read_or_write_fallback(system):
    db, _, _, _, _ = system
    plan, reviews, _ = proposed(
        system, {"action": {"update": 0.51, "select": 0.49}}, "Change these readings"
    )
    assert plan["logical_sql"] == "" and plan["proposal"]["status"] == "unresolved"
    assert not plan["review"]["can_confirm_sql"]
    with pytest.raises(ValueError, match="incomplete"):
        reviews.confirm("a", "owner", plan["review_id"])
    assert SQLService(db).execute("a", "SELECT SUM(amount) AS total FROM readings")["result"] == [
        {"total": 55}
    ]


def test_literal_conflict_explains_omitted_predicate_and_never_releases_draft(system):
    db, catalog, _, _, _ = system
    catalog.create("a", "notes", [{"id": 1, "body": "review"}], primary_key=["id"])
    plan, _, _ = proposed(
        system,
        {"predicate_kind_c1": "literal", "filter_c1": "none"},
        "Count notes containing review",
        ["notes"],
    )
    assert plan["proposal"]["status"] == "partial"
    assert any(i["code"] == "predicate_conflict" for i in plan["review"]["unresolved"])
    assert plan["review"]["requires_confirmation"]
    assert SQLService(db).execute("a", plan["logical_sql"])["result"] == [{"metric_1": 1}]


def test_incompatible_calculation_keeps_partial_read_with_original_decisions(system):
    db, catalog, _, _, _ = system
    catalog.create(
        "a", "values", [{"id": 1, "left_value": 2, "right_value": 3}], primary_key=["id"]
    )
    plan, _, _ = proposed(
        system,
        {"count_rows": 0.01, "calculation": "e1", "calc_aggregate": "ratio_of_sums"},
        "Compare the combined values",
        ["values"],
    )
    assert plan["proposal"]["status"] == "partial"
    assert plan["review"]["requires_confirmation"] and plan["review"]["can_confirm_sql"]
    assert "LIMIT 100" in plan["logical_sql"]
    assert any(item["code"] == "arithmetic_incomplete" for item in plan["review"]["unresolved"])
    choice = next(d for d in plan["review"]["decisions"] if d["key"] == "calc_aggregate")
    assert choice["selected"] == "ratio_of_sums" and choice["probability"] == 1.0
    assert not choice["overridden"]
    assert len(SQLService(db).execute("a", plan["logical_sql"])["result"]) == 1


def test_uncertain_mutation_confirmation_still_requires_preview_and_commit(system):
    db, _, _, _, _ = system
    plan, reviews, _ = proposed(
        system,
        {"action": {"delete": 0.51, "select": 0.49}},
        "Remove these readings",
    )
    assert plan["operation"] == "delete" and plan["proposal"]["status"] == "candidate"
    assert plan["review"]["requires_confirmation"] and plan["review"]["can_confirm_sql"]
    confirmed = reviews.confirm("a", "owner", plan["review_id"])
    sql = SQLService(db)
    with pytest.raises(ValueError, match="full-table mutation"):
        sql.execute("a", confirmed["logical_sql"], actor="owner")
    preview = sql.execute("a", confirmed["logical_sql"], actor="owner", allow_all=True)
    assert preview["mutation_preview"]
    assert sql.execute("a", "SELECT COUNT(*) AS n FROM readings")["result"] == [{"n": 3}]
    sql.commit("a", preview["preview_token"], "owner")
    assert sql.execute("a", "SELECT COUNT(*) AS n FROM readings")["result"] == [{"n": 0}]
