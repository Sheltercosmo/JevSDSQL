import threading
import time

import pytest

from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.jev import choice
from sdd.generic.planning_review import ReviewDecisions
from sdd.generic.sql import SQLService


class ConcurrentModel:
    model = "parallel-fixture"

    def __init__(self):
        self.lock = threading.Lock()
        self.active = self.peak = self.calls = 0

    def ask(self, tenant, state, questions):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        time.sleep(state["delay"])
        with self.lock:
            self.active -= 1
        assert tenant == "tenant-a"
        return {
            "model": self.model,
            "answers": {
                key: {"type": "choice", "choice": "a", "probabilities": {"a": 0.6, "b": 0.4}}
                for key in questions
            },
            "usage": {"input_tokens": 1},
        }


def test_parallel_reviews_keep_stable_ids_and_human_corrections(monkeypatch):
    monkeypatch.setenv("SDD_PLANNING_WORKERS", "4")
    model = ConcurrentModel()
    first = ReviewDecisions(model)
    jobs = [
        ({"delay": delay}, {f"slot{i}": choice("Choose", {"a": "First", "b": "Second"})})
        for i, delay in enumerate((0.07, 0.01, 0.02))
    ]
    first.ask_many("tenant-a", jobs)
    assert model.peak == 3
    assert [d["key"] for d in first.decisions] == ["slot0", "slot1", "slot2"]
    old = first.attach({"logical_sql": "SELECT 1", "operation": "select"})
    identity = first.decisions[1]["id"]
    resumed = ReviewDecisions(model, old["_review_state"], {identity: "b"})
    results = resumed.ask_many("tenant-a", jobs)
    assert model.calls == 3
    assert resumed.requests == 0
    assert [d["id"] for d in resumed.decisions] == [d["id"] for d in first.decisions]
    assert results[1]["answers"]["slot1"]["_selection"] == "b"
    assert results[1]["answers"]["slot1"]["probabilities"] == {"a": 0.6, "b": 0.4}
    assert sum(r["usage"]["input_tokens"] for r in results) == 0


def test_serial_ablation_retains_the_same_questions_and_review_ids(monkeypatch):
    jobs = [
        ({"delay": 0.01}, {f"slot{i}": choice("Choose", {"a": "A", "b": "B"})}) for i in range(4)
    ]
    versions = []
    for workers in ("1", "4"):
        monkeypatch.setenv("SDD_PLANNING_WORKERS", workers)
        model = ConcurrentModel()
        review = ReviewDecisions(model)
        review.ask_many("tenant-a", jobs)
        versions.append(review.decisions)
        assert model.peak == int(workers)
    assert versions[0] == versions[1]


def test_case_sensitive_catalog_identifiers_remain_executable_and_authorized():
    db = Database("sqlite://")
    db.initialize()
    catalog = Catalog(db)
    catalog.create("tenant-a", "MixedCase", [{"rowID": 1, "A15": 42}], primary_key=["rowID"])
    sql = 'SELECT "A15" FROM "MixedCase" WHERE "rowID" = 1'
    assert SQLService(db).execute("tenant-a", sql)["result"] == [{"A15": 42}]
    qualified = 'SELECT "MixedCase"."A15" FROM "MixedCase" WHERE "MixedCase"."rowID" = 1'
    assert SQLService(db).execute("tenant-a", qualified)["result"] == [{"A15": 42}]
    with pytest.raises(ValueError):
        SQLService(db).execute("tenant-b", sql)
    db.engine.dispose()
