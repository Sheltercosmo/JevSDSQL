import pytest

from sdd.generic.hybrid_context import bridge_tables, filter_context, rule_closure
from sdd.generic.hybrid_operations import validate_steps
from sdd.generic.hybrid import PlanStep
from sdd.generic.planning_review import ReviewDecisions
from test_hybrid_planner import LLM, Reviewer, plan_or_hold, system as hybrid_system
from sdd.generic.planner import Planner
from sdd.generic.sql import SQLService

system = hybrid_system


def test_rule_dependencies_and_schema_bridges_are_preserved():
    records = [
        {
            "id": "a",
            "name": "Activity",
            "definition": "Activity = Visits + 1",
            "dependencies": ["b"],
        },
        {"id": "b", "name": "Visits", "definition": "Visits = count"},
        {"id": "c", "name": "Unrelated", "definition": "Other = x"},
    ]
    assert rule_closure(records, {"a"}) == {"a", "b"}
    tables = [
        {"name": "left", "relationships": [{"target_table": "bridge"}]},
        {"name": "bridge", "relationships": [{"target_table": "right"}]},
        {"name": "right", "relationships": []},
        {"name": "unrelated", "relationships": []},
    ]
    assert bridge_tables(tables, {"left", "right"}) == {"left", "bridge", "right"}


def test_filter_removes_irrelevant_fields_but_keeps_uncertain_evidence_and_keys():
    class Filter(Reviewer):
        def ask(self, tenant, state, questions):
            result = super().ask(tenant, state, questions)
            for field in state.get("fields", []):
                result["answers"]["check_retrieve_" + field["key"]]["noul"] = {
                    "id": 0.01,
                    "amount": 0.6,
                    "noise": 0.01,
                }[field["name"]]
            return result

    packet = {
        "request": "Compare amounts",
        "business_knowledge": [],
        "catalog": [
            {
                "name": "measurements",
                "primary_key": ["id"],
                "relationships": [],
                "columns": [{"name": n, "type": "integer"} for n in ["id", "amount", "noise"]],
            }
        ],
    }
    filtered, trace = filter_context("a", packet, ReviewDecisions(Filter()))
    assert [c["name"] for c in filtered["catalog"][0]["columns"]] == ["id", "amount"]
    assert trace["output_bytes"] < trace["input_bytes"]


def test_operation_dag_accepts_parallel_branches_and_rejects_cycles():
    def step(name, dependencies):
        return PlanStep(
            name=name,
            depends_on=dependencies,
            purpose="test",
            operator="aggregate",
            grain="entity",
            expressions=[],
        )

    validate_steps([step("left", []), step("right", []), step("combine", ["left", "right"])])
    with pytest.raises(ValueError, match="cycle"):
        validate_steps([step("left", ["right"]), step("right", ["left"])])


def test_concept_hypotheses_are_cached_and_do_not_filter_database_rows(system, monkeypatch):
    db, _ = system
    monkeypatch.setenv("SDD_HYBRID_CONCEPTS", "on")

    class ConceptsLLM(LLM):
        def generate(self, prompt, schema):
            if schema.get("title") == "Concepts":
                self.calls += 1
                return {
                    "related_concepts": ["activity"],
                    "row_criteria": ["active records"],
                    "ambiguities": [],
                }, {"calls": 1}
            return super().generate(prompt, schema)

    llm = ConceptsLLM(["SELECT COUNT(*) FROM readings"])
    planner = Planner(db, Reviewer(), "hybrid", llm)
    plan = plan_or_hold(planner)
    assert llm.calls == 2
    assert plan["hybrid"]["llm_calls"] == 2
    assert plan["hybrid"]["row_evidence"]["changes_query_population"] is False
    revised = plan_or_hold(planner, previous=plan)
    assert llm.calls == 2 and revised["hybrid"]["llm_calls"] == 0
    assert SQLService(db).execute("a", "SELECT COUNT(*) AS n FROM readings")["result"] == [{"n": 2}]


def test_review_order_and_context_addition(system):
    db, _ = system

    class FilterThenExpand(Reviewer):
        def __init__(self):
            super().__init__()
            self.keys = []

        def ask(self, tenant, state, questions):
            self.keys.extend(questions)
            result = super().ask(tenant, state, questions)
            for field in state.get("fields", []):
                if field["name"] == "amount":
                    result["answers"]["check_retrieve_" + field["key"]]["noul"] = 0.01
            return result

    reviewer = FilterThenExpand()
    plan = plan_or_hold(Planner(db, reviewer, "hybrid", LLM(["SELECT COUNT(*) FROM readings"])))
    inspection = plan["hybrid"]["plan_inspection"]
    assert inspection["additional_data"]["helpful"][0]["detail"]["name"] == "amount"
    assert "check_c0_operation0" in reviewer.keys
    assert "check_c0_overall" in reviewer.keys
    assert len(plan["hybrid"]["review_waves"]) == 1


def test_jsonb_paths_execute_on_sqlite_and_unsafe_functions_still_fail(system):
    db, catalog = system
    catalog.create(
        "a",
        "documents",
        [{"id": 1, "payload": '{"nested":{"score":2.5},"items":[3,8]}'}],
        primary_key=["id"],
    )
    result = SQLService(db).execute(
        "a",
        "SELECT (payload::jsonb #>> '{nested,score}')::numeric AS n, (payload::jsonb #>> '{items,1}')::numeric AS i FROM documents",
    )
    assert result["result"] == [{"n": 2.5, "i": 8}]
    with pytest.raises(ValueError):
        SQLService(db).prepare("a", "SELECT pg_read_file('/private') FROM documents")


def test_hybrid_api_choice_and_history(system, monkeypatch):
    from fastapi.testclient import TestClient
    from sdd.api import create_app
    from sdd.execution import Executor
    import sdd.generic.api as api

    db, catalog = system
    planner = Planner(db, Reviewer(), llm=LLM(["SELECT COUNT(*) AS total FROM readings"]))
    monkeypatch.setattr(api, "services", lambda executor: (catalog, SQLService(db), planner))
    client = TestClient(
        create_app(
            Executor(db, {}), tokens={"owner": {"tenant": "a", "name": "owner", "role": "reviewer"}}
        )
    )
    headers = {"Authorization": "Bearer owner"}
    response = client.post(
        "/ask",
        headers=headers,
        json={
            "question": "Count readings",
            "planner_mode": "hybrid",
            "execute": False,
            "dataset_ids": ["readings"],
        },
    )
    assert response.status_code == 200, response.text
    history = client.get("/query-history/" + response.json()["history_id"], headers=headers).json()
    assert history["input"]["planner_mode"] == "hybrid"
    assert response.json()["plan"]["hybrid"]["contract"]["data_constraints"]
