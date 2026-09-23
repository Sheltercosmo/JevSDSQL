import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from sdd.api import create_app
from sdd.execution import Executor
from sdd.operators import schema
from sdd.operators.maintenance import work_one
from test_operator_runtime import operators as operator_fixture

operators = operator_fixture


@pytest.fixture
def client(operators, monkeypatch):
    db, model, _ = operators
    from sdd.generic.api import services

    original = services

    def configured(executor):
        catalog, sql, planner = original(executor)
        sql.decisions = model
        return catalog, sql, planner

    monkeypatch.setattr("sdd.generic.api.services", configured)
    tokens = {
        "reviewer": {"tenant": "tenant-a", "name": "reviewer-a", "role": "reviewer"},
        "reader": {"tenant": "tenant-a", "name": "reader", "role": "reader"},
        "other": {"tenant": "other", "name": "other", "role": "reviewer"},
    }
    return TestClient(create_app(Executor(db, {}), tokens=tokens))


def headers(token="reviewer"):
    return {"Authorization": "Bearer " + token}


def test_routes_authentication_and_three_states(client):
    assert client.get("/jev/operators").status_code in (401, 403)
    assert len(client.get("/jev/operators", headers=headers()).json()["functions"]) == 41
    body = {"operator": "NOUL", "arguments": {"state": "x", "proposition": "no"}}
    response = client.post("/jev/call", headers=headers(), json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert next(iter(result["observations"].values()))["value"] is False
    assert client.get("/jev/runs/" + result["run_id"], headers=headers("other")).status_code == 400
    assert client.post("/jev/approve", headers=headers("reader"), json=body).status_code == 403
    assert (
        client.post(
            "/jev/call",
            headers=headers("reader"),
            json={"operator": "REVIEW", "arguments": {"observations": []}},
        ).status_code
        == 403
    )


def test_resume_keeps_remaining_budget_and_is_single_use(client, operators):
    _, model, _ = operators
    body = {
        "operator": "TAG",
        "arguments": {"subjects": ["a", "b"], "concept_revs": ["p"]},
        "limits": {"max_judgments": 1, "concurrency": 1},
    }
    result = client.post("/jev/call", headers=headers(), json=body).json()
    route = "/jev/runs/" + result["run_id"] + "/resume"
    assert client.post(route, headers=headers("reader")).status_code == 403
    resumed = client.post(route, headers=headers())
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["manifest"]["reserved_requests"] == 0
    assert resumed.json()["manifest"]["not_evaluated"] == 1
    assert model.calls == 1
    assert client.post(route, headers=headers()).status_code == 400


def test_approval_is_bound_to_source_population(client, operators):
    db, model, service = operators
    dataset = service.catalog.create(
        service.tenant, "Items", [{"id": 1, "text": "Before"}], primary_key=["id"]
    )
    body = {
        "operator": "TAG",
        "arguments": {"subjects": {"dataset_id": dataset["id"]}, "concept_revs": ["p"]},
    }
    approval = client.post("/jev/approve", headers=headers(), json=body).json()
    with db.transaction(service.tenant) as connection:
        connection.execute(update(service.catalog.table(dataset, connection)).values(text="After"))
    result = client.post(
        "/jev/call", headers=headers(), json={**body, "approval_id": approval["approval_id"]}
    )
    assert result.status_code == 400 and model.calls == 0


def test_change_subscription_is_bounded_and_reuses_unchanged_rows(operators):
    db, model, service = operators
    definition = service.store.add(
        schema.definitions,
        name="Issue",
        owner="reviewer-a",
        definition={"instructions": "p"},
        status="ACTIVE",
        validation={"reviewed": True},
    )
    dataset = service.catalog.create(
        service.tenant,
        "Items",
        [{"id": 1, "text": "Before"}, {"id": 2, "text": "Unchanged"}],
        primary_key=["id"],
    )
    materialized = service.call(
        "MATERIALIZE",
        {
            "concept_rev": definition["id"],
            "target_scope": {"dataset_id": dataset["id"]},
            "refresh_policy": {"mode": "on_change", "max_refreshes": 1, "interval_seconds": 30},
        },
    )
    assert materialized["value"]["published"] and model.calls == 2
    with db.transaction(service.tenant) as connection:
        table = service.catalog.table(dataset, connection)
        connection.execute(update(table).where(table.c.id == 1).values(text="After"))
        connection.execute(
            update(schema.subscriptions)
            .where(schema.subscriptions.c.tenant == service.tenant)
            .values(next_check=time.time() - 1)
        )
    assert work_one(db, model, service.tenant)
    assert model.calls == 3
    with db.transaction(service.tenant) as connection:
        subscription = connection.execute(select(schema.subscriptions)).mappings().one()
        assert subscription["remaining"] == 0 and subscription["last_state"] == "PUBLISHED"
    assert not work_one(db, model, service.tenant)


def test_extraction_approval_uses_same_default_budget_and_policy(client):
    body = {
        "operator": "EXTRACT_TABLE",
        "arguments": {
            "text": "Value 12",
            "row_description": "One measurement",
            "columns": [{"name": "v", "description": "Value", "type": "integer"}],
        },
        "limits": {"max_judgments": 0},
    }
    approved = client.post("/jev/approve", headers=headers(), json=body)
    assert approved.status_code == 200, approved.text
    response = client.post(
        "/jev/call", headers=headers(), json={**body, "approval_id": approved.json()["approval_id"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["operation_state"] == "BLOCKED_BY_BUDGET"
