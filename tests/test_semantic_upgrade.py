import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, update

from sdd.db import Database
from sdd.evaluators import ProviderError
from sdd.generic import schema
from sdd.generic.catalog import Catalog
from sdd.generic.features import FeatureRegistry
from sdd.generic.jev import validate_response
from sdd.generic.semantic_types import candidate_spans
from sdd.generic.sql import SQLService


class Model:
    model = "fixture-upgrade-v1"

    def __init__(self, delay=0.02):
        self.calls = []
        self.delay = delay
        self.active = self.peak = 0
        self.lock = threading.Lock()
        self.malformed = False

    def ask(self, tenant, state, questions):
        with self.lock:
            self.calls.append((state, questions))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            answers = {}
            for key, question in questions.items():
                if question["type"] == "noul":
                    value = 0.05 if "no" in state["subject"] else 0.95
                    answers[key] = {"type": "noul", "noul": value}
                elif question["type"] == "choice":
                    choice = next(iter(question["criteria"]))
                    answers[key] = {
                        "type": "choice",
                        "choice": choice,
                        "confidence": 1,
                        "probabilities": {
                            option: float(option == choice) for option in question["criteria"]
                        },
                    }
                else:
                    levels = question["criteria"]
                    answers[key] = {
                        "type": "score",
                        "score": len(levels) - 1,
                        "confidence": 1,
                        "legend": {str(i): level for i, level in enumerate(levels)},
                        "probabilities": {
                            str(i): float(i == len(levels) - 1) for i in range(len(levels))
                        },
                    }
            if self.malformed:
                answers.pop(next(iter(answers)))
            return {
                "model": self.model,
                "answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": len(questions)},
            }
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def system(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "upgrade.db"))
    db.initialize()
    catalog = Catalog(db)
    dataset = catalog.create(
        "a",
        "records",
        [{"id": i, "body": f"yes {i}。引用「名字{i}」。", "amount": i} for i in range(8)],
        primary_key=["id"],
    )
    model = Model()
    return db, catalog, dataset, model, SQLService(db, model), FeatureRegistry(db)


def feature(system, name="approved", **options):
    _, _, dataset, _, _, registry = system
    row = registry.create(
        "a", "reviewer", dataset["id"], name, "body", "Matches the definition", **options
    )
    return registry.review(
        "a",
        row["id"],
        "reviewer",
        "active",
        "Checked fixtures",
        [{"text": "yes", "expected": True}],
    )


def test_sql_batches_predicates_and_parallelizes_records(system):
    db, _, _, model, sql, _ = system
    query = "SELECT id FROM records WHERE SEMANTIC(body,'A') AND SEMANTIC(body,'B') ORDER BY id"
    result = sql.execute("a", query)
    coverage = result["manifest"]["semantic_coverage"]
    assert len(model.calls) == 8 and 1 < model.peak <= 4
    assert all(len(questions) == 2 for _, questions in model.calls)
    assert coverage["evaluated"] == 16 and coverage["requests"] == 8
    assert coverage["input_tokens"] == 800
    warm = sql.execute("a", query)
    assert warm["result"] == result["result"]
    assert warm["manifest"]["semantic_coverage"]["reused"] == 16 and len(model.calls) == 8
    with db.transaction("a") as connection:
        assert len(connection.execute(select(schema.payloads)).all()) == 16
        calls = connection.execute(select(schema.inference_calls)).mappings().all()
        assert sum(call["usage"]["input_tokens"] for call in calls) == 800


def test_structured_pushdown_preserves_or_and_zero_scope(system):
    _, _, _, model, sql, _ = system
    result = sql.execute("a", "SELECT id FROM records WHERE amount<2 AND SEMANTIC(body,'A')")
    assert len(model.calls) == 2
    assert result["manifest"]["semantic_coverage"]["structured_rows_skipped"] == 6
    sql.execute("a", "SELECT id FROM records WHERE amount<2 OR SEMANTIC(body,'B')")
    assert len(model.calls) == 10
    empty = sql.execute(
        "a", "SELECT COUNT(*) AS n FROM records WHERE amount<0 AND SEMANTIC(body,'C')"
    )
    assert empty["result"] == [{"n": 0}] and len(model.calls) == 10


def test_repeated_operator_and_budget_are_not_double_charged(system):
    _, _, _, model, sql, _ = system
    result = sql.execute(
        "a",
        "SELECT id FROM records WHERE SEMANTIC(body,'A') OR SEMANTIC(body,'A')",
        max_evaluations=3,
    )
    assert len(model.calls) == 3
    assert result["manifest"]["semantic_coverage"]["evaluated"] == 3
    assert result["manifest"]["semantic_coverage"]["unknown"] == 5


def test_missing_batch_answer_rejects_all_results(system):
    db, _, _, model, sql, _ = system
    model.malformed = True
    result = sql.execute(
        "a", "SELECT id FROM records WHERE SEMANTIC(body,'A') OR SEMANTIC(body,'B')"
    )
    assert result["result"] == []
    assert result["manifest"]["semantic_coverage"]["unknown"] == 16
    with db.transaction("a") as connection:
        assert not connection.execute(select(schema.payloads)).all()


def test_concurrent_queries_share_leases(system):
    db, _, _, model, sql, _ = system
    query = "SELECT id FROM records WHERE SEMANTIC(body,'A')"
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: sql.execute("a", query), range(2)))
    assert len(model.calls) == 8
    assert sql.execute("a", query)["manifest"]["complete"]
    with db.transaction("a") as connection:
        assert len(connection.execute(select(schema.payloads)).all()) == 8


def test_typed_features_share_context_and_use_sql(system):
    _, _, _, model, sql, _ = system
    feature(system, "类别", kind="choice", criteria={"研究": "Research", "其他": "Other"})
    feature(system, "程度", kind="score", criteria=["Low", "Medium", "High"])
    result = sql.execute(
        "a",
        "SELECT SEMANTIC_FEATURE(body,'类别') AS category, AVG(SEMANTIC_FEATURE(body,'程度')) AS score FROM records GROUP BY category",
    )
    assert result["result"] == [{"category": "研究", "score": 2.0}]
    assert len(model.calls) == 8 and all(len(questions) == 3 for _, questions in model.calls)
    assert all(set(state["context"]) == {"body"} for state, _ in model.calls)


def test_focused_dependencies_and_human_corrections(system):
    _, _, _, model, sql, registry = system
    row = feature(system)
    query = "SELECT id FROM records WHERE SEMANTIC_FEATURE(body,'approved') ORDER BY id"
    sql.execute("a", query)
    preview = sql.execute("a", "UPDATE records SET amount=99 WHERE id=1", actor="r")
    sql.commit("a", preview["preview_token"], "r")
    assert sql.execute("a", query)["manifest"]["semantic_coverage"]["reused"] == 8
    assert len(model.calls) == 8
    registry.assert_value("a", row["id"], {"id": 1}, False, "reviewer", "Reviewed source")
    result = sql.execute("a", query)
    assert {record["id"] for record in result["result"]} == set(range(8)) - {1}
    assert result["manifest"]["semantic_coverage"]["reviewed"] == 1
    preview = sql.execute("a", "UPDATE records SET body='no revised' WHERE id=1", actor="r")
    sql.commit("a", preview["preview_token"], "r")
    result = sql.execute("a", query)
    assert len(model.calls) == 9 and result["manifest"]["semantic_coverage"]["reviewed"] == 0


def test_changed_reviews_invalidate_even_equal_size_mutation_preview(system):
    _, _, _, _, sql, registry = system
    row = feature(system)
    registry.assert_value("a", row["id"], {"id": 1}, False, "r", "First review")
    preview = sql.execute(
        "a", "UPDATE records SET amount=42 WHERE SEMANTIC_FEATURE(body,'approved')", actor="r"
    )
    registry.assert_value("a", row["id"], {"id": 1}, True, "r", "Corrected")
    registry.assert_value("a", row["id"], {"id": 2}, False, "r", "Corrected")
    with pytest.raises(ValueError, match="review changed"):
        sql.commit("a", preview["preview_token"], "r")
    assert sql.execute("a", "SELECT amount FROM records WHERE id=0")["result"] == [{"amount": 0}]


def test_revisions_and_aliases_do_not_merge_meanings(system):
    _, _, dataset, _, sql, registry = system
    first = feature(system, aliases=["已批准"])
    assert registry.resolve("a", dataset["id"], "已批准")["id"] == first["id"]
    second = registry.create(
        "a", "r", dataset["id"], "approved", "body", "A different definition", aliases=["已批准"]
    )
    with pytest.raises(ValueError, match="inactive"):
        sql.execute("a", f"SELECT SEMANTIC_FEATURE(body,'{second['id']}') FROM records")
    registry.review(
        "a", second["id"], "r", "active", "Reviewed change", [{"text": "yes", "expected": True}]
    )
    assert registry.get("a", first["id"])["status"] == "deprecated"
    assert registry.resolve("a", dataset["id"], "已批准")["id"] == second["id"]
    with pytest.raises(ValueError):
        registry.resolve("b", dataset["id"], "approved")


def test_grounded_spans_preserve_unicode_offsets(system):
    _, _, _, model, sql, _ = system
    text = "甲说：「張偉」。第二句保留 full-width！"
    spans = candidate_spans(text)
    assert any(span["text"] == "張偉" for span in spans)
    assert all(text[span["start"] : span["end"]] == span["text"] for span in spans)
    feature(system, "excerpt", kind="extract")
    result = sql.execute("a", "SELECT SEMANTIC_FEATURE(body,'excerpt') AS passage FROM records")
    assert all(
        item["passage"] in state["subject"]
        for item, (state, _) in zip(result["result"], model.calls)
    )


def test_maintenance_evaluates_new_rows_only(system):
    _, _, _, model, sql, registry = system
    row = feature(system, maintain=True)
    assert registry.work_one("a", model)
    assert registry.get("a", row["id"])["materialization"]["run_id"]
    assert len(model.calls) == 8
    preview = sql.execute(
        "a", "INSERT INTO records(id,body,amount) VALUES(8,'yes new',8)", actor="r"
    )
    sql.commit("a", preview["preview_token"], "r")
    assert registry.work_one("a", model) and len(model.calls) == 9
    assert not registry.work_one("a", model)


def test_expired_final_lease_becomes_terminal(system):
    db, _, _, model, sql, _ = system
    model.malformed = True
    query = "SELECT id FROM records WHERE amount=0 AND SEMANTIC(body,'A')"
    sql.execute("a", query)
    with db.transaction("a") as connection:
        connection.execute(
            update(schema.evidence).values(state="running", attempts=3, lease_until=0)
        )
    sql.execute("a", query)
    assert len(model.calls) == 1
    with db.transaction("a") as connection:
        assert connection.execute(select(schema.evidence.c.state)).scalar_one() == "failed"


@pytest.mark.parametrize("change", ["nan", "range", "legend", "distribution"])
def test_invalid_score_contract_is_rejected(change):
    question = {"q": {"type": "score", "instructions": "Rate", "criteria": ["Low", "High"]}}
    answer = {
        "type": "score",
        "score": 1,
        "legend": {"0": "Low", "1": "High"},
        "probabilities": {"0": 0, "1": 1},
    }
    if change == "nan":
        answer["score"] = float("nan")
    if change == "range":
        answer["score"] = 3
    if change == "legend":
        answer["legend"]["1"] = "Changed"
    if change == "distribution":
        answer["probabilities"]["0"] = 1
    with pytest.raises(ProviderError):
        validate_response({"model": "m", "answers": {"q": answer}}, question, "m")


def test_cancelled_maintenance_does_not_publish(system):
    db, _, dataset, model, _, registry = system
    active = feature(system, maintain=True)
    entered, released = threading.Event(), threading.Event()
    original = model.ask

    def blocked(*args, **kwargs):
        entered.set()
        assert released.wait(5)
        return original(*args, **kwargs)

    model.ask = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(registry.work_one, "a", model)
        assert entered.wait(5)
        with db.transaction("a") as connection:
            connection.execute(
                update(schema.maintenance_jobs)
                .where(schema.maintenance_jobs.c.dataset_id == dataset["id"])
                .values(state="cancelled", lease_token=None)
            )
        released.set()
        assert future.result(timeout=5)
    assert registry.get("a", active["id"])["materialization"] == {}
    with db.transaction("a") as connection:
        assert (
            connection.execute(select(schema.maintenance_jobs.c.state)).scalar_one() == "cancelled"
        )


def test_feature_alias_cannot_shadow_source_column(system):
    with pytest.raises(ValueError, match="shadow"):
        feature(system, aliases=["AMOUNT"])


def test_traditional_chinese_literals():
    from sdd.generic.planner import literals, chinese_number

    values = literals("前兩筆中，名稱為「張偉」，改為『待複核』，至少三萬。")
    assert 2 in values["numbers"] and 30000 in values["numbers"]
    assert "張偉" in values["strings"] and "待複核" in values["strings"]
    assert chinese_number("兩萬三千") == 23000


def test_extraction_review_rejects_missing_source(system):
    _, _, _, _, sql, registry = system
    active = feature(system, kind="extract")
    preview = sql.execute("a", "UPDATE records SET body=NULL WHERE id=0", actor="reviewer")
    sql.commit("a", preview["preview_token"], "reviewer")
    with pytest.raises(ValueError, match="copied"):
        registry.assert_value(
            "a", active["id"], {"id": 0}, "invented", "reviewer", "Invalid correction"
        )


def test_semantic_pushdown_uses_local_cte_scope_before_outer_window_filter(system):
    _, _, _, model, sql, _ = system
    result = sql.execute(
        "a",
        """WITH matched AS (
        SELECT id, amount FROM records WHERE amount < 3 AND SEMANTIC(body, 'A')
        ), ranked AS (SELECT id, ROW_NUMBER() OVER (ORDER BY amount DESC) AS rank FROM matched)
        SELECT id FROM ranked WHERE rank = 1""",
    )
    assert result["result"] == [{"id": 2}]
    assert len(model.calls) == 3
    assert result["manifest"]["semantic_coverage"]["structured_rows_skipped"] == 5


def test_semantic_sibling_populations_are_unioned_and_shared_rows_evaluated_once(system):
    _, _, _, model, sql, _ = system
    result = sql.execute(
        "a",
        """WITH a AS (
        SELECT id FROM records WHERE amount < 3 AND SEMANTIC(body, 'A')
        ), b AS (SELECT id FROM records WHERE amount BETWEEN 2 AND 4 AND SEMANTIC(body, 'A'))
        SELECT id FROM a UNION ALL SELECT id FROM b ORDER BY id""",
    )
    assert result["result"] == [{"id": n} for n in (0, 1, 2, 2, 3, 4)]
    assert len(model.calls) == 5
    assert result["manifest"]["semantic_coverage"]["structured_rows_skipped"] == 3
