"""Real PostgreSQL checks for generic tenant tables, decimals, and concurrent commits."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select, delete
from sqlalchemy.exc import DBAPIError
from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService
from sdd.generic import schema as s

pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_POSTGRES_URL"), reason="PostgreSQL test URL not configured"
)


@pytest.fixture
def pgdata():
    db = Database(os.environ["SDD_TEST_POSTGRES_URL"])
    tenant = "generic-test-" + uuid.uuid4().hex
    catalog = Catalog(db)
    sql = SQLService(db)
    d = catalog.create(
        tenant,
        "账户",
        [
            {"编号": 1, "余额": "12.30", "日期": "2026-09-21"},
            {"编号": 2, "余额": "5.25", "日期": "2026-08-15"},
        ],
        columns=[
            {"name": "编号", "type": "integer"},
            {"name": "余额", "type": "number"},
            {"name": "日期", "type": "date"},
        ],
        primary_key=["编号"],
    )
    yield db, tenant, catalog, sql, d
    # Only this fixture's randomly named tenant-owned tables are removed.
    with db.transaction(tenant) as cx:
        for item in sorted(catalog.list(tenant), key=lambda d: d["created_at"], reverse=True):
            catalog.table(item, cx).drop(cx)
        cx.execute(delete(s.datasets).where(s.datasets.c.tenant == tenant))
        cx.execute(delete(s.previews).where(s.previews.c.tenant == tenant))
        cx.execute(delete(s.runs).where(s.runs.c.tenant == tenant))
        cx.execute(delete(s.query_history).where(s.query_history.c.tenant == tenant))


def test_generic_pg_forced_rls(pgdata):
    db, tenant, cat, sql, d = pgdata
    with db.transaction(tenant) as cx:
        table = cat.table(d, cx)
        assert len(cx.execute(select(table)).all()) == 2
    with db.transaction(tenant + "-other") as cx:
        assert cx.execute(select(table)).all() == []
        assert cx.execute(select(s.datasets)).all() == []
    with pytest.raises(ValueError):
        sql.execute(tenant + "-other", 'SELECT * FROM "账户"')


def test_decimal_date_and_cte(pgdata):
    _, tenant, _, sql, _ = pgdata
    r = sql.execute(tenant, 'WITH x AS (SELECT * FROM "账户") SELECT SUM("余额") AS total FROM x')
    from decimal import Decimal

    assert Decimal(r["result"][0]["total"]) == Decimal("17.55")
    r = sql.execute(
        tenant,
        'SELECT EXTRACT(MONTH FROM "日期") AS month FROM "账户" WHERE "日期">=\'2026-09-01\'',
    )
    assert Decimal(r["result"][0]["month"]) == 9
    r = sql.execute(
        tenant, 'SELECT DATE_TRUNC(\'month\', "日期") AS month FROM "账户" ORDER BY month'
    )
    assert len(r["result"]) == 2


def test_two_reviewers_cannot_commit_same_preview(pgdata):
    _, tenant, _, sql, _ = pgdata
    p = sql.execute(tenant, 'UPDATE "账户" SET "余额"="余额"+1 WHERE "编号"=1', actor="r")

    def commit():
        try:
            return sql.commit(tenant, p["preview_token"], "r")["manifest"]["committed"]
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: commit(), range(2))) == [False, True]
    assert (
        sql.execute(tenant, 'SELECT "余额" FROM "账户" WHERE "编号"=1')["result"][0]["余额"]
        == "13.3000000000"
    )


def test_constraints_and_failed_commit_roll_back(pgdata):
    _, tenant, cat, sql, d = pgdata
    cat.create(tenant, "ledger", [{"id": 1, "account": 1}], primary_key=["id"])
    cat.link(tenant, "ledger", d["id"], "account", "编号")
    p = sql.execute(tenant, 'DELETE FROM "账户" WHERE "编号"=1', actor="r")
    with pytest.raises(DBAPIError):
        sql.commit(tenant, p["preview_token"], "r")
    assert len(sql.execute(tenant, 'SELECT * FROM "账户"')["result"]) == 2
    assert cat.ledger.get(tenant, s.previews, p["preview_token"])["state"] == "pending"


def test_read_only_and_numeric_division(pgdata):
    _, tenant, cat, sql, _ = pgdata
    cat.create(tenant, "readings", [{"n": 3, "d": 2}], writable=False)
    r = sql.execute(tenant, "SELECT n::numeric/NULLIF(d,0) AS ratio FROM readings")
    assert float(r["result"][0]["ratio"]) == 1.5
    with pytest.raises(ValueError, match="read-only"):
        sql.execute(tenant, "DELETE FROM readings WHERE n=3")


def test_observed_snapshot_immutable_in_postgres(pgdata):
    db, tenant, _, sql, d = pgdata
    from sdd.ledger import uid, now
    from sqlalchemy import insert, update

    identity = uid()
    with db.transaction(tenant) as cx:
        cx.execute(
            insert(s.row_versions).values(
                id=identity,
                tenant=tenant,
                dataset_id=d["id"],
                row_key="k",
                row_hash="h",
                value={"v": 1},
                created_at=now(),
            )
        )
    with pytest.raises(DBAPIError):
        with db.transaction(tenant) as cx:
            cx.execute(
                update(s.row_versions).where(s.row_versions.c.id == identity).values(value={"v": 2})
            )


def test_distinct_composite_entity_key(pgdata):
    _, tenant, _, sql, _ = pgdata
    r = sql.execute(tenant, 'SELECT COUNT(DISTINCT ("编号","日期")) AS n FROM "账户"')
    assert r["result"] == [{"n": 2}]


class SemanticFixture:
    model = "pg-semantic-fixture-v1"

    def ask(self, tenant, state, questions):
        return {
            "model": self.model,
            "answers": {
                key: {"type": "noul", "noul": 0.95 if "yes" in state["subject"] else 0.05}
                for key in questions
            },
            "usage": {"input_tokens": 10, "output_tokens": len(questions)},
        }


def test_pg_semantic_grouping_and_evidence_guards(pgdata):
    from sqlalchemy import update
    from sdd.generic.features import FeatureRegistry

    db, tenant, catalog, _, _ = pgdata
    dataset = catalog.create(
        tenant, "texts", [{"id": 1, "body": "yes"}, {"id": 2, "body": "no"}], primary_key=["id"]
    )
    registry = FeatureRegistry(db)
    feature = registry.create(
        tenant, "reviewer", dataset["id"], "approved", "body", "Affirmative statement"
    )
    registry.review(
        tenant,
        feature["id"],
        "reviewer",
        "active",
        "Checked fixture",
        [{"text": "yes", "expected": True}],
    )
    sql = SQLService(db, SemanticFixture())
    result = sql.execute(
        tenant,
        "SELECT SEMANTIC_FEATURE(body, 'approved') AS approved, COUNT(*) AS n FROM texts GROUP BY SEMANTIC_FEATURE(body, 'approved')",
    )
    assert {row["approved"]: row["n"] for row in result["result"]} == {True: 1, False: 1}
    # Equal literals in unrelated SQL contexts must still receive independently typed parameters.
    assert sql.execute(
        tenant, "SELECT CAST('2026-09-22' AS DATE) AS day, '2026-09-22' AS label FROM texts LIMIT 1"
    )["result"]
    assertion = registry.assert_value(
        tenant, feature["id"], {"id": 1}, False, "reviewer", "Correction"
    )
    with db.transaction(tenant) as connection:
        payload = connection.execute(select(s.payloads)).mappings().first()
    for table, identity, changes in (
        (s.features, feature["id"], {"definition": {}}),
        (s.payloads, payload["id"], {"answer": {}}),
        (s.feature_reviews, assertion["id"], {"value": True}),
    ):
        with pytest.raises(DBAPIError):
            with db.transaction(tenant) as connection:
                connection.execute(update(table).where(table.c.id == identity).values(**changes))
    with db.transaction(tenant + "-other") as connection:
        for table in (
            s.features,
            s.payloads,
            s.inference_calls,
            s.feature_reviews,
            s.maintenance_jobs,
        ):
            assert connection.execute(select(table)).all() == []


def test_native_reads_exceed_previous_limit_without_python_snapshots(pgdata, monkeypatch):
    db, tenant, catalog, service, _ = pgdata
    dataset = catalog.create(
        tenant,
        "large_readings",
        [],
        columns=[{"name": "id", "type": "integer"}],
        primary_key=["id"],
    )
    from sqlalchemy import text

    with db.transaction(tenant) as connection:
        table = catalog.table(dataset, connection)
        quoted = connection.dialect.identifier_preparer.format_table(table)
        connection.execute(text("INSERT INTO " + quoted + " SELECT generate_series(1, 50002)"))

    def forbidden(*args, **kwargs):
        raise AssertionError("Relational reads must not materialize source rows in Python")

    monkeypatch.setattr(service, "snapshots", forbidden)
    result = service.execute(tenant, "SELECT COUNT(*) AS n, SUM(id) AS total FROM large_readings")
    assert result["result"] == [{"n": 50002, "total": "1250125003"}]
    assert result["manifest"]["source_rows"] == 50002
    assert result["manifest"]["snapshot_mode"] == "postgres_repeatable_read"
    assert result["manifest"]["complete"]
    limited = service.execute(tenant, "SELECT id FROM large_readings ORDER BY id")
    assert len(limited["result"]) == 1000 and limited["manifest"]["truncated"]
    with pytest.raises(ValueError):
        service.execute(tenant + "-outside", "SELECT * FROM large_readings")


def test_native_snapshot_stays_consistent_during_concurrent_insert(pgdata):
    db, tenant, catalog, service, dataset = pgdata
    from sqlalchemy import event, insert

    inserted = False

    def concurrent_insert(connection, cursor, statement, parameters, context, executemany):
        nonlocal inserted
        if "SELECT COUNT(*) FROM" in statement and not inserted:
            inserted = True
            with db.transaction(tenant) as other:
                other.execute(
                    insert(catalog.table(dataset, other)),
                    {"编号": 3, "余额": "100", "日期": "2026-09-22"},
                )

    event.listen(db.engine, "after_cursor_execute", concurrent_insert)
    try:
        result = service.execute(tenant, 'SELECT COUNT(*) AS n FROM "账户"')
        assert result["manifest"]["source_rows"] == 2
        assert result["result"] == [{"n": 2}]
    finally:
        event.remove(db.engine, "after_cursor_execute", concurrent_insert)
    assert service.execute(tenant, 'SELECT COUNT(*) AS n FROM "账户"')["result"] == [{"n": 3}]


def test_ordered_set_percentiles_are_guarded_and_executable(pgdata):
    _, tenant, _, service, _ = pgdata
    result = service.execute(
        tenant,
        'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "余额") AS median, PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY "余额") AS discrete FROM "账户"',
    )
    assert float(result["result"][0]["median"]) == pytest.approx(8.775)
    assert float(result["result"][0]["discrete"]) == 5.25
    with pytest.raises(ValueError):
        service.execute(tenant, "SELECT pg_read_file('/tmp/private') FROM \"账户\"")


def test_history_postgres_actor_scope_pagination_and_rls(pgdata):
    from sdd.generic.history import QueryHistory

    db, tenant, _, sql, _ = pgdata
    history = QueryHistory(db)
    query = 'SELECT "编号" FROM "账户" ORDER BY "编号"'
    first = history.capture(
        tenant, "owner", {"mode": "sql", "text": query}, lambda: sql.execute(tenant, query)
    )
    second = history.capture(
        tenant,
        "owner",
        {"mode": "sql", "text": query},
        lambda: sql.execute(tenant, query),
        parent_id=first["history_id"],
    )
    assert history.recent(tenant, "owner", 1)["items"][0]["id"] == second["history_id"]
    assert (
        history.recent(tenant, "owner", 1, second["history_id"])["items"][0]["id"]
        == first["history_id"]
    )
    assert history.recent(tenant, "other")["items"] == []
    with db.transaction(tenant + "-other") as connection:
        assert connection.execute(select(s.query_history)).all() == []
    with pytest.raises(ValueError):
        history.get(tenant + "-other", "owner", first["history_id"])


@pytest.mark.parametrize("literal", ["100.0", "1e2", "100.000"])
def test_decimal_literal_preserves_fractional_division(pgdata, literal):
    from decimal import Decimal

    _, tenant, _, sql, _ = pgdata
    result = sql.execute(
        tenant, f'SELECT {literal} * "编号" / 3 AS share FROM "账户" ORDER BY "编号"'
    )
    assert abs(Decimal(result["result"][0]["share"]) - Decimal(100) / 3) < Decimal("1e-10")
    assert abs(Decimal(result["result"][1]["share"]) - Decimal(200) / 3) < Decimal("1e-10")


def test_integer_literal_retains_integer_division(pgdata):
    _, tenant, _, sql, _ = pgdata
    result = sql.execute(tenant, 'SELECT 100 * "编号" / 3 AS share FROM "账户" ORDER BY "编号"')
    assert [r["share"] for r in result["result"]] == [33, 66]


@pytest.mark.parametrize("function, expected", [("BOOL_AND", False), ("BOOL_OR", True)])
def test_postgres_boolean_aggregate_aliases(pgdata, function, expected):
    _, tenant, _, sql, _ = pgdata
    result = sql.execute(tenant, f'SELECT {function}("编号" > 1) AS matched FROM "账户"')
    assert result["result"] == [{"matched": expected}]


def test_typed_stage_dag_decimal_scope_and_tenant_binding(pgdata):
    from decimal import Decimal
    from sqlglot import exp
    from sdd.generic.stage_dag import Column, StageDAG, operation as op, ref, value

    _, tenant, _, sql, _ = pgdata
    dag = StageDAG()
    source = dag.source(
        exp.select('"编号" AS id', '"余额" AS amount').from_('"账户"'),
        {"id": Column("integer", "编号", unit="identifier"), "amount": Column("number", "余额")},
        keys=[("id",)],
    )
    mean = dag.aggregate(source, [], {"mean": op("avg", ref("amount"))})
    joined = dag.join(
        source,
        mean,
        [],
        how="cross",
        outputs={"id": ref("l.id"), "amount": ref("l.amount"), "mean": ref("r.mean")},
    )
    distances = dag.project(
        joined, {"distance": op("abs", op("sub", ref("amount"), ref("mean")))}, keep=True
    )
    ranked = dag.window(distances, "rank", op("rank"), partition=[], order=[("distance", False)])
    winners = dag.filter(ranked, op("le", ref("rank"), value(1)))
    result = sql.execute(tenant, dag.compile(winners).sql(dialect="postgres"))["result"]
    assert sorted(row["id"] for row in result) == [1, 2]
    assert {Decimal(row["mean"]) for row in result} == {Decimal("8.775")}
    assert len(dag.describe(winners)["shared_stages"]) == 1


def test_business_rule_math_and_json_are_portable(pgdata):
    from sqlglot import exp
    from sdd.generic.stage_dag import operation as op, ref, value

    _, tenant, catalog, service, _ = pgdata
    catalog.create(
        tenant,
        "metrics",
        [{"id": 1, "payload": {"rate": 1.2345}}],
        columns=[{"name": "id", "type": "integer"}, {"name": "payload", "type": "json"}],
        primary_key=["id"],
    )
    term = op(
        "round",
        op("mul", op("number", op("json_text", ref("payload"), value("rate"))), value(2)),
        value(2),
    )
    query = exp.select(exp.alias_(term.sql(), "answer")).from_("metrics").sql(dialect="postgres")
    result = service.execute(tenant, query)["result"]
    assert float(result[0]["answer"]) == 2.47
    query = (
        exp.select(exp.alias_(op("log10", value(1000)).sql(), "answer"))
        .from_("metrics")
        .sql(dialect="postgres")
    )
    assert float(service.execute(tenant, query)["result"][0]["answer"]) == 3.0


def test_mixed_population_windows_share_row_identity_on_postgres(pgdata):
    from sqlglot import exp
    from sdd.generic.knowledge import RuleGraph
    from sdd.generic.rule_stages import compile_values
    from sdd.generic.stage_dag import Column, StageDAG, operation as op, ref, value

    _, tenant, _, service, _ = pgdata
    dag = StageDAG()
    columns = {"amount": Column("number", "Balance")}
    graph = RuleGraph({}, {}, dict(columns))
    rank = graph.register_window("percent_rank", ref("amount"))
    mean = graph.register_window("avg", ref("amount"))
    source = dag.source(
        exp.select(exp.alias_(exp.column("余额", quoted=True), "amount", quoted=True)).from_(
            exp.Table(this=exp.to_identifier("账户", quoted=True))
        ),
        columns,
    )
    node = compile_values(
        dag,
        source,
        graph,
        {"w0", "w1"},
        restrictions=[op("gt", ref("amount"), value(6))],
        scopes={"w1": (0,)},
    )
    node = dag.project(node, {"amount": ref("amount"), "rank": rank, "mean": mean})
    rows = service.execute(tenant, dag.compile(node).sql(dialect="postgres"))["result"]
    rows = sorted(rows, key=lambda row: float(row["amount"]))
    assert [float(row["rank"]) for row in rows] == [0, 1]
    assert rows[0]["mean"] is None
    assert float(rows[1]["mean"]) == pytest.approx(12.3)
