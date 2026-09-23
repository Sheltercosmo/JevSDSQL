from types import SimpleNamespace

import pytest

from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.parallel_planner import ParallelPlanner
from sdd.generic.planner import Planner
from sdd.generic.relational import Field, Link, Program
from sdd.generic.relationship_roles import bind_roles
from sdd.generic.sql import SQLService


class CountModel:
    model = "count-contract-fixture"

    def ask(self, tenant, state, questions):
        answers = {}
        choices = {
            "action": "read",
            "family": "aggregate",
            "root": "t0",
            "aggregation": "count",
            "feasibility": "supported",
            "output_0": "stat",
            "contract_structure": "aggregate",
            "contract_calculation": "ordinary",
            "contract_grain": "scalar",
            "contract_stat_output": "yes",
            "answer_statistic": "yes",
        }
        for key, question in questions.items():
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.99}
                continue
            options = question["criteria"]
            selected = choices.get(
                key,
                "none" if "none" in options else "no" if "no" in options else next(iter(options)),
            )
            answers[key] = {
                "type": "choice",
                "choice": selected,
                "probabilities": {k: float(k == selected) for k in options},
            }
        return {"model": self.model, "answers": answers, "usage": {}}


def test_later_reconciliation_does_not_replace_output_batch_probabilities():
    db = Database("sqlite://")
    db.initialize()
    Catalog(db).create(
        "t", "items", [{"id": 1, "value": 2}, {"id": 2, "value": 9}], primary_key=["id"]
    )
    plan = Planner(db, CountModel(), "parallel").plan("t", "How many items?")
    assert SQLService(db).execute("t", plan["logical_sql"])["result"] == [{"result_1": 2}]
    decisions = plan["review"]["decisions"]
    assert next(d for d in decisions if d["key"] == "output_0")["selected"] == "stat"
    assert (
        next(d for d in decisions if d["key"] == "answer_statistic")["operation_state"]
        == "COMPLETED"
    )
    db.engine.dispose()


def test_candidate_repair_cannot_drop_a_resolved_filter():
    planner = ParallelPlanner(None, SimpleNamespace())
    planner.required_filters = [("f0", "gt", 4)]
    candidate = Program(
        "items", {"f0": Field("f0", "items", "amount", "number")}, [], outputs=["f0"]
    )
    accepted, rejected = planner.validate("t", [("Drop condition", candidate)])
    assert not accepted
    assert "obligations" in rejected[0]["error"]


def test_grouped_order_rejects_sqlite_arbitrary_member_values():
    planner = ParallelPlanner(None, SimpleNamespace())
    fields = {
        "f0": Field("f0", "items", "kind", "text"),
        "f1": Field("f1", "items", "amount", "number"),
    }
    candidate = Program(
        "items",
        fields,
        [],
        aggregation="count",
        outputs=["f0"],
        groups=["f0"],
        order=[("f1", True)],
    )
    accepted, rejected = planner.validate("t", [("Invalid grouping", candidate)])
    assert not accepted
    assert "ungrouped" in rejected[0]["error"]


def test_parallel_lookup_roles_do_not_collapse_composite_keys():
    links = [
        Link("events", "places", "origin", "id", constraint_id="origin"),
        Link("events", "places", "destination", "id", constraint_id="destination"),
        Link("events", "places", "region", "region", constraint_id="destination"),
    ]

    def ask(tenant, state, questions):
        assert "destination" in questions["relationship_role_0"]["criteria"]["1"]
        return {"relationship_role_0": "1"}

    assert bind_roles(ask, "t", "destination places", links, []) == links[1:]


def test_skipped_phase_is_not_false_and_provider_failure_is_unknown():
    decisions = SimpleNamespace(decisions=[], ask_many=lambda tenant, jobs: [])
    planner = ParallelPlanner(None, decisions)
    assert planner.evaluate("t", [], "cohort_predicates") == []
    assert planner.graph["rounds"][-1]["output_state"] == "NOT_EVALUATED"

    def fail(*args):
        raise RuntimeError("DailyBudgetExceeded")

    decisions.ask_many = fail
    with pytest.raises(RuntimeError):
        planner.evaluate("t", [({}, {"x": {}})], "cohort_predicates")
    record = planner.graph["rounds"][-1]
    assert record["output_state"] == "UNKNOWN"
    assert record["operation_state"] == "BLOCKED_BY_BUDGET"
