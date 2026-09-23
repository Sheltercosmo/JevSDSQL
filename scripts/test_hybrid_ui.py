"""Exercise the bilingual hybrid selector, contract view and history against the API."""

from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_hybrid_planner import LLM, Reviewer
from sdd.api import create_app
from sdd.db import Database
from sdd.execution import Executor
from sdd.generic.catalog import Catalog
from sdd.generic.planner import Planner
from sdd.generic.sql import SQLService


def main():
    Path(".runtime").mkdir(exist_ok=True)
    os.environ["SDD_HYBRID_CONCEPTS"] = "off"
    errors = []
    with (
        TemporaryDirectory(
            dir=".runtime", prefix="hybrid-ui-", ignore_cleanup_errors=True
        ) as directory,
        sync_playwright() as p,
    ):
        browser = p.chromium.launch(channel="msedge", headless=True)
        for locale in ("en", "zh"):
            db = Database("sqlite:///" + str(Path(directory) / (locale + ".sqlite")))
            db.initialize()
            catalog = Catalog(db)
            catalog.create("a", "readings", [{"id": 1, "amount": 4}], primary_key=["id"])
            llm = LLM(
                [
                    "SELECT COUNT(*) AS total FROM readings",
                    "SELECT SUM(amount) AS total FROM readings",
                ]
            )
            if locale == "zh":
                llm.draft.update(objective="统计读数", result_grain="一个总数")
                llm.draft["steps"][0].update(
                    name="计数", purpose="统计符合条件的记录", grain="一个总数"
                )
                llm.draft["data_constraints"][0].update(
                    row_scope="全部读数",
                    grain="每条读数一行",
                    keys_and_relationships="id是唯一主键",
                    units_and_nulls="统计所有记录",
                    purpose="统计总体",
                )
            for i, candidate in enumerate(llm.draft["candidates"]):
                candidate["label"] = (
                    ["Count records", "Total amount"]
                    if locale == "en"
                    else ["统计记录数", "汇总金额"]
                )[i]
                candidate["assumptions"] = [
                    "One source row is one record." if locale == "en" else "每个来源行是一条记录。"
                ]
            planner = Planner(db, Reviewer(), llm=llm)
            with patch("sdd.generic.api.services", return_value=(catalog, SQLService(db), planner)):
                app = create_app(
                    Executor(db, {}),
                    tokens={"fixture": {"tenant": "a", "name": "owner", "role": "reviewer"}},
                )
            with TestClient(app) as client:
                calls = []

                def proxy(route):
                    incoming = route.request
                    url = urlsplit(incoming.url)
                    body = incoming.post_data_json if incoming.method == "POST" else None
                    calls.append((incoming.method, url.path, body))
                    response = client.request(
                        incoming.method,
                        url.path + ("?" + url.query if url.query else ""),
                        headers={"Authorization": "Bearer fixture"},
                        json=body,
                    )
                    route.fulfill(
                        status=response.status_code,
                        body=response.content,
                        content_type="application/json",
                    )

                page = browser.new_page(viewport={"width": 1280, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                for pattern in (
                    "**/datasets**",
                    "**/query-history**",
                    "**/ask",
                    "**/ask/review",
                    "**/ask/confirm",
                    "**/features**",
                    "**/data/**",
                ):
                    page.route(pattern, proxy)
                page.goto("http://127.0.0.1:8000/ask/" + locale)
                page.locator('a[href="#workspace-guide"]').click()
                expect(page.locator("#workspace-guide")).to_have_attribute("open", "")
                expect(page.locator("#workspace-guide")).to_contain_text(
                    "Turn text into records" if locale == "en" else "从文本创建记录"
                )
                page.locator("#workspace-guide > summary").click()
                page.locator("#token").fill("fixture")
                page.locator("#connect").click()
                page.locator("#planner-mode").select_option("hybrid")
                page.locator("#question").fill("Count readings" if locale == "en" else "统计读数")
                page.locator("#preview").click()
                expect(page.locator("#hybrid-contract")).to_be_visible()
                page.locator("#hybrid-contract > summary").click()
                expect(page.locator("#hybrid-contract")).to_contain_text(
                    "Data constraints" if locale == "en" else "数据约束"
                )
                expect(page.locator("#review-other-decisions")).to_contain_text(
                    "Answer field review" if locale == "en" else "输出字段核验"
                )
                assert (
                    next(
                        body for method, path, body in calls if method == "POST" and path == "/ask"
                    )["planner_mode"]
                    == "hybrid"
                )
                expect(page.locator("#hybrid-overview")).to_contain_text(
                    "How this plan was built" if locale == "en" else "方案生成过程"
                )
                expect(page.locator("#planner-help")).to_contain_text(
                    "parallel" if locale == "en" else "并行"
                )
                page.locator('[data-hybrid-candidate="c1"]').click()
                expect(page.locator("#review-proposal-sql")).to_contain_text("SUM(amount)")
                assert any(path == "/ask/review" for _, path, _ in calls)
                assert not any(path == "/ask/confirm" for _, path, _ in calls)
                assert llm.calls == 1
                identity = client.get(
                    "/query-history", headers={"Authorization": "Bearer fixture"}
                ).json()["items"][0]["id"]
                page.locator("#planner-mode").select_option("jev")
                page.locator('[data-history-id="' + identity + '"]').click()
                expect(page.locator("#planner-mode")).to_have_value("hybrid")
                page.locator("#history-review").click()
                expect(page.locator("#hybrid-contract")).to_be_visible()
                assert llm.calls == 1
                page.locator("#hybrid-contract > summary").click()
                page.screenshot(path=".runtime/hybrid-ui-" + locale + ".png", full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=".runtime/hybrid-ui-mobile-" + locale + ".png", full_page=True)
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )
                page.evaluate("""renderHybridContract({
                    logical_sql: "",
                    hybrid: {
                        retrieval: {output_state: "NOT_EVALUATED", operation_state: "BLOCKED_BY_BUDGET"},
                        generation_state: {output: "UNKNOWN", operation: "FAILED"},
                        review_state: {output: "NOT_EVALUATED", operation: "SKIPPED"}
                    }
                })""")
                expect(page.locator("#hybrid-overview")).to_contain_text(
                    "No SQL proposal" if locale == "en" else "尚未生成 SQL 方案"
                )
                expect(page.locator(".hybrid-flow li").nth(0)).to_contain_text(
                    "Not evaluated · Budget limit reached"
                    if locale == "en"
                    else "未评估 · 达到额度上限"
                )
                expect(page.locator(".hybrid-flow li").nth(1)).to_contain_text(
                    "Needs review · Could not complete" if locale == "en" else "尚待核验 · 未能完成"
                )
                expect(page.locator("#hybrid-choices")).to_be_hidden()
                page.close()
            db.engine.dispose()
        browser.close()
    assert not errors, errors
    print(
        "English and Simplified Chinese hybrid UI, API payload, history restoration and draft reuse passed"
    )


if __name__ == "__main__":
    main()
