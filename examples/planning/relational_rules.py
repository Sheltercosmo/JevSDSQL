"""Run: python examples/planning/relational_rules.py (no provider key needed)."""

from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlglot import exp  # noqa: E402
from sdd.generic.stage_dag import Column, StageDAG, operation as op, ref  # noqa: E402


def main():
    dag = StageDAG()
    source = dag.source(
        exp.select("id", "device", "observed", "amount").from_("readings"),
        {
            "id": Column("integer", "Observation sequence"),
            "device": Column("text", "Device identifier"),
            "observed": Column("text", "Observation time"),
            "amount": Column("number", "Measured amount"),
        },
    )
    latest = dag.latest(source, partition=["device"], order=[("observed", True), ("id", True)])
    compared = dag.windows(
        latest,
        {
            "percentile": {"term": op("percent_rank"), "order": [("amount", False)]},
            "population_mean": {"term": op("avg", ref("amount"))},
        },
    )
    answer = dag.project(
        compared, {k: ref(k) for k in ["device", "amount", "percentile", "population_mean"]}
    )
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE readings(id INTEGER, device TEXT, observed TEXT, amount REAL)")
        db.executemany(
            "INSERT INTO readings VALUES(?,?,?,?)",
            [
                (20, "A", "2026-01-01", 99),
                (4, "A", "2026-02-01", 3),
                (5, "A", "2026-02-01", 2),
                (7, "B", "2026-02-01", 8),
            ],
        )
        print(db.execute(dag.compile(answer).sql(dialect="sqlite")).fetchall())


if __name__ == "__main__":
    main()
