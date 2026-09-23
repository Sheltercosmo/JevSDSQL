import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_planning_review import system as review_system

from sdd.api import create_app
from sdd.execution import Executor
from sdd.generic import schema
from sdd.generic.history import QueryHistory
from sdd.generic.sql import SQLService

system = review_system


@pytest.fixture
def client(system, monkeypatch):
    import sdd.generic.api as api

    db, catalog, _, _, reviews = system
    monkeypatch.setattr(
        api, "services", lambda executor: (catalog, SQLService(db), reviews.planner)
    )
    return TestClient(
        create_app(
            Executor(db, {}),
            tokens={
                "owner": {"tenant": "a", "name": "owner", "role": "reviewer"},
                "peer": {"tenant": "a", "name": "peer", "role": "reviewer"},
                "other": {"tenant": "b", "name": "owner", "role": "reviewer"},
            },
        )
    )


OWNER = {"Authorization": "Bearer owner"}


def post(client, path, body):
    return client.post(path, headers=OWNER, json=body)


def detail(client, identity):
    response = client.get("/query-history/" + identity, headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()


def test_query_revisions_preserve_original_and_paginate(client):
    original = post(
        client, "/data/sql", {"sql": "SELECT amount FROM readings WHERE amount>20"}
    ).json()
    identity = original["history_id"]
    saved = detail(client, identity)
    assert saved["output"]["result"] == [{"amount": 30}]
    child = post(
        client,
        "/data/sql",
        {
            "sql": "SELECT amount FROM readings WHERE amount>10 ORDER BY amount",
            "parent_history_id": identity,
        },
    ).json()
    assert child["result"] == [{"amount": 20}, {"amount": 30}]
    assert detail(client, child["history_id"])["parent_id"] == identity
    assert detail(client, identity) == saved
    page = client.get("/query-history?limit=1", headers=OWNER).json()
    assert page["items"][0]["id"] == child["history_id"]
    older = client.get("/query-history?limit=1&before=" + page["next_cursor"], headers=OWNER).json()
    assert older["items"][0]["id"] == identity and older["next_cursor"] is None
    assert client.get("/query-history?limit=1000", headers=OWNER).status_code == 422


def test_failed_sql_is_editable_without_error_parameter_leaks(client):
    failed = post(client, "/data/sql", {"sql": "SELECT unavailable FROM readings"})
    assert failed.status_code == 400
    record = client.get("/query-history", headers=OWNER).json()["items"][0]
    saved = detail(client, record["id"])
    assert (
        saved["status"] == "error" and saved["input"]["text"] == "SELECT unavailable FROM readings"
    )
    assert "parameters" not in saved and "Traceback" not in str(saved)
    corrected = post(
        client,
        "/data/sql",
        {"sql": "SELECT COUNT(*) AS n FROM readings", "parent_history_id": record["id"]},
    )
    assert corrected.json()["result"] == [{"n": 3}]


def test_history_is_bound_to_actor_and_tenant(client):
    identity = post(client, "/data/sql", {"sql": "SELECT id FROM readings"}).json()["history_id"]
    for token in ("peer", "other"):
        headers = {"Authorization": "Bearer " + token}
        assert client.get("/query-history", headers=headers).json()["items"] == []
        assert client.get("/query-history/" + identity, headers=headers).status_code == 400
        assert client.get("/query-history?before=" + identity, headers=headers).status_code == 400
        assert (
            client.post(
                "/data/sql",
                headers=headers,
                json={
                    "sql": "UPDATE readings SET amount=99 WHERE id=1",
                    "parent_history_id": identity,
                },
            ).status_code
            == 400
        )


def test_expired_history_decisions_reopen_without_execution_or_model_calls(client, system):
    db, _, _, model, reviews = system
    response = post(
        client, "/ask", {"question": "Count readings with amount above 10", "max_evaluations": 12}
    )
    assert response.status_code == 422
    history_id, old_review = response.json()["history_id"], response.json()["review_id"]
    stored = reviews.load("a", "owner", old_review)
    stored["_expires_at"] = 0
    with db.transaction("a") as connection:
        connection.execute(
            update(schema.runs).where(schema.runs.c.id == old_review).values(plan=stored)
        )
    calls = model.calls
    opened = post(client, "/query-history/" + history_id + "/review", {}).json()
    assert opened["executed"] is False and model.calls == calls
    plan = opened["plan"]
    assert plan["review_id"] != old_review and "_review_state" not in plan
    decision = next(
        d for d in plan["review"]["decisions"] if d["id"] == plan["review"]["failed_decision"]
    )
    rebuilt = post(
        client,
        "/ask/review",
        {
            "review_id": plan["review_id"],
            "corrections": {decision["id"]: decision["selected"]},
            "parent_history_id": history_id,
        },
    ).json()
    child = rebuilt["history_id"]
    assert child != history_id and detail(client, child)["status"] == "plan"
    confirmed = post(
        client, "/ask/confirm", {"review_id": rebuilt["review_id"], "parent_history_id": child}
    ).json()
    assert confirmed["history_id"] == child and confirmed["result"] == [{"metric_1": 2}]
    assert detail(client, child)["status"] == "complete" and detail(client, child)["has_decisions"]
    assert detail(client, history_id)["status"] == "review"


def test_reopening_changed_catalog_keeps_editable_input_but_rejects_old_decisions(client, system):
    db, _, dataset, _, _ = system
    attempt = post(client, "/ask", {"question": "Count readings with amount above 10"}).json()
    with db.transaction("a") as connection:
        connection.execute(
            update(schema.datasets)
            .where(schema.datasets.c.id == dataset["id"])
            .values(description="New field meaning")
        )
    identity = attempt["history_id"]
    assert detail(client, identity)["input"]["text"] == "Count readings with amount above 10"
    assert post(client, "/query-history/" + identity + "/review", {}).status_code == 400


def test_saved_write_has_no_replayable_commit_action(client):
    preview = post(client, "/data/sql", {"sql": "UPDATE readings SET amount=9 WHERE id=1"}).json()
    identity = preview["history_id"]
    saved = detail(client, identity)
    assert saved["status"] == "preview" and "preview_token" not in str(saved)
    assert "mutation_preview" not in saved["output"]
    fresh = post(
        client,
        "/data/sql",
        {"sql": "UPDATE readings SET amount=8 WHERE id=1", "parent_history_id": identity},
    ).json()
    assert fresh["preview_token"] != preview["preview_token"]
    assert (
        post(client, "/data/mutations/" + fresh["preview_token"] + "/commit", {}).status_code == 200
    )
    assert detail(client, fresh["history_id"])["status"] == "committed"
    assert detail(client, identity)["status"] == "preview"


def test_source_deletion_redacts_history_input_and_sql(client):
    identity = post(client, "/data/sql", {"sql": "SELECT amount FROM readings WHERE id=1"}).json()[
        "history_id"
    ]
    preview = post(client, "/data/sql", {"sql": "DELETE FROM readings WHERE id=1"}).json()
    post(client, "/data/mutations/" + preview["preview_token"] + "/commit", {})
    assert client.get("/query-history/" + identity, headers=OWNER).status_code == 400
    items = client.get("/query-history", headers=OWNER).json()["items"]
    assert all(i["status"] == "redacted" and i["title"] == "" for i in items)


def test_legacy_import_only_uses_attributable_records(client, system):
    db, _, _, model, reviews = system
    model.uncertain = False
    plan = reviews.begin("a", "owner", "Count readings", ["readings"])
    SQLService(db).execute(
        "a", plan["logical_sql"], plan=plan, request=plan["request"], actor="owner"
    )
    SQLService(db).execute("a", "SELECT * FROM readings")
    history = QueryHistory(db)
    assert history.import_owned_runs("a", "peer") == 0
    assert history.import_owned_runs("a", "owner") == 1
    assert history.import_owned_runs("a", "owner") == 0
    imported = history.recent("a", "owner")["items"][0]
    assert imported["status"] == "complete" and imported["title"] == "Count readings"


def test_execution_failure_keeps_generated_sql_for_editing(client, system, monkeypatch):
    _, _, _, model, _ = system
    model.uncertain = False

    def reject(*args, **kwargs):
        raise ValueError("Execution could not finish")

    monkeypatch.setattr(SQLService, "execute", reject)
    response = post(client, "/ask", {"question": "Count readings"})
    assert response.status_code == 400
    identity = client.get("/query-history", headers=OWNER).json()["items"][0]["id"]
    saved = detail(client, identity)
    assert saved["status"] == "error" and "COUNT" in saved["output"]["logical_sql"]
    assert saved["has_decisions"]
