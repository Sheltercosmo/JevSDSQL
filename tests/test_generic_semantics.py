import pytest
from sqlalchemy import select, update
from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService
from sdd.generic import schema as s
from sdd.evaluators import ProviderError


class Decisions:
    model = "fixture-generic-v1"

    def __init__(self):
        self.calls = 0
        self.values = {"yes": 0.95, "no": 0.05, "maybe": 0.5}
        self.hook = None

    def ask(self, tenant, state, questions):
        self.calls += 1
        if self.hook:
            self.hook()
        value = self.values[state["subject"]]
        if isinstance(value, Exception):
            raise value
        return {
            "model": self.model,
            "answers": {"predicate": {"type": "noul", "noul": value}},
            "usage": {},
        }


@pytest.fixture
def sem(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "sem.db"))
    db.initialize()
    catalog = Catalog(db)
    d = catalog.create(
        "a",
        "arbitrary",
        [
            {"id": 1, "body": "yes", "amount": 10},
            {"id": 2, "body": "no", "amount": 20},
            {"id": 3, "body": "maybe", "amount": 30},
        ],
        primary_key=["id"],
    )
    decisions = Decisions()
    service = SQLService(db, decisions)
    return db, catalog, d, decisions, service


def test_three_valued_cache_and_negation(sem):
    _, _, _, model, sql = sem
    q = "SELECT id FROM arbitrary WHERE SEMANTIC(body,'definition')"
    a = sql.execute("a", q)
    assert a["result"] == [{"id": 1}] and a["manifest"]["semantic_coverage"]["unknown"] == 1
    b = sql.execute("a", "SELECT id FROM arbitrary WHERE NOT SEMANTIC(body,'definition')")
    assert (
        b["result"] == [{"id": 2}]
        and model.calls == 3
        and b["manifest"]["semantic_coverage"]["reused"] == 3
    )
    c = sql.execute("a", q, accept=0.99)
    assert c["result"] == [] and model.calls == 3


def test_budget_failure_and_unknown_mutation(sem):
    _, _, _, model, sql = sem
    q = "SELECT id FROM arbitrary WHERE SEMANTIC(body,'definition')"
    result = sql.execute("a", q, max_evaluations=0)
    assert (
        result["result"] == []
        and result["manifest"]["semantic_coverage"]["unknown"] == 3
        and model.calls == 0
    )
    model.values["yes"] = ProviderError("HTTP_429", True)
    result = sql.execute("a", q)
    assert (
        not result["manifest"]["complete"]
        and result["manifest"]["semantic_coverage"]["failed"] == 1
    )
    with pytest.raises(ValueError, match="unresolved"):
        sql.execute("a", "DELETE FROM arbitrary WHERE SEMANTIC(body,'definition')")
    assert len(sql.execute("a", "SELECT id FROM arbitrary")["result"]) == 3


def test_context_change_invalidates_only_changed_row(sem):
    _, _, _, model, sql = sem
    q = "SELECT id FROM arbitrary WHERE SEMANTIC(body,'definition')"
    sql.execute("a", q)
    p = sql.execute("a", "UPDATE arbitrary SET amount=11 WHERE id=1", actor="r")
    sql.commit("a", p["preview_token"], "r")
    result = sql.execute("a", q)
    assert model.calls == 4 and result["manifest"]["semantic_coverage"]["reused"] == 2


def test_delete_purges_evidence_and_old_results(sem):
    db, catalog, d, _, sql = sem
    first = sql.execute("a", "SELECT id FROM arbitrary WHERE SEMANTIC(body,'definition')")
    p = sql.execute("a", "DELETE FROM arbitrary WHERE id=1", actor="r")
    sql.commit("a", p["preview_token"], "r")
    with db.transaction("a") as cx:
        assert len(cx.execute(select(s.evidence)).all()) == 2
    assert catalog.ledger.get("a", s.runs, first["run_id"])["result"] == []


def test_semantic_mutation_when_complete(sem):
    _, _, _, model, sql = sem
    model.values["maybe"] = 0.02
    p = sql.execute(
        "a", "UPDATE arbitrary SET amount=99 WHERE SEMANTIC(body,'definition')", actor="r"
    )
    assert p["affected_rows"] == 1
    sql.commit("a", p["preview_token"], "r")
    assert model.calls == 3
    assert sql.execute("a", "SELECT amount FROM arbitrary WHERE id=1")["result"] == [{"amount": 99}]


def test_source_change_during_evaluation_refuses_stale_result(sem):
    db, catalog, d, model, sql = sem

    def change():
        model.hook = None
        with db.transaction("a") as cx:
            table = catalog.table(d, cx)
            cx.execute(update(table).where(table.c.id == 1).values(amount=123))

    model.hook = change
    with pytest.raises(ValueError, match="changed"):
        sql.execute("a", "SELECT id FROM arbitrary WHERE SEMANTIC(body,'definition')")


def test_semantic_unknown_not_exists_cannot_become_definite_absence(sem):
    _, _, _, _, sql = sem
    with pytest.raises(ValueError, match="Unresolved semantic"):
        sql.execute(
            "a",
            (
                "SELECT id FROM arbitrary a WHERE NOT EXISTS (SELECT 1 FROM arbitrary b WHERE"
                " b.id=a.id AND SEMANTIC(b.body,'definition'))"
            ),
        )


def test_null_text_stays_unknown_without_provider_call(sem):
    _, catalog, _, model, sql = sem
    catalog.create(
        "a",
        "missing",
        [{"id": 1, "body": None}],
        columns=[{"name": "id", "type": "integer"}, {"name": "body", "type": "text"}],
        primary_key=["id"],
    )
    result = sql.execute("a", "SELECT id FROM missing WHERE NOT SEMANTIC(body,'definition')")
    assert result["result"] == [] and model.calls == 0
    assert result["manifest"]["semantic_coverage"]["missing_subject"] == 1


def test_observed_source_versions_are_retained_across_updates(sem):
    db, _, _, _, sql = sem
    query = "SELECT id FROM arbitrary WHERE SEMANTIC(body,'definition')"
    first = sql.execute("a", query)
    identity = first["manifest"]["evidence"][0]["observations"][0]["source_version_id"]
    p = sql.execute("a", "UPDATE arbitrary SET amount=99 WHERE id=1", actor="r")
    sql.commit("a", p["preview_token"], "r")
    sql.execute("a", query)
    with db.transaction("a") as cx:
        original = (
            cx.execute(select(s.row_versions).where(s.row_versions.c.id == identity))
            .mappings()
            .one()
        )
        assert original["value"]["amount"] == 10
        assert len(cx.execute(select(s.row_versions)).all()) == 4


def test_retry_budget_exhaustion_is_terminal(sem):
    db, _, _, model, sql = sem
    model.values["yes"] = ProviderError("HTTP_503", True)
    query = "SELECT id FROM arbitrary WHERE SEMANTIC(body,'definition')"
    for _ in range(3):
        sql.execute("a", query)
        with db.transaction("a") as cx:
            cx.execute(
                update(s.evidence).where(s.evidence.c.state == "pending").values(lease_until=0)
            )
    calls = model.calls
    result = sql.execute("a", query)
    assert model.calls == calls and result["manifest"]["semantic_coverage"]["failed"] == 1
    with db.transaction("a") as cx:
        failed = (
            cx.execute(select(s.evidence).where(s.evidence.c.state == "failed")).mappings().one()
        )
        assert failed["attempts"] == 3
