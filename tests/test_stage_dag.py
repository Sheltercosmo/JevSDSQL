import sqlite3

import pytest
from sqlglot import exp

from sdd.generic.stage_dag import Column, StageDAG, operation as op, ref, value


def source(dag):
    return dag.source(
        exp.select("division", "item", "amount").from_("observations"),
        {
            "division": Column("text", "division"),
            "item": Column("text", "item"),
            "amount": Column("number", "amount", unit="currency"),
        },
    )


def execute(dag, target):
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE observations(division TEXT, item TEXT, amount REAL)")
        db.executemany(
            "INSERT INTO observations VALUES (?,?,?)",
            [
                ("A", "x", 4),
                ("A", "x", 6),
                ("A", "y", 9),
                ("A", "z", 2),
                ("B", "q", 5),
                ("B", "q", 8),
                ("B", "r", 7),
            ],
        )
        return db.execute(dag.compile(target).sql(dialect="sqlite")).fetchall()


def test_aggregate_then_rank_then_aggregate_changes_grain():
    dag = StageDAG()
    raw = source(dag)
    totals = dag.aggregate(raw, ["division", "item"], {"total": op("sum", ref("amount"))})
    ranked = dag.window(
        totals, "position", op("rank"), partition=["division"], order=[("total", True)]
    )
    winners = dag.filter(ranked, op("le", ref("position"), value(1)))
    result = dag.aggregate(winners, [], {"answer": op("avg", ref("total"))})
    assert execute(dag, result) == [(11.5,)]
    assert dag.nodes[totals].grain == ("division", "item")
    assert dag.nodes[result].grain == ()


def test_parallel_branches_share_source_and_preserve_merge_population():
    dag = StageDAG()
    raw = source(dag)
    totals = dag.aggregate(raw, ["division", "item"], {"total": op("sum", ref("amount"))})
    summary = dag.aggregate(
        raw, ["division"], {"orders": op("count"), "region_total": op("sum", ref("amount"))}
    )
    ranked = dag.window(
        totals, "position", op("rank"), partition=["division"], order=[("total", True)]
    )
    winners = dag.filter(ranked, op("eq", ref("position"), value(1)))
    merged = dag.join(
        winners,
        summary,
        [("division", "division")],
        outputs={
            "division": ref("l.division"),
            "item": ref("l.item"),
            "total": ref("l.total"),
            "orders": ref("r.orders"),
            "region_total": ref("r.region_total"),
        },
    )
    assert sorted(execute(dag, merged)) == [("A", "x", 10, 4, 21), ("B", "q", 13, 3, 20)]
    graph = dag.describe(merged)
    assert graph["shared_stages"] == [raw]
    assert graph["max_independent_stages"] == 2
    assert dag.compile(merged).sql().count("FROM observations") == 1


def test_closest_to_group_mean_is_a_separate_distance_and_rank_stage():
    dag = StageDAG()
    raw = source(dag)
    means = dag.aggregate(raw, ["division"], {"baseline": op("avg", ref("amount"))})
    attached = dag.join(
        raw,
        means,
        [("division", "division")],
        outputs={
            "division": ref("l.division"),
            "item": ref("l.item"),
            "amount": ref("l.amount"),
            "baseline": ref("r.baseline"),
        },
    )
    distance = dag.project(
        attached, {"distance": op("abs", op("sub", ref("amount"), ref("baseline")))}, keep=True
    )
    ranked = dag.window(
        distance, "position", op("rank"), partition=["division"], order=[("distance", False)]
    )
    winners = dag.filter(ranked, op("eq", ref("position"), value(1)))
    result = dag.project(winners, {"division": ref("division"), "amount": ref("amount")})
    assert sorted(execute(dag, result)) == [("A", 6), ("B", 7)]


def test_type_scope_and_cardinality_failures_are_rejected_before_sql():
    dag = StageDAG()
    raw = source(dag)
    with pytest.raises(ValueError, match="numeric"):
        dag.aggregate(raw, [], {"bad": op("sum", ref("item"))})
    with pytest.raises(ValueError, match="unavailable"):
        dag.project(raw, {"bad": ref("missing")})
    with pytest.raises(ValueError, match="Nested aggregate"):
        dag.aggregate(raw, [], {"bad": op("avg", op("sum", ref("amount")))})
    with pytest.raises(ValueError, match="multiply"):
        dag.join(raw, raw, [("division", "division")], outputs={"amount": ref("l.amount")})
    with pytest.raises(ValueError, match="after their own stage"):
        dag.filter(raw, op("gt", op("sum", ref("amount")), value(10)))
    with pytest.raises(ValueError, match="order"):
        dag.window(raw, "rank", op("rank"))


def test_identifier_units_zero_denominator_and_common_stage_reuse():
    dag = StageDAG()
    raw = dag.source(
        exp.select("item").from_("observations"),
        {"item": Column("integer", "identity", unit="identifier")},
    )
    with pytest.raises(ValueError, match="Identifiers"):
        dag.aggregate(raw, [], {"bad": op("sum", ref("item"))})
    dag = StageDAG()
    raw = source(dag)
    first = dag.filter(raw, op("gt", ref("amount"), value(4)))
    assert dag.filter(raw, op("gt", ref("amount"), value(4))) == first
    result = dag.project(raw, {"ratio": op("div", ref("amount"), value(0))})
    assert execute(dag, result) == [(None,)] * 7


def test_rolling_window_has_explicit_frame():
    dag = StageDAG()
    raw = source(dag)
    window = dag.window(
        raw,
        "moving",
        op("avg", ref("amount")),
        partition=["division"],
        order=[("amount", False)],
        frame=(1, 1),
    )
    result = dag.finish(window, order=[("division", False), ("amount", False)])
    assert execute(dag, result)[0] == ("A", "z", 2, 3)


def test_arithmetic_rejects_identifier_units_and_logic_requires_booleans():
    schema = {
        "id": Column("integer", "identity", unit="identifier"),
        "amount": Column("number", "measurement"),
    }
    with pytest.raises(ValueError, match="Identifiers"):
        op("sub", ref("id"), ref("amount")).type(schema)
    with pytest.raises(ValueError, match="Boolean"):
        op("and", ref("amount"), value(True)).type(schema)
    assert op("eq", ref("id"), value(3)).type(schema).kind == "boolean"
