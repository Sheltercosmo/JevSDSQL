import sqlite3

import pytest
from sqlglot import parse_one
from sqlglot.optimizer.qualify import qualify

from sdd.generic.query_rewrites import decorrelate_scalar_aggregate


def rewrite(query):
    return decorrelate_scalar_aggregate(qualify(parse_one(query, dialect="postgres")))


@pytest.mark.parametrize(
    "aggregate", ["AVG(b.value)*2", "SUM(b.value)", "MIN(b.value)", "MAX(b.value)+1"]
)
def test_correlated_aggregate_preserves_nulls_empty_matches_and_duplicates(aggregate):
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE readings (id INTEGER, bucket INTEGER, category TEXT, value REAL)"
    )
    connection.executemany(
        "INSERT INTO readings VALUES (?, ?, ?, ?)",
        [
            (1, 1, "a", 2),
            (2, 1, "a", 8),
            (3, 1, "b", 30),
            (4, 2, "a", None),
            (5, None, "a", 5),
            (6, None, "a", 10),
            (7, 3, None, 7),
            (8, 3, None, 7),
        ],
    )
    query = f"SELECT a.id, (SELECT {aggregate} FROM readings b WHERE b.bucket=a.bucket AND b.category=a.category) AS x FROM readings a ORDER BY a.id"
    changed = rewrite(query).sql(dialect="sqlite")
    assert "JOIN" in changed
    assert connection.execute(changed).fetchall() == connection.execute(query).fetchall()
    connection.close()


@pytest.mark.parametrize(
    "expression, suffix",
    [
        ("COUNT(*)", ""),
        ("COALESCE(SUM(b.value),0)", ""),
        ("AVG(b.value)", " AND b.value>0"),
        ("AVG(b.value)", " LIMIT 1"),
    ],
)
def test_unproven_rewrites_remain_unchanged(expression, suffix):
    query = f"SELECT a.id FROM readings a WHERE a.value>(SELECT {expression} FROM readings b WHERE b.bucket=a.bucket{suffix})"
    tree = qualify(parse_one(query))
    before = tree.sql()
    assert decorrelate_scalar_aggregate(tree).sql() == before
