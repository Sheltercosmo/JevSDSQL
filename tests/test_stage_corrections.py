from types import SimpleNamespace

import pytest

from sdd.generic.staged_planner import StagedPlanner


def planner(*locked):
    instance = object.__new__(StagedPlanner)
    instance.decisions = SimpleNamespace(
        decisions=[{"key": key, "overridden": True} for key in locked]
    )
    return instance


@pytest.mark.parametrize("key", ["dag_grain_f1", "dag_primary_metric_0_field", "dag_filter_f2"])
def test_contract_reconciliation_never_changes_a_user_correction(key):
    compiler = planner(key)
    contracts = {key: "original"}
    with pytest.raises(ValueError, match="User correction conflicts"):
        compiler.reconcile(contracts, key, "derived")
    assert contracts == {key: "original"}


def test_reconciled_decisions_record_the_actually_compiled_binding():
    compiler = planner("dag_grain_f1")
    contracts = {"dag_grain_f1": "yes", "dag_partition_f1": "no"}
    compiler.reconcile(contracts, "dag_grain_f1", "yes")
    compiler.reconcile(contracts, "dag_partition_f1", "yes")
    assert compiler.contract_bindings == contracts
