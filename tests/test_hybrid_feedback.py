"""Small reproductions of executable-candidate and missing-evidence failures."""

from copy import deepcopy

from test_hybrid_planner import LLM, Reviewer, system as system, plan_or_hold
from sdd.generic.planner import Planner
from sdd.generic.sql import SQLService


def test_backend_probe_prevents_selecting_unexecutable_alternative(system):
    db, _ = system
    good = "SELECT COUNT(*) AS total FROM readings"
    bad = "SELECT r.id FROM readings r JOIN LATERAL (SELECT r.amount) x ON TRUE"
    plan = plan_or_hold(Planner(db, Reviewer("c1"), "hybrid", LLM([good, bad])))
    assert plan["logical_sql"] == good
    assert not plan["hybrid"]["candidates"][1]["valid"]
    assert SQLService(db).execute("a", plan["logical_sql"])["result"] == [{"total": 2}]


def test_generator_receives_backend_contract_and_actual_values(system):
    db, catalog = system
    catalog.create(
        "a",
        "statuses",
        [{"code": "R", "description": "Ready"}, {"code": "P", "description": "Pending"}],
    )
    llm = LLM(["SELECT COUNT(*) FROM statuses WHERE code='R'"])
    try:
        plan = Planner(db, Reviewer(), "hybrid", llm).plan("a", "Count ready records", ["statuses"])
    except Exception as exc:
        plan = exc.plan
    assert "execution_backend" in llm.prompt
    assert "value_evidence" in llm.prompt
    assert "Ready" in llm.prompt and "Pending" in llm.prompt
    assert plan["logical_sql"]


class RepairLLM(LLM):
    def generate(self, prompt, schema):
        if self.calls:
            self.draft["candidates"] = [
                {
                    "label": "Executable calculation",
                    "sql": "SELECT SUM(amount) AS total FROM readings",
                    "assumptions": [],
                }
            ]
        return super().generate(prompt, schema)


def test_one_repair_uses_guard_feedback_and_then_reuses_draft(system):
    db, _ = system
    llm = RepairLLM(["SELECT AGE(CURRENT_DATE) FROM readings"])
    planner = Planner(db, Reviewer(), "hybrid", llm)
    plan = plan_or_hold(planner, "Total amount")
    assert llm.calls == 2
    assert "AGE" in llm.prompt and "feedback" in llm.prompt
    assert SQLService(db).execute("a", plan["logical_sql"])["result"] == [{"total": 12}]
    assert plan["hybrid"]["repair"]["used"]
    previous = deepcopy(plan)
    plan_or_hold(planner, "Total amount", previous=previous)
    assert llm.calls == 2


def test_repair_has_a_hard_one_call_limit(system):
    db, _ = system
    llm = LLM(["SELECT AGE(CURRENT_DATE) FROM readings"])
    plan = plan_or_hold(Planner(db, Reviewer(), "hybrid", llm))
    assert llm.calls == 2
    assert not plan["logical_sql"]
    assert plan["hybrid"]["candidates"]


class OutputReviewer(Reviewer):
    def ask(self, tenant, state, questions):
        result = super().ask(tenant, state, questions)
        projections = {c["id"]: c.get("projections", []) for c in state.get("candidates", [])}
        if "check_c0_issue" in questions and "SUM(" not in state.get("candidate_sql", "").upper():
            result["answers"]["check_c0_issue"].update(
                choice="output",
                probabilities={
                    k: float(k == "output") for k in questions["check_c0_issue"]["criteria"]
                },
            )
            result["answers"]["check_c0_outputs"]["noul"] = 0.05
        for key, answer in result["answers"].items():
            if (
                answer["type"] == "noul"
                and "_output" in key
                and key.rsplit("output", 1)[1].isdigit()
            ):
                identity = key.split("_")[1]
                index = int(key.rsplit("output", 1)[1])
                if "SUM(" not in projections[identity][index].upper():
                    answer["noul"] = 0.05
        return result


def test_output_only_feedback_repairs_extra_identifiers(system):
    db, _ = system
    llm = RepairLLM(["SELECT id, amount FROM readings"])
    plan = plan_or_hold(
        Planner(db, OutputReviewer(), "hybrid", llm), "Return only the total amount"
    )
    assert llm.calls == 2
    assert plan["hybrid"]["repair"]["used"]
    assert SQLService(db).execute("a", plan["logical_sql"])["result"] == [{"total": 12}]


def test_failed_repair_preserves_the_original_inspectable_solution(system):
    from sdd.evaluators import ProviderError

    class FailingRepair(LLM):
        def generate(self, prompt, schema):
            if self.calls:
                self.calls += 1
                raise ProviderError("RepairUnavailable", False)
            return super().generate(prompt, schema)

    db, _ = system
    llm = FailingRepair(["SELECT id, amount FROM readings"])
    plan = plan_or_hold(
        Planner(db, OutputReviewer(), "hybrid", llm), "Return only the total amount"
    )
    assert plan["logical_sql"] == "SELECT id, amount FROM readings"
    assert plan["hybrid"]["repair"]["output_state"] == "NOT_EVALUATED"
    assert plan["review"]["can_confirm_sql"]
