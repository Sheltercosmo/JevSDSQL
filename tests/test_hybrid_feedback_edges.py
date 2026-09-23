from test_hybrid_planner import LLM, Reviewer, system as system, plan_or_hold
from test_hybrid_feedback import RepairLLM
from sdd.generic.planner import Planner
from sdd.generic.sql import SQLService
from sdd.generic.hybrid_feedback import repair_feedback


def test_overall_rejection_reaches_repair(system):
    class WholePlanReviewer(Reviewer):
        def ask(self, tenant, state, questions):
            result = super().ask(tenant, state, questions)
            if "check_c0_overall" in questions:
                result["answers"]["check_c0_overall"]["noul"] = (
                    0.95 if "SUM(" in state["candidate_sql"] else 0.1
                )
                if "SUM(" not in state["candidate_sql"]:
                    result["answers"]["check_c0_coverage"]["noul"] = 0.1
                    result["answers"]["check_c0_issue"].update(
                        choice="coverage",
                        probabilities={
                            k: float(k == "coverage")
                            for k in questions["check_c0_issue"]["criteria"]
                        },
                    )
            return result

    db, _ = system
    llm = RepairLLM(["SELECT id FROM readings"])
    plan = plan_or_hold(Planner(db, WholePlanReviewer(), "hybrid", llm), "Total amount")
    assert llm.calls == 2
    assert "coverage" in llm.prompt
    assert SQLService(db).execute("a", plan["logical_sql"])["result"] == [{"total": 12}]


def test_unknown_output_is_feedback_not_a_false_claim():
    feedback = repair_feedback(
        {
            "hybrid": {
                "selected": "c0",
                "candidates": [{"id": "c0", "projections": ["SUM(amount)"]}],
                "checks": {"check_c0_output0": 0.3},
            }
        }
    )
    assert feedback == []  # Uncertain reviews remain inspectable, but do not request generation.


def test_private_review_packet_is_not_exposed(system):
    db, _ = system
    plan = plan_or_hold(Planner(db, Reviewer(), "hybrid", LLM(["SELECT COUNT(*) FROM readings"])))
    assert "_hybrid_review_context" not in plan


def test_large_ordinary_read_uses_database_snapshot(system, monkeypatch):
    db, catalog = system
    from sqlalchemy import insert

    dataset = catalog.get("a", "readings")
    with db.transaction("a") as conn:
        conn.execute(
            insert(catalog.table(dataset, conn)), [{"id": n, "amount": 1} for n in range(3, 50004)]
        )
    service = SQLService(db)

    def forbidden(*args, **kwargs):
        raise AssertionError("Ordinary SQL must not materialize every source row")

    monkeypatch.setattr(service, "snapshots", forbidden)
    answer = service.execute("a", "SELECT SUM(amount) AS total FROM readings")
    assert answer["result"] == [{"total": 50013}]
    assert answer["manifest"]["source_rows"] == 50003
    assert answer["manifest"]["snapshot_mode"] == "sqlite_transaction"
