import pytest
from fastapi.testclient import TestClient
from sdd.db import Database
from sdd.execution import Executor
from sdd.api import create_app


@pytest.fixture
def api(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "api.db"))
    db.initialize()
    client = TestClient(
        create_app(
            Executor(db, {}),
            tokens={
                "reviewer": {"tenant": "a", "name": "r", "role": "reviewer"},
                "reader": {"tenant": "a", "name": "u", "role": "reader"},
                "other": {"tenant": "b", "name": "o", "role": "reviewer"},
            },
        )
    )
    h = {"Authorization": "Bearer reviewer"}
    r = client.post(
        "/datasets",
        json={"name": "测量", "rows": [{"样本": 1, "数值": 2}], "primary_key": ["样本"]},
        headers=h,
    )
    assert r.status_code == 200
    return client, h, r.json()["id"]


def test_catalog_and_auth(api):
    c, h, d = api
    assert c.get("/datasets").status_code in (401, 403)
    assert len(c.get("/datasets", headers=h).json()["datasets"]) == 1
    assert c.get("/datasets", headers={"Authorization": "Bearer other"}).json()["datasets"] == []
    assert (
        c.get("/datasets/" + d + "/rows", headers={"Authorization": "Bearer other"}).status_code
        == 400
    )
    assert (
        c.post(
            "/datasets",
            headers={"Authorization": "Bearer reader"},
            json={"name": "x", "rows": [{"v": 1}]},
        ).status_code
        == 403
    )


def test_generic_query_and_controlled_write(api):
    c, h, _ = api
    q = c.post("/data/sql", headers=h, json={"sql": 'SELECT SUM("数值") AS total FROM "测量"'})
    assert q.status_code == 200 and q.json()["result"] == [{"total": 2}]
    request = {"sql": 'UPDATE "测量" SET "数值"=4 WHERE "样本"=1'}
    assert (
        c.post("/data/sql", headers={"Authorization": "Bearer reader"}, json=request).status_code
        == 403
    )
    p = c.post("/data/sql", headers=h, json=request).json()
    assert p["mutation_preview"] and p["affected_rows"] == 1
    route = "/data/mutations/" + p["preview_token"] + "/commit"
    assert c.post(route, headers={"Authorization": "Bearer reader"}).status_code == 403
    assert c.post(route, headers=h).json()["manifest"]["committed"]
    assert c.post(route, headers=h).status_code == 400


def test_generic_ui_and_validation(api):
    c, h, _ = api
    for route in ("/ask", "/ask/en", "/ask/zh"):
        page = c.get(route)
        assert page.status_code == 200 and "locales.js" in page.text and "explore.js" in page.text
        assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert c.post("/ask", headers=h, json={"question": "test", "tenant": "b"}).status_code == 422
    assert c.post("/ask", headers=h, json={"question": "test"}).status_code == 503


def test_feature_review_permissions_and_tenant_scope(api):
    client, reviewer, _ = api
    dataset = client.post(
        "/datasets",
        headers=reviewer,
        json={"name": "Notes", "rows": [{"id": 1, "body": "A request"}], "primary_key": ["id"]},
    ).json()
    payload = {
        "dataset_id": dataset["id"],
        "name": "needs_action",
        "column": "body",
        "definition": "Requests an action",
    }
    reader = {"Authorization": "Bearer reader"}
    other = {"Authorization": "Bearer other"}
    assert client.post("/features", headers=reader, json=payload).status_code == 403
    feature = client.post("/features", headers=reviewer, json=payload)
    assert feature.status_code == 200
    identity = feature.json()["id"]
    review = {
        "status": "active",
        "reason": "Reviewed fixture",
        "examples": [{"text": "A request", "expected": True}],
    }
    assert client.get("/features", headers=other).json()["features"] == []
    assert (
        client.post(f"/features/{identity}/review", headers=other, json=review).status_code == 400
    )
    assert (
        client.post(f"/features/{identity}/review", headers=reader, json=review).status_code == 403
    )
    assert (
        client.post(f"/features/{identity}/review", headers=reviewer, json=review).json()["status"]
        == "active"
    )
    assert (
        client.post(
            f"/features/{identity}/assertions",
            headers=reader,
            json={"primary_key": {"id": 1}, "value": True, "reason": "Checked"},
        ).status_code
        == 403
    )
    assert client.post(f"/features/{identity}/preview", headers=reader, json={}).status_code == 503
