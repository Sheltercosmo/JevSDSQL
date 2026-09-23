from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.relational import Field
from sdd.generic.sql import SQLService
from sdd.generic.staged_planner import StagedPlanner


def test_raw_comparison_operand_is_not_erased_by_an_unused_count_decision():
    class Contracts(StagedPlanner):
        def ask_factors(self, tenant, state, questions, phase):
            picks = {
                "dag_root": "readings",
                "dag_group": "no",
                "dag_primary_scope": "rows",
                "dag_rank": "yes",
                "dag_rank_direction": "asc",
                "dag_rank_ties": "rank",
                "dag_rank_count": "1",
                "dag_rank_basis": "distance",
                "dag_reference_scope": "groups",
                "dag_primary_metric_0": "count",
                "dag_primary_metric_0_field": "f1",
                "dag_reference_metric_0": "avg",
                "dag_reference_metric_0_field": "f1",
                "dag_table_0": "needed",
                "dag_partition_f0": "yes",
                "dag_reference_grain_f0": "yes",
                "dag_return_f0": "yes",
                "dag_return_f1": "yes",
                "dag_rank_partition_field": "f0",
            }
            return {
                key: picks[key]
                if key in picks
                else (
                    1
                    if q["type"] == "noul"
                    else next(
                        option for option in ("none", "no", "identity") if option in q["criteria"]
                    )
                )
                for key, q in questions.items()
            }

        def evaluate(self, tenant, jobs, stage):
            return [{"answers": {key: {"noul": 1} for key in questions}} for _, questions in jobs]

        def finish(self, tenant, state, dag, source, metric_labels, numbers):
            from sdd.generic.stage_dag import ref

            return dag.project(source, {"zone": ref("f0"), "amount": ref("f1")})

    from types import SimpleNamespace

    db = Database("sqlite://")
    db.initialize()
    catalog = Catalog(db)
    dataset = catalog.create(
        "t",
        "readings",
        [
            {"zone": "A", "amount": 2},
            {"zone": "A", "amount": 6},
            {"zone": "A", "amount": 7},
        ],
    )
    fields = {
        "f0": Field("f0", "readings", "zone", "text"),
        "f1": Field("f1", "readings", "amount", "integer"),
    }
    compiler = Contracts(catalog, SimpleNamespace(decisions=[], model="fixture"))
    result = compiler.compose(
        "t",
        "Closest reading to its zone average",
        [dataset],
        fields,
        [],
        {"none": "None", **{k: f.label for k, f in fields.items()}},
        {"request": "Closest reading to its zone average"},
    )
    assert SQLService(db).execute("t", result["logical_sql"])["result"] == [
        {"zone": "A", "amount": 6}
    ]
    db.engine.dispose()
