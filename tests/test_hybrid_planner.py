from copy import deepcopy

import pytest

from sdd.db import Database
from sdd.evaluators import ProviderError
from sdd.generic.catalog import Catalog
from sdd.generic.planner import Planner
from sdd.generic.planning_review import PlanReviewRequired
from sdd.generic.sql import SQLService


class LLM:
    def __init__(self, queries):
        self.calls = 0
        self.draft = {
            "objective": "Count readings",
            "result_grain": "one total",
            "outputs": ["total"],
            "steps": [
                {
                    "name": "count",
                    "depends_on": [],
                    "purpose": "Count eligible rows",
                    "operator": "aggregate",
                    "grain": "one total",
                    "expressions": ["COUNT(*)"],
                }
            ],
            "candidates": [
                {"label": f"Interpretation {i}", "sql": q, "assumptions": []}
                for i, q in enumerate(queries)
            ],
            "data_constraints": [
                {
                    "source": "readings",
                    "columns": ["id"],
                    "row_scope": "All readings",
                    "grain": "one row per reading",
                    "keys_and_relationships": "id is unique",
                    "units_and_nulls": "Count rows",
                    "purpose": "Count the population",
                }
            ],
            "preferred_index": 0,
        }

    def generate(self, prompt, schema):
        self.calls += 1
        self.prompt = prompt
        return deepcopy(self.draft), {"calls": 1, "model": "test", "generation_ms": 1}


class Reviewer:
    model = "test"

    def __init__(self, winner="c0", probability=0.99, fail=False):
        self.winner, self.probability, self.fail = winner, probability, fail
        self.calls = 0

    def ask(self, tenant, state, questions):
        self.calls += 1
        if self.fail:
            raise ProviderError("TestUnavailable", False)
        answers = {}
        for key, q in questions.items():
            if q["type"] == "noul":
                answers[key] = {"type": "noul", "noul": self.probability}
            else:
                answers[key] = {
                    "type": "choice",
                    "choice": self.winner
                    if self.winner in q["criteria"]
                    else next(iter(q["criteria"])),
                    "probabilities": {
                        k: float(
                            k
                            == (
                                self.winner
                                if self.winner in q["criteria"]
                                else next(iter(q["criteria"]))
                            )
                        )
                        for k in q["criteria"]
                    },
                }
        return {"model": self.model, "answers": answers, "usage": {}}


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("SDD_HYBRID_CONCEPTS", "off")
    db = Database("sqlite:///" + str(tmp_path / "hybrid.db"))
    db.initialize()
    catalog = Catalog(db)
    catalog.create(
        "a", "readings", [{"id": 1, "amount": 4}, {"id": 2, "amount": 8}], primary_key=["id"]
    )
    catalog.create("a", "private_scope", [{"id": 1}], primary_key=["id"])
    catalog.create("b", "other_tenant", [{"id": 1}], primary_key=["id"])
    yield db, catalog
    db.engine.dispose()


def plan_or_hold(planner, question="Count readings", **kwargs):
    try:
        return planner.plan("a", question, ["readings"], **kwargs)
    except PlanReviewRequired as exc:
        return exc.plan


def test_parallel_review_routes_whole_candidates_and_correction_reuses_generation(system):
    db, _ = system
    llm = LLM(
        ["SELECT SUM(amount) AS total FROM readings", "SELECT COUNT(*) AS total FROM readings"]
    )
    reviewer = Reviewer("c1")
    planner = Planner(db, reviewer, "hybrid", llm)
    plan = plan_or_hold(planner)
    assert plan["hybrid"]["selected"] == "c1"
    assert llm.calls == 1 and reviewer.calls == 6
    assert SQLService(db).execute("a", plan["logical_sql"])["result"] == [{"total": 2}]
    decision = next(d for d in plan["review"]["decisions"] if d["key"] == "hybrid_candidate")
    changed = plan_or_hold(planner, previous=plan, corrections={decision["id"]: "c0"})
    assert changed["hybrid"]["selected"] == "c0"
    assert llm.calls == 1 and reviewer.calls == 6
    assert changed["hybrid"]["generation"]["calls"] == 0


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM private_scope",
        "SELECT * FROM other_tenant",
        "DROP TABLE readings",
        "SELECT missing FROM readings",
        "SELECT 1; DELETE FROM readings",
    ],
)
def test_invalid_or_out_of_scope_candidates_are_never_selected(system, query):
    db, _ = system
    plan = plan_or_hold(
        Planner(db, Reviewer("c0"), "hybrid", LLM([query, "SELECT COUNT(*) FROM readings"]))
    )
    assert not plan["hybrid"]["candidates"][0]["valid"]
    assert plan["hybrid"]["selected"] == "c1"


def test_uncertain_checks_keep_executable_proposal(system):
    db, _ = system
    plan = plan_or_hold(
        Planner(db, Reviewer(probability=0.5), "hybrid", LLM(["SELECT COUNT(*) FROM readings"]))
    )
    assert plan["logical_sql"] and plan["review"]["can_confirm_sql"]
    assert plan["review"]["requires_confirmation"]
    assert all(
        d["output_state"] == "UNKNOWN" for d in plan["review"]["decisions"] if d["kind"] == "noul"
    )


def test_failed_review_is_not_a_false_answer(system):
    db, _ = system
    plan = plan_or_hold(
        Planner(db, Reviewer(fail=True), "hybrid", LLM(["SELECT COUNT(*) FROM readings"]))
    )
    assert plan["logical_sql"]
    assert plan["hybrid"]["review_state"] == {
        "output": "NOT_EVALUATED",
        "operation": "FAILED",
        "code": "TestUnavailable",
    }
    assert plan["review"]["requires_confirmation"]


def test_mutation_planning_never_changes_rows(system):
    db, _ = system
    planner = Planner(db, Reviewer(), "hybrid", LLM(["UPDATE readings SET amount=20 WHERE id=1"]))
    plan = plan_or_hold(planner, "将编号为1的读数改成20")
    assert plan["operation"] == "update"
    assert SQLService(db).execute("a", "SELECT amount FROM readings WHERE id=1")["result"] == [
        {"amount": 4}
    ]
    preview = SQLService(db).execute("a", plan["logical_sql"], actor="owner")
    assert preview["mutation_preview"]


def test_context_budget_is_explicit_and_does_not_call_llm(system):
    db, _ = system
    llm = LLM(["SELECT COUNT(*) FROM readings"])
    plan = plan_or_hold(
        Planner(db, Reviewer(), "hybrid", llm),
        knowledge=[{"id": str(i), "name": str(i), "definition": "x" * 10000} for i in range(14)],
    )
    assert llm.calls == 0 and not plan["logical_sql"]
    assert plan["hybrid"]["generation_state"]["operation"] == "BLOCKED_BY_BUDGET"
