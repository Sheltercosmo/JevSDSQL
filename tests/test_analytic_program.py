import sqlite3

from sdd.generic.analytic_program import AnalyticProgram
from sdd.generic.relational import Field, Link


def run(program, setup):
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(setup)
        return connection.execute(program.compile().sql(dialect="sqlite")).fetchall()


def test_conditional_percentage_preserves_the_common_population():
    fields = {
        key: Field(key, "events", key, kind)
        for key, kind in [("id", "integer"), ("segment", "text"), ("success", "integer")]
    }
    program = AnalyticProgram(
        "events",
        fields,
        [],
        family="aggregate",
        aggregation="count",
        ratio_kind="count",
        filters=[("segment", "eq", "A")],
        numerator_filters=[("success", "eq", 1)],
        outputs=["stat"],
    )
    rows = run(
        program,
        "CREATE TABLE events(id,segment,success); INSERT INTO events VALUES(1,'A',1),(2,'A',0),(3,'A',0),(4,'A',0),(5,'B',1);",
    )
    assert rows == [(25.0,)]


def test_conditional_sums_share_a_relation_without_sharing_their_filters():
    fields = {
        key: Field(key, "observations", key, kind)
        for key, kind in [("amount", "number"), ("category", "text")]
    }
    program = AnalyticProgram(
        "observations",
        fields,
        [],
        family="aggregate",
        aggregation="sum",
        measure="amount",
        ratio_kind="sum",
        ratio_scale=1,
        numerator_filters=[("category", "eq", "A")],
        denominator_filters=[("category", "eq", "B")],
        outputs=["stat"],
    )
    assert run(
        program,
        "CREATE TABLE observations(amount,category); INSERT INTO observations VALUES(6,'A'),(3,'A'),(2,'B'),(1,'B');",
    ) == [(3.0,)]
    assert run(
        program,
        "CREATE TABLE observations(amount,category); INSERT INTO observations VALUES(6,'A'),(0,'B');",
    ) == [(None,)]


def test_unused_metric_does_not_add_a_population_changing_join():
    fields = {
        "id": Field("id", "items", "id", "integer"),
        "cost": Field("cost", "charges", "cost", "number"),
    }
    link = Link("charges", "items", "item_id", "id")
    program = AnalyticProgram(
        "items", fields, [link], outputs=["id"], metrics={"metric:avg:cost": ("avg", "cost")}
    )
    assert run(
        program,
        "CREATE TABLE items(id); CREATE TABLE charges(item_id,cost); INSERT INTO items VALUES(1),(2); INSERT INTO charges VALUES(1,8);",
    ) == [(1,), (2,)]


def test_independent_metric_can_be_returned_while_ranking_by_count():
    fields = {
        "customer": Field("customer", "orders", "customer", "text"),
        "amount": Field("amount", "orders", "amount", "number"),
    }
    program = AnalyticProgram(
        "orders",
        fields,
        [],
        family="groups",
        aggregation="count",
        metrics={"metric:avg:amount": ("avg", "amount")},
        groups=["customer"],
        outputs=["customer", "metric:avg:amount"],
        order=[("stat", True)],
        limit=1,
    )
    assert run(
        program,
        "CREATE TABLE orders(customer,amount); INSERT INTO orders VALUES('A',2),('A',6),('B',100);",
    ) == [("A", 4.0)]


def test_composite_relationship_keeps_every_key_component():
    fields = {
        "value": Field("value", "events", "value", "integer"),
        "name": Field("name", "periods", "name", "text"),
    }
    links = [
        Link("events", "periods", "series", "series", constraint_id="period"),
        Link("events", "periods", "step", "step", constraint_id="period"),
    ]
    program = AnalyticProgram("events", fields, links, outputs=["name", "value"])
    assert run(
        program,
        "CREATE TABLE events(series,step,value); CREATE TABLE periods(series,step,name); INSERT INTO events VALUES(1,2,9); INSERT INTO periods VALUES(1,1,'old'),(1,2,'current');",
    ) == [("current", 9)]


def test_result_interval_has_the_requested_width_and_offset():
    fields = {"id": Field("id", "items", "id", "integer")}
    program = AnalyticProgram(
        "items", fields, [], outputs=["id"], order=[("id", False)], offset=1, range_limit=2
    )
    assert run(program, "CREATE TABLE items(id); INSERT INTO items VALUES(1),(2),(3),(4);") == [
        (2,),
        (3,),
    ]


def test_composite_absence_matches_one_child_tuple_not_separate_components():
    fields = {key: Field(key, "parents", key, "integer") for key in ("a", "b")}
    links = [Link("children", "parents", key, key, constraint_id="pair") for key in ("a", "b")]
    program = AnalyticProgram(
        "parents", fields, links, family="absence", absent_table="children", outputs=["a", "b"]
    )
    assert run(
        program,
        "CREATE TABLE parents(a,b); CREATE TABLE children(a,b); INSERT INTO parents VALUES(1,2),(1,1); INSERT INTO children VALUES(1,1),(2,2);",
    ) == [(1, 2)]
