import sqlite3

import pytest

from sdd.generic.hybrid_contract import sql_facts
from sdd.generic.hybrid_operations import project_without, local_alternatives
from test_hybrid_planner import LLM, Reviewer, system as system, plan_or_hold
from sdd.generic.planner import Planner


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT DISTINCT id, amount FROM readings",
        "SELECT id, amount FROM readings ORDER BY 2",
        "SELECT 'total', SUM(amount) FROM readings",
    ],
)
def test_projection_repair_preserves_distinct_ordinals_and_aggregate_grain(sql):
    assert project_without(sql, [1]) is None


def test_projection_repair_preserves_alias_order_and_source_rows():
    sql = "SELECT id, amount * 2 AS ranked FROM readings ORDER BY ranked DESC"
    revised = project_without(sql, [1])
    with sqlite3.connect(":memory:") as db:
        db.executescript(
            "CREATE TABLE readings(id INT, amount INT); INSERT INTO readings VALUES (1,4),(2,8),(3,8),(4,NULL);"
        )
        assert db.execute(revised).fetchall() == [(2,), (3,), (1,), (4,)]
    assert project_without(sql, [0, 1]) is None


def test_parallel_review_can_select_local_projection_without_another_llm(system):
    class OutputReviewer(Reviewer):
        def ask(self, tenant, state, questions):
            result = super().ask(tenant, state, questions)
            for key, answer in result["answers"].items():
                if key in ("check_c0_outputs", "check_c0_output1"):
                    answer["noul"] = 0.01
                elif key == "check_c0_issue":
                    answer.update(
                        choice="output",
                        probabilities={k: float(k == "output") for k in questions[key]["criteria"]},
                    )
            return result

    db, _ = system
    llm = LLM(["SELECT id, amount FROM readings ORDER BY amount DESC"])
    plan = plan_or_hold(
        Planner(db, OutputReviewer("pc0"), "hybrid", llm),
        "List only the record IDs by decreasing amount",
    )
    assert llm.calls == 1
    assert plan["hybrid"]["selected"] == "pc0"
    assert plan["hybrid"]["local_alternatives"] == 1
    assert len(plan["hybrid"]["candidates"]) == 2
    assert len(plan["hybrid"]["review_waves"]) == 2
    assert plan["hybrid"]["repair"]["attempted"] is False


def test_set_operations_and_correlated_subqueries_have_explicit_dependencies():
    packet = {"request": "Counts", "catalog": []}
    facts = sql_facts("SELECT id FROM a UNION ALL SELECT id FROM b", packet)
    union = next(s for s in facts["steps"] if s.operator == "set_operation")
    assert union.expressions == ["UNION ALL"] and len(union.depends_on) == 2
    facts = sql_facts("SELECT id FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.id=a.id)", packet)
    assert facts["output_state"] == "VALUE"
    final_filter = [s for s in facts["steps"] if s.operator == "filter"][-1]
    assert len(final_filter.depends_on) == 2


def test_local_join_repair_retains_original_and_does_not_edit_predicate():
    original = "SELECT a.id FROM a JOIN b ON a.id=b.id WHERE a.id > 1"
    candidates = [{"id": "c0", "operation": "select", "sql": original}]
    alternatives = local_alternatives(
        {"request": "Show all accounts"},
        candidates,
        {"checks": {"check_preserve_c0_j0": 0.99}, "selected": "c0"},
    )
    assert len(alternatives) == 1 and "LEFT JOIN" in alternatives[0]["sql"]
    assert "WHERE a.id > 1" in alternatives[0]["sql"]
    assert candidates[0]["sql"] == original


def test_focused_output_role_can_propose_alternative_without_global_rejection():
    candidate = {
        "id": "c0",
        "operation": "select",
        "sql": "SELECT id, amount FROM readings ORDER BY amount DESC",
        "projections": ["id", "amount"],
    }
    review = {
        "selected": "c0",
        "checks": {"check_c0_output1": 0.3},
        "output_roles": {
            "check_role_c0_output1": {
                "probabilities": {"support": 0.9, "answer": 0.05, "ambiguous": 0.05}
            }
        },
    }
    assert len(local_alternatives({"request": "Which records?"}, [candidate], review)) == 1
    review["output_roles"]["check_role_c0_output1"]["probabilities"]["support"] = 0.4
    assert local_alternatives({"request": "Which records?"}, [candidate], review) == []


def test_explicit_foreign_identifier_is_not_replaced_by_source_primary_key():
    from sdd.generic.hybrid_operations import identity_alternatives

    packet = {
        "request": "List parent IDs",
        "catalog": [{"name": "events", "primary_key": ["event_id"]}],
    }
    candidate = {"id": "c0", "operation": "select", "sql": "SELECT parent_id FROM events"}
    assert identity_alternatives(packet, [candidate]) == []
