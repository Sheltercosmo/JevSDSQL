import sqlite3

import pytest
from sqlglot import parse_one

from sdd.generic.cohort_planner import cohort_dag, predicate_options
from sdd.generic.stage_dag import Column, operation as op, ref, value
from sdd.generic.value_evidence import rank_values


def execute(aggregate, operand, operation, left, right, rows):
    query = parse_one("SELECT entity, amount, category FROM events")
    columns = {
        "entity": Column("integer", "entity", unit="identifier"),
        "amount": Column("number", "amount"),
        "category": Column("text", "category"),
    }
    dag, final = cohort_dag(
        query, columns, left, right, aggregate=aggregate, operand=operand, operation=operation
    )
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE events(entity INTEGER, amount REAL, category TEXT)")
        connection.executemany("INSERT INTO events VALUES (?,?,?)", rows)
        result = connection.execute(dag.compile(final).sql(dialect="sqlite")).fetchone()[0]
    return result, dag.describe(final)


def test_population_percentage_preserves_entity_identity_under_join_duplicates():
    left = [op("eq", ref("category"), value("A"))]
    result, graph = execute(
        "count_distinct",
        "entity",
        "percentage",
        left,
        [],
        [(1, 2, "A"), (1, 3, "A"), (2, 4, "B"), (3, 7, "B")],
    )
    assert result == pytest.approx(100 / 3)
    assert any(len(layer) > 1 for layer in graph["layers"])


def test_difference_of_counts_is_not_difference_of_identifier_sums():
    left = [op("lt", ref("amount"), value(10))]
    right = [op("gt", ref("amount"), value(20))]
    result, _ = execute(
        "count",
        None,
        "difference",
        left,
        right,
        [(90, 5, "A"), (700, 6, "B"), (1000, 30, "B"), (4, None, "A")],
    )
    assert result == 1


def test_common_or_filter_applies_to_both_populations_and_null_is_not_zero():
    common = predicate_options("amount", "number", [0])["zero_or_null"][1]
    left = [common, op("eq", ref("category"), value("A"))]
    right = [common, op("eq", ref("category"), value("B"))]
    result, graph = execute(
        "count",
        None,
        "difference",
        left,
        right,
        [(1, 0, "A"), (2, None, "A"), (3, 99, "A"), (4, 0, "B")],
    )
    assert result == 1
    assert (
        sum(s["description"] == "Restrictions shared by both populations" for s in graph["stages"])
        == 1
    )


def test_empty_denominator_remains_null():
    result, _ = execute(
        "count", None, "ratio", [], [op("eq", ref("category"), value("missing"))], [(1, 5, "A")]
    )
    assert result is None


def test_sum_rejects_identifier_units():
    with pytest.raises(ValueError, match="Identifiers"):
        execute("sum", "entity", "difference", [], [], [(1, 5, "A")])


def test_fuzzy_evidence_ranks_misspelled_name_without_selecting_a_predicate():
    values = ["FAIRFAX CO SCHS", "FREMONT COUNTY", "ADAMS COUNTY", "CLARK COUNTY", "WAYNE COUNTY"]
    assert rank_values("funding for Fairfex County", values)[0] == "FAIRFAX CO SCHS"
