import sqlite3

import pytest
from sqlglot import exp

from sdd.generic.knowledge import RuleGraph, rules_from
from sdd.generic.relational import Field
from sdd.generic.stage_dag import Column, StageDAG, operation as op, ref


def rule_graph(definition, name="Completion Ratio"):
    fields = {
        "q": Field("q", "requests", "quantity", "number"),
        "r": Field("r", "updates", "remaining", "number"),
    }
    return RuleGraph(
        rules_from([{"id": "ratio", "name": name, "definition": definition}]),
        fields,
        {k: Column("number", f.name) for k, f in fields.items()},
    )


def test_named_formula_is_separated_from_its_explanation():
    graph = rule_graph(
        r"Completion Ratio = \frac{quantity - remaining}{quantity} \times 100, \text{where } quantity \text{ is the requested amount.}"
    )
    term = graph.build("ratio")
    with sqlite3.connect(":memory:") as db:
        answer = db.execute(
            "SELECT " + term.sql().sql(dialect="sqlite") + " FROM (SELECT 8 q, 2 r)"
        ).fetchone()[0]
    assert answer == 75


def test_percentile_window_preserves_ties_and_population():
    dag = StageDAG()
    source = dag.source(
        exp.select("item", "amount").from_("observations"),
        {"item": Column("text", "item"), "amount": Column("number", "amount")},
    )
    ranked = dag.window(source, "percentile", op("percent_rank"), order=[("amount", False)])
    result = dag.project(ranked, {"item": ref("item"), "percentile": ref("percentile")})
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE observations(item TEXT, amount REAL)")
        db.executemany(
            "INSERT INTO observations VALUES (?,?)", [("A", 1), ("B", 1), ("C", 5), ("D", 9)]
        )
        values = dict(db.execute(dag.compile(result).sql(dialect="sqlite")))
    assert values == {"A": 0, "B": 0, "C": pytest.approx(2 / 3), "D": 1}


def test_scoped_cardinality_keeps_its_relationship_bindings():
    from sdd.generic.rule_reductions import reductions

    captured = []

    def register(table, reducer, operand, bindings=()):
        captured.append((table, reducer, operand, bindings))
        return "a0"

    result = reductions(
        r"SC_a = \left| \{ \text{record_id} \in \text{visits} \mid \text{profile_ref} = \text{profile_id}, \text{owner_ref} = a \} \right|",
        register,
    )
    assert "a0" in result
    assert captured == [
        ("visits", "count", None, (("profile_ref", "profile_id"), ("owner_ref", "a")))
    ]


def test_latest_relation_is_reduced_before_parent_join():
    from sdd.generic.rule_stages import latest_source

    dag = StageDAG()
    node = latest_source(
        dag,
        "updates",
        {
            "id": Column("integer", "sequence"),
            "owner": Column("integer", "owner"),
            "observed": Column("text", "observation time"),
            "remaining": Column("number", "remaining"),
        },
        partition=["owner"],
        order=[("observed", True), ("id", True)],
    )
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE updates(id INTEGER, owner INTEGER, observed TEXT, remaining REAL)")
        db.executemany(
            "INSERT INTO updates VALUES (?,?,?,?)",
            [
                (10, 1, "2026-02-01", 8),
                (4, 1, "2026-02-03", 3),
                (11, 1, "2026-02-03", 2),
                (9, 2, "2026-01-01", None),
            ],
        )
        rows = db.execute(dag.compile(node).sql(dialect="sqlite")).fetchall()
    assert {(row[1], row[3]) for row in rows} == {(1, 2), (2, None)}


def test_weighted_formula_preserves_both_reductions_and_local_definition():
    fields = {k: Field(k, "readings", name, "number") for k, name in (("x", "score"), ("d", "age"))}
    graph = RuleGraph(
        rules_from(
            [
                {
                    "id": "r",
                    "name": "Weighted Score",
                    "definition": r"W = \frac{\sum_{r \in readings} (score \cdot w(r))}{\sum_{r \in readings} w(r)}, \text{where } w(r) = 1 / (1 + age)",
                }
            ]
        ),
        fields,
        {k: Column("number", f.name) for k, f in fields.items()},
    )
    graph.build("r")
    assert len(graph.reductions) == 2
    assert graph.reductions["a0"][2].columns() == {"x", "d"}
    assert graph.reductions["a1"][2].columns() == {"d"}


def test_row_index_is_resolved_only_inside_its_declared_domain():
    graph = RuleGraph(
        rules_from(
            [{"id": "r", "name": "Total", "definition": r"T = \sum_{i \in readings} amount_i"}]
        ),
        {"a": Field("a", "readings", "amount", "number")},
        {"a": Column("number", "amount")},
    )
    graph.build("r")
    assert graph.reductions["a0"][2] == ref("a")
    graph = rule_graph("T = remaining_i")
    with pytest.raises(ValueError, match="catalog matches"):
        graph.build("ratio")


def test_ordered_categories_preserve_boolean_precedence_and_fallback():
    graph = rule_graph(
        "Priority is:\n- 'HIGH' if quantity > 4 AND remaining < 2\n- 'MEDIUM' if quantity > 2 OR remaining < 2\n- 'LOW' otherwise"
    )
    term = graph.build("ratio")
    with sqlite3.connect(":memory:") as db:
        rows = [
            db.execute(
                "SELECT " + term.sql().sql(dialect="sqlite") + " FROM (SELECT ? q, ? r)", pair
            ).fetchone()[0]
            for pair in [(8, 1), (8, 4), (1, 4), (None, None)]
        ]
    assert rows == ["HIGH", "MEDIUM", "LOW", "LOW"]
