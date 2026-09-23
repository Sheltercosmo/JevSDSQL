import pytest
from test_planning_review import Model, begin, system as review_system

from sdd.generic import schema
from sdd.generic.planner import Planner
from sdd.generic.sql import SQLService

system = review_system


def test_deleted_source_redacts_planning_candidates(system):
    db, catalog, _, _, reviews = system
    plan = begin(system)
    sql = SQLService(db)
    preview = sql.execute("a", "DELETE FROM readings WHERE id=1", actor="owner")
    sql.commit("a", preview["preview_token"], "owner")
    record = catalog.ledger.get("a", schema.runs, plan["review_id"])
    assert record["plan"] == {} and record["result"] == []
    with pytest.raises(ValueError, match="unavailable"):
        reviews.load("a", "owner", plan["review_id"])


def test_two_stage_average_uses_group_totals_not_row_average(system):
    db, catalog, _, _, _ = system
    catalog.create(
        "a",
        "measurements",
        [
            {"id": 1, "value": 5, "batch": "a"},
            {"id": 2, "value": 15, "batch": "a"},
            {"id": 3, "value": 40, "batch": "b"},
        ],
        primary_key=["id"],
    )

    class NestedModel(Model):
        def ask(self, tenant, state, questions):
            result = super().ask(tenant, state, questions)
            for key, value in {
                "nested_grain": "g2",
                "inner_metric": "metric_c1_sum",
                "outer_metric": "avg",
            }.items():
                if key in questions:
                    result["answers"][key] = {
                        "type": "choice",
                        "choice": value,
                        "probabilities": {
                            option: int(option == value) for option in questions[key]["criteria"]
                        },
                    }
            if "count_rows" in questions:
                result["answers"]["count_rows"]["noul"] = 0.01
            return result

    plan = Planner(db, NestedModel(uncertain=False)).plan(
        "a", "Average totals per batch", ["measurements"]
    )
    result = SQLService(db).execute("a", plan["logical_sql"])
    assert result["result"] == [{"value": 30.0}]


def test_ratio_of_sums_is_weighted_and_handles_zero_denominator(system):
    from sqlglot import exp, parse_one
    from sdd.generic.planner import calculated_metric

    db, catalog, _, _, _ = system
    catalog.create("a", "ratios", [{"a": 1, "b": 2}, {"a": 9, "b": 10}, {"a": 0, "b": 0}])
    metric = calculated_metric(parse_one("a / NULLIF(b, 0)"), "ratio_of_sums")
    query = exp.select(exp.alias_(metric, "ratio")).from_("ratios")
    sql = SQLService(db)
    assert sql.execute("a", query.sql(dialect="postgres"))["result"][0]["ratio"] == pytest.approx(
        10 / 12
    )
    assert sql.execute("a", query.where("b=0").sql(dialect="postgres"))["result"] == [
        {"ratio": None}
    ]


def test_api_correction_requires_confirmation_and_preserves_write_preview(system, monkeypatch):
    from fastapi.testclient import TestClient
    from sdd.api import create_app
    from sdd.execution import Executor
    import sdd.generic.api as api

    db, catalog, _, model, reviews = system
    sql = SQLService(db)
    monkeypatch.setattr(api, "services", lambda executor: (catalog, sql, reviews.planner))
    client = TestClient(
        create_app(
            Executor(db, {}),
            tokens={
                "owner": {"tenant": "a", "name": "owner", "role": "reviewer"},
                "reader": {"tenant": "a", "name": "reader", "role": "reader"},
            },
        )
    )
    headers = {"Authorization": "Bearer owner"}
    response = client.post(
        "/ask", headers=headers, json={"question": "Count readings with amount above 10"}
    )
    assert response.status_code == 422 and response.json()["executed"] is False
    plan = response.json()["plan"]
    failed = plan["review"]["failed_decision"]
    decision = next(d for d in plan["review"]["decisions"] if d["id"] == failed)
    body = {"review_id": plan["review_id"], "corrections": {failed: decision["selected"]}}
    assert (
        client.post(
            "/ask/review", headers={"Authorization": "Bearer reader"}, json=body
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/ask/review", headers=headers, json={**body, "corrections": {failed: 1}}
        ).status_code
        == 422
    )
    result = client.post("/ask/review", headers=headers, json=body)
    assert result.status_code == 200 and result.json()["executed"] is False
    reviewed = result.json()["plan"]
    confirmed = client.post(
        "/ask/confirm", headers=headers, json={"review_id": reviewed["review_id"]}
    )
    assert confirmed.status_code == 200 and confirmed.json()["result"] == [{"metric_1": 2}]
    stored = reviews.load("a", "owner", reviewed["review_id"])
    stored.update(operation="update", logical_sql="UPDATE readings SET amount=99 WHERE id=1")
    write = reviews.save("a", "owner", stored)
    preview = client.post("/ask/confirm", headers=headers, json={"review_id": write["review_id"]})
    assert preview.status_code == 200 and preview.json()["mutation_preview"]
    assert sql.execute("a", "SELECT amount FROM readings WHERE id=1")["result"] == [{"amount": 5}]
    reader_plan = reviews.save("a", "reader", stored)
    assert (
        client.post(
            "/ask/confirm",
            headers={"Authorization": "Bearer reader"},
            json={"review_id": reader_plan["review_id"]},
        ).status_code
        == 403
    )
    committed = client.post(
        "/data/mutations/" + preview.json()["preview_token"] + "/commit", headers=headers
    )
    assert committed.status_code == 200
    assert sql.execute("a", "SELECT amount FROM readings WHERE id=1")["result"] == [{"amount": 99}]


def test_cli_returns_structured_review_instead_of_a_traceback(system, monkeypatch, capsys):
    import json
    import sdd.cli as cli
    import sdd.generic.api as api
    from sdd.execution import Executor

    db, catalog, _, _, reviews = system
    executor = Executor(db, {})
    monkeypatch.setattr(cli, "runtime", lambda: (db, executor))
    monkeypatch.setattr(
        api, "services", lambda executor: (catalog, SQLService(db), reviews.planner)
    )
    monkeypatch.setenv("SDD_API_TOKENS", json.dumps({"test": {"tenant": "a", "name": "owner"}}))
    monkeypatch.setattr("sys.argv", ["sdd", "ask", "Count readings with amount above 10"])
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output["review_required"] and output["executed"] is False
    assert output["history_id"]
    assert output["plan"]["review_id"]
    assert "_review_state" not in output["plan"]
