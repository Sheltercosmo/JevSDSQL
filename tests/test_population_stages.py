import sqlite3

import pytest
from sqlglot import exp

from sdd.generic.knowledge import RuleGraph
from sdd.generic.rule_stages import compile_values, latest_source
from sdd.generic.stage_dag import Column, StageDAG, operation as op, ref, value


def population():
    columns = {
        "amount": Column("number", "Measured amount"),
        "eligible": Column("boolean", "Eligible cohort"),
    }
    graph = RuleGraph({}, {}, dict(columns))
    dag = StageDAG()
    source = dag.source(exp.select("amount", "eligible").from_("observations"), columns)
    return graph, dag, source


def execute(dag, node, observations):
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE observations(amount REAL, eligible BOOLEAN)")
        db.executemany("INSERT INTO observations VALUES (?,?)", observations)
        return db.execute(dag.compile(node).sql(dialect="sqlite")).fetchall()


def test_mixed_comparison_populations_preserve_duplicates_and_unknowns():
    graph, dag, source = population()
    rank = graph.register_window("percent_rank", ref("amount"))
    mean = graph.register_window("avg", ref("amount"))
    node = compile_values(
        dag, source, graph, {"w0", "w1"}, restrictions=[ref("eligible")], scopes={"w1": (0,)}
    )
    node = dag.project(node, {"amount": ref("amount"), "rank": rank, "mean": mean})
    rows = execute(dag, node, [(1, False), (2, True), (2, True), (9, True), (10, None)])
    assert sorted(rows) == [
        (1, 0, None),
        (2, 0.25, pytest.approx(13 / 3)),
        (2, 0.25, pytest.approx(13 / 3)),
        (9, 0.75, pytest.approx(13 / 3)),
        (10, 1, None),
    ]
    assert dag.describe(node)["max_independent_stages"] >= 2


def test_filter_placement_changes_percentile_denominator():
    graph, dag, source = population()
    rank = graph.register_window("percent_rank", ref("amount"))
    results = []
    for scope in ({}, {"w0": (0,)}):
        node = compile_values(
            dag, source, graph, {"w0"}, restrictions=[ref("eligible")], scopes=scope
        )
        node = dag.filter(node, ref("eligible"))
        node = dag.project(node, {"amount": ref("amount"), "rank": rank})
        results.append(sorted(execute(dag, node, [(1, False), (2, True), (3, True)])))
    assert results == [[(2, 0.5), (3, 1)], [(2, 0), (3, 1)]]


def test_independent_windows_share_one_dependency_barrier():
    graph, dag, source = population()
    graph.register_window("percent_rank", ref("amount"))
    graph.register_window("avg", ref("amount"), [ref("eligible")])
    node = compile_values(dag, source, graph, {"w0", "w1"})
    windows = [s for s in dag.describe(node)["stages"] if s["operator"] == "window"]
    assert len(windows) == 1
    assert set(windows[0]["columns"]) >= {"w0", "w1"}


def test_population_filter_cannot_depend_on_its_own_percentile():
    graph, dag, source = population()
    graph.register_window("percent_rank", ref("amount"))
    with pytest.raises(ValueError, match="cyclic population"):
        compile_values(
            dag,
            source,
            graph,
            {"w0"},
            restrictions=[op("gt", ref("w0"), value(0.5))],
            scopes={"w0": (0,)},
        )


@pytest.mark.parametrize(
    "rows,expected", [([], []), ([(7, True)], [(0,)]), ([(7, True), (7, True)], [(0,), (0,)])]
)
def test_empty_singleton_and_all_tied_populations(rows, expected):
    graph, dag, source = population()
    rank = graph.register_window("percent_rank", ref("amount"))
    node = compile_values(dag, source, graph, {"w0"})
    node = dag.project(node, {"rank": rank})
    assert execute(dag, node, rows) == expected


def test_latest_source_preserves_parent_without_observation():
    dag = StageDAG()
    latest = latest_source(
        dag,
        "events",
        {
            "id": Column("integer", "Event id"),
            "owner": Column("integer", "Owner"),
            "at": Column("text", "Event time"),
        },
        partition=["owner"],
        order=[("at", True), ("id", True)],
    )
    query = (
        exp.select("p.id", "e.id AS observation")
        .from_("parents p")
        .join("events e", on="e.owner = p.id", join_type="LEFT")
    )
    result = dag.source(
        query,
        {"id": Column("integer", "Parent"), "observation": Column("integer", "Latest observation")},
        bindings={"events": latest},
    )
    with sqlite3.connect(":memory:") as db:
        db.executescript(
            "CREATE TABLE parents(id INTEGER); INSERT INTO parents VALUES(1),(2);"
            "CREATE TABLE events(id INTEGER,owner INTEGER,at TEXT);"
            "INSERT INTO events VALUES(8,1,'2020-02-02'),(20,1,'2020-01-01');"
        )
        assert db.execute(dag.compile(result).sql(dialect="sqlite")).fetchall() == [
            (1, 8),
            (2, None),
        ]


@pytest.mark.parametrize("kind,unit", [("text", None), ("integer", "identifier")])
def test_distribution_measure_must_be_numeric_and_not_an_identifier(kind, unit):
    graph = RuleGraph({}, {}, {"x": Column(kind, "x", unit=unit)})
    with pytest.raises(ValueError, match="numeric measure"):
        graph.register_window("percent_rank", ref("x"))


def test_fixed_catalog_choices_do_not_call_the_provider():
    from sdd.generic.knowledge_planner import KnowledgePlanner
    from sdd.generic.jev import choice

    class Provider:
        def ask_many(self, tenant, jobs):
            assert jobs == []
            return []

    planner = KnowledgePlanner(None, Provider())
    answers = planner.batch(
        "tenant",
        [({}, {"root": choice("Entity population", {"only": "Only table"})})],
        "independent_grounding",
    )
    assert answers == {"root": "only"}


def test_case_promotes_integer_and_decimal_branches():
    term = op("case", value(True), ref("count"), value(0.5))
    assert term.type({"count": Column("integer", "Count")}).kind == "number"
