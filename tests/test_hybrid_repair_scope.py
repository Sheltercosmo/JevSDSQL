from copy import deepcopy

from test_hybrid_planner import LLM, Reviewer, system as system, plan_or_hold
from sdd.generic.planner import Planner


def test_output_feedback_cannot_change_population(system):
    class MissingOutput(Reviewer):
        def ask(self, tenant, state, questions):
            result = super().ask(tenant, state, questions)
            for key in questions:
                if key == "check_c0_output1":
                    result["answers"][key]["noul"] = 0.05
            return result

    class ChangesPopulation(LLM):
        def generate(self, prompt, schema):
            if self.calls:
                self.draft["candidates"][0]["sql"] = "SELECT id FROM readings"
            return super().generate(prompt, schema)

    db, _ = system
    original = "SELECT id, amount FROM readings WHERE amount > 5"
    llm = ChangesPopulation([original])
    plan = plan_or_hold(Planner(db, MissingOutput(), "hybrid", llm), "IDs with an amount over five")
    assert plan["logical_sql"] == original
    assert not plan["hybrid"]["repair"]["used"]
    assert llm.calls == 1  # A lone probability does not authorize an unrelated rewrite.


def test_projection_scope_preserves_cte_calculations():
    from sdd.generic.hybrid_feedback import preserves_repair_scope

    original = "WITH v AS (SELECT id, amount/100.0 AS n FROM readings) SELECT id,n FROM v"
    assert preserves_repair_scope(
        original, "WITH v AS (SELECT id, amount/100.0 AS n FROM readings) SELECT id FROM v"
    )
    assert not preserves_repair_scope(
        original, "WITH v AS (SELECT id, amount/10.0 AS n FROM readings) SELECT id FROM v"
    )


def test_changed_context_can_repair_despite_saved_draft(system):
    from test_hybrid_feedback import RepairLLM

    db, catalog = system
    llm = RepairLLM(["SELECT AGE(CURRENT_DATE) FROM readings"])
    planner = Planner(db, Reviewer(), "hybrid", llm)
    initial = plan_or_hold(planner, "Total amount")
    previous = deepcopy(initial)
    previous["_hybrid_draft"]["signature"] = "different-context"
    llm.calls = 0
    llm.draft["candidates"][0]["sql"] = "SELECT AGE(CURRENT_DATE) FROM readings"
    refreshed = plan_or_hold(planner, "Total amount", previous=previous)
    assert llm.calls == 2
    assert refreshed["hybrid"]["repair"]["used"]
