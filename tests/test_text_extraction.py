"""Contract tests use a deterministic decision provider, not language-quality scores."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sdd.api import create_app
from sdd.db import Database
from sdd.execution import Executor
from sdd.generic.catalog import Catalog
from sdd.generic.extraction_schema import ExtractionColumn
from sdd.generic.extraction_spans import convert
from sdd.generic.text_import import TextImporter
from sdd.operators.service import OperatorService


class ExtractModel:
    model = "jev-1.13.0"

    def __init__(self, records, reject_grounding=False):
        self.records, self.reject_grounding = records, reject_grounding
        self.calls = 0

    def ask(self, tenant, state, questions):
        self.calls += 1
        answers = {}
        for key, q in questions.items():
            instructions = q["instructions"]
            phase = instructions["phase"]
            if phase == "record_boundary":
                current = next(
                    x["current"]
                    for x in state["candidates"]
                    if x["id"] == instructions["candidate_id"]
                )
            else:
                source = str(
                    next(x for x in state["records"] if x["id"] == instructions["record_id"])
                )
                gold = next(
                    (row for row in self.records if row["anchor"] in source), self.records[0]
                )
            if q["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.01 if self.reject_grounding else 0.99}
                continue
            if phase == "record_boundary":
                selected = (
                    "start"
                    if any(row["anchor"] in current for row in self.records)
                    else "irrelevant"
                )
            else:
                raw = gold.get(instructions["column"]["name"])
                if phase == "field_location":
                    selected = (
                        "absent"
                        if raw is None
                        else next(
                            k
                            for k in q["criteria"]
                            if k not in {"absent", "ambiguous"} and raw in q["criteria"][k]
                        )
                    )
                elif phase == "scalar_literal":
                    selected = next(
                        k
                        for k, v in q["criteria"].items()
                        if isinstance(v, dict) and v["text"] == raw
                    )
                else:
                    passage = instructions["passage"]
                    lo = passage.index(raw)
                    hi = lo + len(raw)
                    from sdd.generic.extraction_spans import TOKEN

                    tokens = list(TOKEN.finditer(passage))
                    target = (
                        next(i for i, token in enumerate(tokens) if token.start() == lo)
                        if phase == "span_start"
                        else next(i for i, token in enumerate(tokens) if token.end() == hi)
                    )
                    selected = str(target)
            answers[key] = {
                "type": "choice",
                "choice": selected,
                "confidence": 0.99,
                "probabilities": {k: float(k == selected) for k in q["criteria"]},
            }
        return {
            "model": self.model,
            "answers": answers,
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }


@pytest.fixture
def db(tmp_path):
    database = Database("sqlite:///" + str(tmp_path / "extraction.db"))
    database.initialize()
    yield database
    database.engine.dispose()


def request():
    return {
        "name": "Deliveries",
        "text": "Northwind sent 12 valves, total $1,250.50.\nEastbank sent 8 valves, total $840.00.",
        "row_description": "One supplier delivery per row",
        "record_mode": "line",
        "columns": [
            {"name": "supplier", "description": "Supplier name", "type": "text"},
            {"name": "quantity", "description": "Valve count", "type": "integer"},
            {"name": "amount", "description": "Total charge", "type": "number"},
        ],
    }


def model():
    return ExtractModel(
        [
            {
                "anchor": "Northwind",
                "supplier": "Northwind",
                "quantity": "12",
                "amount": "$1,250.50",
            },
            {"anchor": "Eastbank", "supplier": "Eastbank", "quantity": "8", "amount": "$840.00"},
        ]
    )


def test_extract_create_exact_values_and_idempotent_commit(db):
    payload = request()
    importer = TextImporter(db, model())
    preview = importer.preview("a", "alice", **payload)
    assert preview["can_import"]
    rows = preview["extraction"]["value"]["rows"]
    assert len(rows) == 2
    for row in rows:
        for cell in row["cells"].values():
            span = cell["source"]
            assert payload["text"][span["start"] : span["end"]] == span["text"]
    assert not Catalog(db).list("a")
    result = importer.commit("a", "alice", preview["preview_token"])
    assert importer.commit("a", "alice", preview["preview_token"])["replayed"]
    dataset = Catalog(db).get("a", result["dataset_id"])
    stored = Catalog(db).rows("a", dataset)
    assert len(stored) == 2
    assert {r["amount"] for r in stored} == {Decimal("1250.50"), Decimal("840.00")}


def test_hold_and_budget_do_not_insert_false_values(db):
    importer = TextImporter(db, ExtractModel(model().records, reject_grounding=True))
    preview = importer.preview("a", "alice", **request())
    assert preview["extraction"]["output_state"] == "UNKNOWN"
    assert not preview["can_import"]
    with pytest.raises(ValueError, match="complete"):
        importer.commit("a", "alice", preview["preview_token"])
    result = OperatorService(db, model(), "budget_tenant", "alice").call(
        "EXTRACT_TABLE",
        {k: v for k, v in request().items() if k != "name"},
        limits={"max_judgments": 0},
    )
    assert result["output_state"] == "NOT_EVALUATED"
    assert result["operation_state"] == "BLOCKED_BY_BUDGET"
    assert Catalog(db).list("a") == []


def test_absent_optional_null_is_explicit(db):
    payload = request()
    payload["columns"].append(
        {
            "name": "memo",
            "type": "text",
            "description": "An explicit memo",
            "nullable": True,
            "on_missing": "null",
        }
    )
    preview = TextImporter(db, model()).preview("a", "alice", **payload)
    assert preview["can_import"]
    assert preview["extraction"]["value"]["rows"][0]["values"]["memo"] is None


def test_chinese_and_schema_names_preserve_unicode_spans(db):
    payload = {
        "name": "采样",
        "text": "海桥站的温度为－12.50，采样日期是2026年9月23日。",
        "row_description": "每个站点的测量占一行",
        "record_mode": "document",
        "columns": [
            {"name": "地点", "type": "text", "description": "站点名称"},
            {"name": "温度", "type": "number", "description": "温度数值，保留正负号"},
            {"name": "日期", "type": "date", "description": "采样的公历日期"},
        ],
    }
    provider = ExtractModel(
        [{"anchor": "海桥站", "地点": "海桥站", "温度": "－12.50", "日期": "2026年9月23日"}]
    )
    result = TextImporter(db, provider).preview("a", "alice", **payload)
    assert result["can_import"]
    assert result["extraction"]["value"]["rows"][0]["values"] == {
        "地点": "海桥站",
        "温度": "-12.50",
        "日期": "2026-09-23",
    }


@pytest.mark.parametrize(
    "raw,config,expected",
    [
        ("1.234,50", {"decimal_separator": ",", "group_separator": "."}, "1234.50"),
        ("2.5万", {}, "25000.0"),
        ("9999999999999999999999999999.99", {}, "9999999999999999999999999999.99"),
        ("1.25000000000", {}, "1.25"),
        ("12.5%", {"percent": "fraction"}, "0.125"),
        ("(1,250.50)", {}, "-1250.50"),
        ("-1.20e3", {}, "-1200"),
    ],
)
def test_exact_numeric_conversion(raw, config, expected):
    column = ExtractionColumn(name="v", description="value", type="number", **config)
    assert Decimal(convert(raw, column)) == Decimal(expected)


@pytest.mark.parametrize(
    "raw",
    [
        "1,25",
        "NaN",
        "Infinity",
        "1e-100000",
        "1e100000",
        "12 percent",
        "1.123456789012",
        "99999999999999999999999999999",
    ],
)
def test_ambiguous_or_unrepresentable_numbers_are_rejected(raw):
    with pytest.raises(ValueError):
        convert(raw, ExtractionColumn(name="v", description="value", type="number"))


def test_append_authorization_and_atomic_duplicate_failure(db):
    catalog = Catalog(db)
    target = catalog.create(
        "a",
        "Existing",
        [{"supplier": "Northwind", "quantity": 1, "amount": 1}],
        columns=request()["columns"],
        primary_key=["supplier"],
    )
    payload = {**request(), "dataset_id": target["id"]}
    payload.pop("name")
    payload["text"] = "\n".join(reversed(payload["text"].splitlines()))
    importer = TextImporter(db, model())
    preview = importer.preview("a", "alice", **payload)
    with pytest.raises(ValueError):
        importer.commit("b", "alice", preview["preview_token"])
    with pytest.raises(ValueError):
        importer.commit("a", "bob", preview["preview_token"])
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        importer.commit("a", "alice", preview["preview_token"])
    assert len(catalog.rows("a", target)) == 1


def test_api_supports_automatic_import_and_restricts_readers(db):
    app = create_app(
        Executor(db, {}),
        tokens={
            "reviewer": {"tenant": "a", "name": "alice", "role": "reviewer"},
            "reader": {"tenant": "a", "name": "bob", "role": "reader"},
        },
    )
    app.state.text_importer.decisions = model()
    client = TestClient(app)
    assert (
        client.post(
            "/data/extractions", headers={"Authorization": "Bearer reader"}, json=request()
        ).status_code
        == 403
    )
    response = client.post(
        "/data/extractions",
        headers={"Authorization": "Bearer reviewer"},
        json={**request(), "commit": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["committed"]
    assert response.json()["import"]["inserted_rows"] == 2


def test_truncation_keeps_partial_entries_but_blocks_import(db):
    preview = TextImporter(db, model()).preview("a", "alice", **request(), max_rows=1)
    assert not preview["can_import"]
    assert preview["extraction"]["operation_state"] == "TRUNCATED"
    assert preview["extraction"]["row_candidates"] == 2
    assert len(preview["extraction"]["partial_value"]["rows"]) == 1


def test_mid_pipeline_budget_keeps_cells_unevaluated(db):
    preview = TextImporter(db, model()).preview(
        "a", "alice", **request(), limits={"max_requests": 2}
    )
    assert not preview["can_import"]
    rows = preview["extraction"]["partial_value"]["rows"]
    assert rows
    assert all(
        cell["output_state"] == "NOT_EVALUATED" for row in rows for cell in row["cells"].values()
    )
    assert preview["extraction"]["operation_state"] == "BLOCKED_BY_BUDGET"


def test_provider_failure_is_not_a_false_record(db):
    from sdd.evaluators import ProviderError

    class Failed(ExtractModel):
        def ask(self, *args):
            raise ProviderError("Unavailable", False)

    preview = TextImporter(db, Failed([])).preview("a", "alice", **request())
    assert not preview["can_import"]
    assert preview["extraction"]["output_state"] == "NOT_EVALUATED"
    assert preview["extraction"]["operation_state"] == "FAILED"


def test_nullable_alone_does_not_authorize_filling_missing_values(db):
    payload = request()
    payload["columns"].append({"name": "memo", "description": "Memo", "nullable": True})
    preview = TextImporter(db, model()).preview("a", "alice", **payload)
    assert not preview["can_import"]
    assert (
        preview["extraction"]["partial_value"]["rows"][0]["cells"]["memo"]["output_state"]
        == "UNKNOWN"
    )


def test_changed_destination_definition_invalidates_preview(db):
    from sqlalchemy import update
    from sdd.generic import schema

    target = Catalog(db).create("a", "Existing", [], columns=request()["columns"])
    payload = {**request(), "dataset_id": target["id"]}
    payload.pop("name")
    importer = TextImporter(db, model())
    preview = importer.preview("a", "alice", **payload)
    with db.transaction("a") as connection:
        connection.execute(
            update(schema.datasets)
            .where(schema.datasets.c.id == target["id"])
            .values(writable=False)
        )
    with pytest.raises(ValueError, match="changed"):
        importer.commit("a", "alice", preview["preview_token"])
    assert Catalog(db).rows("a", target) == []


def test_precision_loss_rolls_back_dataset_creation(db):
    payload = dict(
        name="precision",
        text="The value is 9007199254740993.01.",
        row_description="One measurement",
        record_mode="document",
        columns=[{"name": "v", "type": "number", "description": "Value"}],
    )
    importer = TextImporter(db, ExtractModel([{"anchor": "value", "v": "9007199254740993.01"}]))
    preview = importer.preview("a", "alice", **payload)
    assert preview["can_import"]
    with pytest.raises(ValueError, match="precision"):
        importer.commit("a", "alice", preview["preview_token"])
    assert Catalog(db).list("a") == []


def test_independent_records_are_batched_and_occurrences_preserved(db):
    payload = request()
    payload["text"] = "\n".join([payload["text"].splitlines()[0]] * 24)
    provider = model()
    importer = TextImporter(db, provider)
    preview = importer.preview("a", "alice", **payload)
    assert preview["can_import"]
    assert preview["rows"] == 24
    assert provider.calls < 24
    result = importer.commit("a", "alice", preview["preview_token"])
    assert len(Catalog(db).rows("a", Catalog(db).get("a", result["dataset_id"]))) == 24


def test_expired_previews_cannot_write(db):
    from sqlalchemy import update
    from sdd.generic import schema

    importer = TextImporter(db, model())
    preview = importer.preview("a", "alice", **request())
    with db.transaction("a") as connection:
        connection.execute(
            update(schema.previews)
            .where(schema.previews.c.id == preview["preview_token"])
            .values(expires_at=0)
        )
    with pytest.raises(ValueError, match="unexpired"):
        importer.commit("a", "alice", preview["preview_token"])
    assert not Catalog(db).list("a")
