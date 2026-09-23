from types import SimpleNamespace

import pytest

from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.planner import Planner
from sdd.generic.planning_review import PlanReviewRequired
from sdd.generic.relational import Field
from sdd.generic.sql import SQLService
from sdd.generic.stage_dag import ref
from sdd.generic.staged_planner import StagedPlanner


class ComparisonContracts(StagedPlanner):
    def ask_factors(self, tenant, state, questions, phase):
        picks = {
            "dag_root": "readings",
            "dag_group": "no",
            "dag_rank": "no",
            "dag_rank_basis": "primary",
            "dag_reference_scope": "global",
            "dag_primary_metric_0_field": "f1",
            "dag_reference_metric_0": "avg",
            "dag_reference_metric_0_field": "f1",
            "dag_table_0": "needed",
            "dag_return_f0": "yes",
            "dag_comparison": "gt",
        }
        return {
            key: picks[key]
            if key in picks
            else (
                1
                if q["type"] == "noul"
                else next(
                    (
                        candidate
                        for candidate in ("none", "no", "identity")
                        if candidate in q["criteria"]
                    ),
                    next(iter(q["criteria"])),
                )
            )
            for key, q in questions.items()
        }

    def evaluate(self, tenant, jobs, stage):
        return [{"answers": {key: {"noul": 1} for key in questions}} for _, questions in jobs]

    def finish(self, tenant, state, dag, source, metric_labels, numbers):
        return dag.project(source, {"label": ref("f0")})


@pytest.mark.parametrize("amounts, expected", [([2, 6, None], ["item-1"]), ([2, 2, None], [])])
def test_reference_comparison_is_applied_after_independent_mean(amounts, expected):
    db = Database("sqlite://")
    db.initialize()
    catalog = Catalog(db)
    dataset = catalog.create(
        "t",
        "readings",
        [{"label": f"item-{i}", "amount": number} for i, number in enumerate(amounts)],
    )
    fields = {
        "f0": Field("f0", "readings", "label", "text"),
        "f1": Field("f1", "readings", "amount", "number"),
    }
    planner = ComparisonContracts(catalog, SimpleNamespace(decisions=[], model="fixture"))
    request = "List observations whose amount exceeds the overall average."
    plan = planner.compose(
        "t",
        request,
        [dataset],
        fields,
        [],
        {"none": "None", **{k: f.label for k, f in fields.items()}},
        {"request": request},
    )
    result = SQLService(db).execute("t", plan["logical_sql"])["result"]
    assert [row["label"] for row in result] == expected
    db.engine.dispose()


class RuleContracts:
    model = "fixture"

    def ask(self, tenant, state, questions):
        answers = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.99}
                continue
            options = question["criteria"]
            chosen = next(iter(options))
            if key.startswith("kb_field_"):
                chosen = "output" if state["fields"][key[9:]]["name"] == "id" else "unused"
            elif key.startswith("kb_rule_"):
                chosen = "metric"
            elif key == "kb_count":
                chosen = "2"
            elif key == "kb_population":
                chosen = "observations"
            elif key.startswith("kb_output_"):
                chosen = "f0" if key.endswith("_0") else "k0"
            elif key.startswith("kb_agg_"):
                chosen = "value"
            elif key == "kb_predicate_k0":
                chosen = "v0_gt"
            elif key.startswith(("kb_sort_", "kb_round_", "kb_predicate_")) or key == "kb_limit":
                chosen = "none"
            answers[key] = {
                "type": "choice",
                "choice": chosen,
                "probabilities": {k: float(k == chosen) for k in options},
            }
        return {"model": self.model, "answers": answers, "usage": {}}


def test_numeric_business_rule_threshold_filters_derived_value():
    db = Database("sqlite://")
    db.initialize()
    Catalog(db).create(
        "t",
        "observations",
        [
            {"id": 1, "amount": 3},
            {"id": 2, "amount": 5},
            {"id": 3, "amount": None},
        ],
        primary_key=["id"],
    )
    try:
        plan = Planner(db, RuleContracts(), "staged").plan(
            "t",
            "Show id and Score only for observations with Score above 8.",
            knowledge=[{"id": "0", "name": "Score", "definition": "S = amount * 2"}],
        )
    except PlanReviewRequired as exc:
        plan = exc.plan
    result = SQLService(db).execute("t", plan["logical_sql"])["result"]
    assert [tuple(row.values()) for row in result] == [(2, 10)]
    db.engine.dispose()


def test_joint_assignment_preserves_two_different_aggregates_of_one_operand():
    from sdd.generic.knowledge_planner import KnowledgePlanner
    from sdd.generic.stage_dag import Column

    planner = KnowledgePlanner(None, None)
    planner.answers = {f"kb_output_{i}": {"probabilities": {"x": 1.0}} for i in range(2)}
    bindings = planner.assign_outputs(
        {"kb_output_0": "x", "kb_output_1": "x", "kb_agg_0": "avg", "kb_agg_1": "sum"},
        2,
        {"x": ref("x")},
        {"x": Column("number", "measurement")},
    )
    assert bindings == {"kb_output_0": "x", "kb_output_1": "x"}


def test_safe_unicode_formula_and_nested_json_path():
    from sdd.generic.knowledge import RuleGraph, rules_from
    from sdd.generic.stage_dag import Column
    import sqlite3

    rules = rules_from(
        [
            {
                "id": "0",
                "name": "Quality",
                "definition": "Q = metric × telemetry.sensor.value ÷ 2, using measurements from the catalog.",
            }
        ]
    )
    fields = {
        "m": Field("m", "observations", "metric", "number"),
        "j": Field("j", "observations", "telemetry", "text"),
    }
    graph = RuleGraph(
        rules, fields, {"m": Column("number", "metric"), "j": Column("text", "JSON telemetry")}
    )
    term = graph.build("0")
    with sqlite3.connect(":memory:") as connection:
        result = connection.execute(
            "SELECT " + term.sql().sql(dialect="sqlite") + " FROM (SELECT 6 AS m, ? AS j)",
            ('{"sensor":{"value":4}}',),
        ).fetchone()[0]
    assert result == 12


def test_duplicate_raw_outputs_are_jointly_assigned_with_corrections():
    from sdd.generic.knowledge_planner import KnowledgePlanner
    from sdd.generic.stage_dag import Column

    planner = KnowledgePlanner(None, None)
    planner.answers = {
        "kb_output_0": {"probabilities": {"x": 0.6, "y": 0.4}, "_selection": "y"},
        "kb_output_1": {"probabilities": {"x": 0.9, "y": 0.1}},
    }
    bindings = planner.assign_outputs(
        {"kb_output_0": "y", "kb_output_1": "x", "kb_agg_0": "value", "kb_agg_1": "value"},
        2,
        {"x": ref("x"), "y": ref("y")},
        {"x": Column("number", "x"), "y": Column("number", "y")},
    )
    assert bindings == {"kb_output_0": "y", "kb_output_1": "x"}


def test_distinct_answers_can_order_by_an_omitted_measure():
    import sqlite3
    from sqlglot import exp
    from sdd.generic.stage_dag import Column, StageDAG

    class OrderedContracts(StagedPlanner):
        def ask_factors(self, tenant, state, questions, phase):
            picks = {
                "dag_output_count": "1",
                "dag_output_0": "label",
                "dag_distinct": "yes",
                "dag_sort": "amount",
                "dag_sort_direction": "asc",
                "dag_limit": "none",
            }
            return {key: picks[key] for key in questions}

    dag = StageDAG()
    source = dag.source(
        exp.select("label", "amount").from_("readings"),
        {"label": Column("text", "label"), "amount": Column("number", "amount")},
    )
    planner = OrderedContracts(None, SimpleNamespace(decisions=[]))
    target = planner.finish(
        "t",
        {"request": "Distinct labels in ascending measurement order", "field_contracts": {}},
        dag,
        source,
        {},
        [],
    )
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE readings(label TEXT, amount REAL)")
        db.executemany("INSERT INTO readings VALUES (?,?)", [("A", 3), ("A", 1), ("B", 2)])
        assert db.execute(dag.compile(target).sql(dialect="sqlite")).fetchall() == [("A",), ("B",)]


def test_catalog_json_field_is_a_typed_output_and_retains_null():
    from sqlalchemy import update
    from sdd.generic import schema

    class JsonContracts(RuleContracts):
        def ask(self, tenant, state, questions):
            result = super().ask(tenant, state, questions)
            for key, question in questions.items():
                selected = None
                if key.startswith("kb_field_j"):
                    selected = "output"
                elif key.startswith("kb_rule_"):
                    selected = "unused"
                elif key == "kb_output_1":
                    selected = "j0"
                if selected:
                    result["answers"][key] = {
                        "type": "choice",
                        "choice": selected,
                        "probabilities": {k: float(k == selected) for k in question["criteria"]},
                    }
            return result

    db = Database("sqlite://")
    db.initialize()
    dataset = Catalog(db).create(
        "t",
        "observations",
        [
            {"id": 1, "telemetry": '{"sensor":{"value":4.5}}'},
            {"id": 2, "telemetry": '{"sensor":{}}'},
        ],
        primary_key=["id"],
    )
    for column in dataset["columns"]:
        if column["name"] == "telemetry":
            column["json_fields"] = {"sensor.value": {"type": "number", "description": "Reading"}}
    with db.transaction("t") as connection:
        connection.execute(
            update(schema.datasets)
            .where(schema.datasets.c.id == dataset["id"])
            .values(columns=dataset["columns"])
        )
    try:
        plan = Planner(db, JsonContracts(), "staged").plan(
            "t",
            "Show id and the sensor reading for every observation",
            knowledge=[{"id": "unused", "name": "Unused rule", "definition": "U = id * 2"}],
        )
    except PlanReviewRequired as exc:
        plan = exc.plan
    rows = SQLService(db).execute("t", plan["logical_sql"])["result"]
    assert [tuple(row.values()) for row in rows] == [(1, 4.5), (2, None)]
    db.engine.dispose()
