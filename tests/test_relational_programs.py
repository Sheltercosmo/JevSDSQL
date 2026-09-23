from decimal import Decimal

import pytest

from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.relational import Field, Link, Program
from sdd.generic.sql import SQLService


@pytest.fixture
def relational():
    db = Database("sqlite://")
    db.initialize()
    catalog = Catalog(db)
    catalog.create(
        "test",
        "units",
        [
            {"uid": "a", "label": "Alpha", "zone": "east"},
            {"uid": "b", "label": "Beta", "zone": "east"},
            {"uid": "c", "label": "Gamma", "zone": "west"},
            {"uid": "d", "label": "Delta", "zone": "west"},
        ],
        primary_key=["uid"],
    )
    catalog.create(
        "test",
        "observations",
        [
            {"uid": "a", "tick": 1, "reading": 10, "mass": 1},
            {"uid": "a", "tick": 2, "reading": 8, "mass": 2},
            {"uid": "a", "tick": 3, "reading": 12, "mass": 3},
            {"uid": "b", "tick": 1, "reading": 20, "mass": 9},
            {"uid": "b", "tick": 2, "reading": 21, "mass": 8},
            {"uid": "b", "tick": 3, "reading": 22, "mass": 7},
            {"uid": "c", "tick": 1, "reading": 30, "mass": 2},
            {"uid": "c", "tick": 3, "reading": None, "mass": 4},
        ],
        primary_key=["uid", "tick"],
        columns=[
            {"name": "uid", "type": "text", "nullable": False},
            {"name": "tick", "type": "integer", "nullable": False},
            {"name": "reading", "type": "number", "nullable": True},
            {"name": "mass", "type": "number", "nullable": False},
        ],
    )
    fields = {
        "entity": Field("entity", "observations", "uid", "text"),
        "time": Field("time", "observations", "tick", "integer"),
        "reading": Field("reading", "observations", "reading", "number"),
        "mass": Field("mass", "observations", "mass", "number"),
        "code": Field("code", "units", "uid", "text"),
        "name": Field("name", "units", "label", "text"),
        "zone": Field("zone", "units", "zone", "text"),
    }
    program = Program("observations", fields, [Link("observations", "units", "uid", "uid")])
    yield SQLService(db), program
    db.engine.dispose()


def results(service, program):
    sql = program.compile().sql(dialect="postgres")
    return [list(row.values()) for row in service.execute("test", sql)["result"]]


def test_output_contract_does_not_expose_sorting_measure(relational):
    service, plan = relational
    plan.family, plan.aggregation = "groups", "count"
    plan.outputs, plan.groups = ["entity"], ["entity"]
    plan.order, plan.limit = [("stat", True), ("entity", False)], 1
    assert results(service, plan) == [["a"]]


def test_baseline_comparison_preserves_row_grain(relational):
    service, plan = relational
    plan.family, plan.measure = "baseline", "reading"
    plan.filters, plan.outputs = [("time", "eq", 1)], ["name", "shortfall"]
    plan.order = [("shortfall", True)]
    assert results(service, plan) == [["Alpha", 10]]


def test_partition_baseline_does_not_mix_groups(relational):
    service, plan = relational
    plan.family, plan.measure, plan.partition = "baseline", "reading", "zone"
    plan.filters, plan.outputs = [("time", "eq", 1)], ["name", "shortfall"]
    assert results(service, plan) == [["Alpha", 5]]


def test_pair_comparison_requires_both_nonnull_observations(relational):
    service, plan = relational
    plan.family, plan.measure, plan.entity, plan.time = "periods", "reading", "entity", "time"
    plan.periods = [1, 3]
    plan.outputs = ["code", "name", "gain:reading:0:1"]
    plan.order = [("gain:reading:0:1", True), ("code", False)]
    assert results(service, plan) == [["a", "Alpha", 2], ["b", "Beta", 2]]


def test_three_period_conditions_are_scoped_to_distinct_roles(relational):
    service, plan = relational
    plan.family, plan.measure, plan.entity, plan.time = "periods", "reading", "entity", "time"
    plan.periods = [1, 2, 3]
    plan.temporal_conditions = [("reading", 0, 1, "lt"), ("reading", 0, 2, "ge")]
    plan.outputs = ["code", "loss:reading:0:1"]
    assert results(service, plan) == [["a", 2]]


def test_weighted_aggregation_uses_weights_not_row_average(relational):
    service, plan = relational
    plan.family, plan.aggregation, plan.measure, plan.weight = (
        "groups",
        "weighted",
        "reading",
        "mass",
    )
    plan.filters = [("time", "eq", 1)]
    plan.outputs, plan.groups, plan.order = ["zone", "stat"], ["zone"], [("zone", False)]
    assert results(service, plan) == [["east", 19], ["west", 30]]


def test_weighted_period_change_uses_matched_complete_cohort(relational):
    service, plan = relational
    plan.family, plan.aggregation = "periods", "weighted"
    plan.measure, plan.weight, plan.entity, plan.time = "reading", "mass", "entity", "time"
    plan.periods = [1, 3]
    plan.outputs, plan.groups = ["zone", "weighted_gain:0:1"], ["zone"]
    assert results(service, plan) == [["east", 0]]


def test_percentage_change_preserves_fractional_division(relational):
    service, plan = relational
    plan.family, plan.measure, plan.entity, plan.time = "periods", "reading", "entity", "time"
    plan.periods, plan.outputs, plan.order = (
        [1, 3],
        ["code", "percent:reading:0:1"],
        [("code", False)],
    )
    rows = results(service, plan)
    assert rows == [["a", Decimal("20")], ["b", Decimal("10")]]


def test_partition_ranking_applies_limit_per_group(relational):
    service, plan = relational
    plan.family, plan.partition, plan.measure = "partition_rank", "zone", "reading"
    plan.filters = [("time", "eq", 1)]
    plan.outputs = ["zone", "name", "reading"]
    plan.order, plan.limit = [("zone", False), ("reading", True), ("code", False)], 1
    assert results(service, plan) == [["east", "Beta", 20], ["west", "Gamma", 30]]


def test_absence_keeps_entities_without_children(relational):
    service, plan = relational
    plan.root, plan.family, plan.absent_table = "units", "absence", "observations"
    plan.outputs = ["code", "name"]
    assert results(service, plan) == [["d", "Delta"]]


def test_nonempty_missingness_checks_output_shape_and_count(relational):
    service, plan = relational
    plan.family, plan.aggregation = "groups", "count"
    plan.filters = [("reading", "null", None)]
    plan.outputs, plan.groups = ["code", "name", "stat"], ["code", "name"]
    assert results(service, plan) == [["c", "Gamma", 1]]


def test_nonconnected_relations_cannot_be_invented(relational):
    _, plan = relational
    plan.outputs, plan.links = ["name"], []
    with pytest.raises(ValueError, match="No relationship path"):
        plan.compile()


def test_weighted_mean_excludes_weights_of_missing_measurements(relational):
    service, plan = relational
    plan.family, plan.aggregation, plan.measure, plan.weight = (
        "aggregate",
        "weighted",
        "reading",
        "mass",
    )
    plan.filters, plan.outputs = [("time", "eq", 3)], ["stat"]
    assert results(service, plan) == [[19]]


def test_consecutive_trend_rejects_missing_periods_and_reversals(relational):
    service, plan = relational
    plan.family, plan.measure, plan.entity, plan.time = "trend", "reading", "entity", "time"
    plan.periods, plan.outputs = [1, 3], ["code", "name"]
    assert results(service, plan) == [["b", "Beta"]]


def test_multihop_absence_means_no_matching_descendant(relational):
    service, plan = relational
    catalog = Catalog(service.db)
    catalog.create("test", "checks", [{"cid": 1, "uid": "a", "tick": 2}], primary_key=["cid"])
    plan.links.append(Link("checks", "observations", "uid", "uid"))
    plan.root, plan.family, plan.absent_table = "units", "absence", "checks"
    plan.outputs, plan.order = ["code"], [("code", False)]
    assert results(service, plan) == [["b"], ["c"], ["d"]]


def test_aggregate_of_product_is_not_product_of_aggregates(relational):
    service, plan = relational
    plan.family, plan.aggregation, plan.formula = "aggregate", "sum", "product"
    plan.measure, plan.weight = "reading", "mass"
    plan.filters, plan.outputs = [("time", "eq", 1)], ["stat"]
    assert results(service, plan) == [[250]]


def test_group_display_sort_cannot_choose_the_group_winner(relational):
    service, plan = relational
    plan.family, plan.partition, plan.measure = "partition_rank", "zone", "reading"
    plan.filters, plan.outputs = [("time", "eq", 1)], ["zone", "name"]
    plan.order, plan.limit = [("zone", False)], 1
    assert results(service, plan) == [["east", "Beta"], ["west", "Gamma"]]
    plan.rank_descending = False
    assert results(service, plan) == [["east", "Alpha"], ["west", "Gamma"]]


def test_unexpressed_global_semantic_population_is_not_silently_ignored(relational):
    _, plan = relational
    plan.family, plan.measure, plan.outputs = "baseline", "reading", ["name"]
    plan.filters = [("name", "semantic", "Relevant names")]
    with pytest.raises(ValueError, match="explicit SQL population"):
        plan.compile()
