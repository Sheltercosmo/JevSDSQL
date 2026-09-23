"""Compile and execute a shared two-population DAG without a model call."""

from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlglot import parse_one  # noqa: E402
from sdd.generic.cohort_planner import cohort_dag  # noqa: E402
from sdd.generic.stage_dag import Column, operation, ref, value  # noqa: E402

query = parse_one("SELECT account_id, plan FROM accounts")
dag, final = cohort_dag(
    query,
    {"account_id": Column("integer", "Account", unit="identifier"), "plan": Column("text", "Plan")},
    [operation("eq", ref("plan"), value("paid"))],
    [],
    aggregate="count_distinct",
    operand="account_id",
    operation="percentage",
)
with sqlite3.connect(":memory:") as connection:
    connection.execute("CREATE TABLE accounts(account_id INTEGER, plan TEXT)")
    connection.executemany("INSERT INTO accounts VALUES (?,?)", [(1, "paid"), (2, "free")])
    result = connection.execute(dag.compile(final).sql(dialect="sqlite")).fetchone()[0]
    assert result == 50
    print("Paid accounts:", result, "%")
print(dag.compile(final).sql(dialect="postgres", pretty=True))
