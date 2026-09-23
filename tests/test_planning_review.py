import pytest
from sqlalchemy import update

from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.planner import Planner, PlanReviewRequired
from sdd.generic.planning_review import PlanReviews
from sdd.generic.sql import SQLService
from sdd.generic import schema


class Model:
    model = "review-test"

    def __init__(self, uncertain=True, audit=True):
        self.uncertain, self.audit = uncertain, audit
        self.calls = 0

    def ask(self, tenant, state, questions):
        self.calls += 1
        answers = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                probability = (
                    0.95 if key == "count_rows" or key == "complete" and self.audit else 0.01
                )
                if key == "complete" and not self.audit:
                    probability = 0.78
                answers[key] = {"type": "noul", "noul": probability}
                continue
            options = question["criteria"]
            chosen = {"action": "select", "root": "t0"}.get(
                key, "none" if "none" in options else next(iter(options))
            )
            probabilities = {option: int(option == chosen) for option in options}
            if key == "filter_c1" and self.uncertain:
                chosen = next(
                    option for option, label in options.items() if label == "greater than 10"
                )
                probabilities = {
                    option: 0.5 if option in (chosen, "none") else 0 for option in options
                }
            answers[key] = {"type": "choice", "choice": chosen, "probabilities": probabilities}
        return {
            "model": self.model,
            "answers": answers,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


@pytest.fixture
def system(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "reviews.db"))
    db.initialize()
    catalog = Catalog(db)
    dataset = catalog.create(
        "a",
        "readings",
        [{"id": 1, "amount": 5}, {"id": 2, "amount": 20}, {"id": 3, "amount": 30}],
        primary_key=["id"],
    )
    model = Model()
    reviews = PlanReviews(db, Planner(db, model))
    return db, catalog, dataset, model, reviews


def begin(system):
    *_, reviews = system
    with pytest.raises(PlanReviewRequired) as caught:
        reviews.begin("a", "owner", "Count readings with amount above 10", ["readings"])
    return caught.value.plan


def test_uncertain_decision_is_visible_and_correction_reuses_independent_answers(system):
    db, _, _, model, reviews = system
    plan = begin(system)
    failed = plan["review"]["failed_decision"]
    decision = next(d for d in plan["review"]["decisions"] if d["id"] == failed)
    assert decision["key"] == "filter_c1" and decision["probability"] == 0.5
    assert decision["options"] and plan["logical_sql"]
    assert plan["review"]["can_confirm_sql"] and plan["review"]["requires_confirmation"]
    assert "_review_state" not in plan
    previous_calls = model.calls
    corrected = reviews.resume("a", "owner", plan["review_id"], {failed: decision["selected"]})
    assert corrected["review"]["can_confirm_sql"]
    assert model.calls == previous_calls  # The same SQL reuses shape and audit answers.
    assert any(d["overridden"] for d in corrected["review"]["decisions"])
    assert SQLService(db).execute("a", corrected["logical_sql"])["result"] == [{"metric_1": 2}]
    assert SQLService(db).execute("a", "SELECT COUNT(*) AS n FROM readings")["result"] == [{"n": 3}]


def test_review_is_actor_tenant_and_option_bound(system):
    *_, reviews = system
    plan = begin(system)
    identity = plan["review_id"]
    decision = plan["review"]["failed_decision"]
    for tenant, actor in [("b", "owner"), ("a", "someone_else")]:
        with pytest.raises(ValueError):
            reviews.resume(tenant, actor, identity, {})
    for corrections in [{"invented": True}, {decision: "DROP TABLE readings"}, {decision: False}]:
        with pytest.raises(ValueError):
            reviews.resume("a", "owner", identity, corrections)
    confirmed = reviews.confirm("a", "owner", identity)
    assert confirmed["human_confirmation"]["actor"] == "owner"


def test_expired_or_schema_changed_review_cannot_resume(system):
    db, _, dataset, _, reviews = system
    plan = begin(system)
    with db.transaction("a") as connection:
        connection.execute(
            update(schema.datasets)
            .where(schema.datasets.c.id == dataset["id"])
            .values(description="Changed interpretation")
        )
    with pytest.raises(ValueError, match="Catalog"):
        reviews.resume("a", "owner", plan["review_id"], {})
    plan = begin(system)
    stored = reviews.load("a", "owner", plan["review_id"])
    stored["_expires_at"] = 0
    with db.transaction("a") as connection:
        connection.execute(
            update(schema.runs).where(schema.runs.c.id == plan["review_id"]).values(plan=stored)
        )
    with pytest.raises(ValueError, match="expired"):
        reviews.confirm("a", "owner", plan["review_id"])


def test_complete_sql_can_be_explicitly_confirmed_without_faking_model_confidence(system):
    db, _, _, model, reviews = system
    model.uncertain, model.audit = False, False
    plan = begin(system)
    assert plan["review"]["reason"] == "audit_uncertain"
    audit = next(d for d in plan["review"]["decisions"] if d["key"] == "complete")
    with pytest.raises(ValueError):
        reviews.resume("a", "owner", plan["review_id"], {audit["id"]: True})
    confirmed = reviews.confirm("a", "owner", plan["review_id"])
    assert confirmed["human_confirmation"]["actor"] == "owner"
    assert (
        next(d for d in confirmed["review"]["decisions"] if d["key"] == "complete")["probability"]
        == 0.78
    )
    assert SQLService(db).execute("a", confirmed["logical_sql"])["result"] == [{"metric_1": 3}]


def test_unused_selected_dataset_does_not_make_review_stale(system):
    _, catalog, _, _, reviews = system
    catalog.create("a", "unrelated", [{"key": 1}])
    with pytest.raises(PlanReviewRequired) as caught:
        reviews.begin("a", "owner", "Count readings with amount above 10")
    plan = caught.value.plan
    failed = plan["review"]["failed_decision"]
    decision = next(d for d in plan["review"]["decisions"] if d["id"] == failed)
    corrected = reviews.resume("a", "owner", plan["review_id"], {failed: decision["selected"]})
    assert reviews.load("a", "owner", corrected["review_id"])


def test_upstream_correction_cannot_reuse_an_old_dependent_choice(system):
    _, catalog, _, _, reviews = system
    catalog.create("a", "second", [{"id": 1, "amount": 50}], primary_key=["id"])
    with pytest.raises(PlanReviewRequired) as caught:
        reviews.begin("a", "owner", "Count readings with amount above 10")
    plan = caught.value.plan
    root = next(d for d in plan["review"]["decisions"] if d["key"] == "root")
    failed = next(
        d for d in plan["review"]["decisions"] if d["id"] == plan["review"]["failed_decision"]
    )
    other = next(o["id"] for o in root["options"] if o["label"].startswith("second —"))
    with pytest.raises(PlanReviewRequired) as changed:
        reviews.resume(
            "a", "owner", plan["review_id"], {root["id"]: other, failed["id"]: failed["selected"]}
        )
    assert changed.value.plan["review"]["reason"] == "stale_correction"
    assert not changed.value.plan["review"]["can_confirm_sql"]
