import pytest

from sdd.db import Database
from sdd.evaluators import ProviderError
from sdd.generic.catalog import Catalog
from sdd.generic.planner import Planner
from sdd.generic.planning_review import PlanReviewRequired
from sdd.generic.sql import SQLService


class CountDecisions:
    model = "composition-fixture"

    def __init__(self, fail_after_intent=False):
        self.calls = 0
        self.fail_after_intent = fail_after_intent

    def ask(self, tenant, state, questions):
        self.calls += 1
        if self.calls > 1 and self.fail_after_intent:
            raise ProviderError("ReadTimeout", True)
        answers = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.99}
                continue
            options = question["criteria"]
            chosen = {
                "action": "read",
                "family": "aggregate",
                "root": "t0",
                "aggregation": "count",
                "feasibility": "supported",
                "output_0": "stat",
                "distinct": "no",
            }.get(key, "none" if "none" in options else next(iter(options)))
            answers[key] = {
                "type": "choice",
                "choice": chosen,
                "probabilities": {option: float(option == chosen) for option in options},
            }
        return {"model": self.model, "answers": answers, "usage": {}}


@pytest.fixture
def catalog():
    db = Database("sqlite://")
    db.initialize()
    catalog = Catalog(db)
    catalog.create(
        "a", "items", [{"id": 1, "amount": 2}, {"id": 2, "amount": 8}], primary_key=["id"]
    )
    yield catalog
    db.engine.dispose()


def test_complete_compositional_read_executes_without_confirmation_when_certain(catalog):
    model = CountDecisions()
    plan = Planner(catalog.db, model, strategy="compositional").plan("a", "How many items?")
    assert not plan["review"]["requires_confirmation"]
    assert plan["search"]["validated"] > 0
    assert SQLService(catalog.db).execute("a", plan["logical_sql"])["result"] == [{"result_1": 2}]


def test_provider_failure_after_read_intent_retains_bounded_inspectable_preview(catalog):
    model = CountDecisions(fail_after_intent=True)
    with pytest.raises(PlanReviewRequired) as caught:
        Planner(catalog.db, model, strategy="compositional").plan("a", "How many items?")
    plan = caught.value.plan
    assert plan["proposal"]["status"] == "partial"
    assert "LIMIT 100" in plan["logical_sql"]
    assert any("ReadTimeout" in item["detail"] for item in plan["review"]["unresolved"])
    assert plan["review"]["requires_confirmation"]


def test_provider_failure_before_read_intent_does_not_invent_operation(catalog):
    class Failed(CountDecisions):
        def ask(self, *args):
            raise ProviderError("ReadTimeout", True)

    with pytest.raises(ProviderError):
        Planner(catalog.db, Failed(), strategy="compositional").plan("a", "Change these items")


def test_extended_objectives_keep_the_existing_compiler(catalog):
    from sdd.generic.compositional import CompositionalPlanner
    from sdd.generic.planning_review import ReviewDecisions

    class Extended(CountDecisions):
        def ask(self, tenant, state, questions):
            result = super().ask(tenant, state, questions)
            result["answers"]["family"] = {
                "type": "choice",
                "choice": "extended",
                "probabilities": {
                    k: float(k == "extended") for k in questions["family"]["criteria"]
                },
            }
            return result

    planner = CompositionalPlanner(catalog, ReviewDecisions(Extended()))
    marker = object()
    assert (
        planner.plan("a", "Mixed conditions", catalog.model_catalog("a"), lambda: marker) is marker
    )
