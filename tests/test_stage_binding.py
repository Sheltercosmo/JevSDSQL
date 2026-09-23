from sqlglot import exp

from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService, col
from sdd.generic.stage_dag import Column, StageDAG, operation as op, ref, value


def test_date_and_clean_numeric_stages_use_the_normal_tenant_binder():
    db = Database("sqlite:///:memory:")
    db.initialize()
    catalog = Catalog(db)
    catalog.create(
        "one",
        "记录",
        [
            {"日期": "2021-09-14", "金额": "USD 1,234.50/yr"},
            {"日期": "2020-12-01", "金额": "₹-5.25"},
        ],
    )
    dag = StageDAG()
    raw = dag.source(
        exp.select(
            exp.alias_(col("日期"), "when", quoted=True),
            exp.alias_(col("金额"), "money", quoted=True),
        ).from_(exp.Table(this=exp.to_identifier("记录", quoted=True))),
        {"when": Column("text", "observation date"), "money": Column("text", "monetary amount")},
    )
    converted = dag.project(
        raw, {"year": op("year", ref("when")), "amount": op("clean_number", ref("money"))}
    )
    filtered = dag.filter(converted, op("eq", ref("year"), value(2021)))
    result = SQLService(db).execute("one", dag.compile(filtered).sql(dialect="postgres"))
    assert result["result"] == [{"year": 2021, "amount": 1234.5}]
    other = SQLService(db).execute("one", dag.compile(converted).sql(dialect="postgres"))
    assert other["result"][1]["amount"] == -5.25


def test_project_keeps_hidden_order_at_terminal_query():
    dag = StageDAG()
    raw = dag.source(
        exp.select("label", "amount").from_("samples"),
        {"label": Column("text", "label"), "amount": Column("number", "amount")},
    )
    target = dag.project(raw, {"answer": ref("label")}, order=[("amount", True)], limit=2)
    sql = dag.compile(target)
    assert sql.args["order"] is not None
    assert sql.args["limit"] is not None
    assert len(sql.expressions) == 1
