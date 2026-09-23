"""Exercise both import interfaces against an isolated API and deterministic JEV."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect

from sdd.api import create_app
from sdd.db import Database
from sdd.execution import Executor
from sdd.generic.catalog import Catalog

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_text_extraction import model, request


def main():
    Path(".runtime").mkdir(exist_ok=True)
    errors = []
    with (
        TemporaryDirectory(
            dir=".runtime", prefix="text-ui-", ignore_cleanup_errors=True
        ) as directory,
        sync_playwright() as p,
    ):
        browser = p.chromium.launch(channel="msedge", headless=True)
        for locale in ("en", "zh"):
            db = Database("sqlite:///" + str(Path(directory) / (locale + ".sqlite")))
            db.initialize()
            app = create_app(
                Executor(db, {}),
                tokens={"fixture": {"tenant": "a", "name": "owner", "role": "reviewer"}},
            )
            app.state.text_importer.decisions = model()
            with TestClient(app) as client:

                def proxy(route):
                    incoming = route.request
                    url = urlsplit(incoming.url)
                    response = client.request(
                        incoming.method,
                        url.path + ("?" + url.query if url.query else ""),
                        headers={
                            "Authorization": "Bearer fixture",
                            "Content-Type": "application/json",
                        },
                        content=incoming.post_data or None,
                    )
                    if response.status_code >= 400 and url.path.startswith("/data/extractions"):
                        print(response.status_code, response.text, flush=True)
                    route.fulfill(
                        status=response.status_code,
                        body=response.content,
                        content_type=response.headers.get("content-type", "application/json"),
                    )

                page = browser.new_page(viewport={"width": 1280, "height": 960})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("http://text-import.test/**", proxy)
                page.goto("http://text-import.test/ask/" + locale)
                page.locator("#token").fill("fixture")
                page.locator("#connect").click()
                page.locator("#text-import-panel > summary").click()
                expect(page.locator("#text-import-panel > summary")).to_have_text(
                    "Extract entries from text" if locale == "en" else "从文本提取记录"
                )
                payload = request()
                page.locator("#extract-dataset-name").fill(payload["name"])
                page.locator("#extract-row").fill(payload["row_description"])
                page.locator("#extract-mode").select_option("line")
                for i, field in enumerate(payload["columns"]):
                    if i:
                        page.locator("#extract-add-column").click()
                    row = page.locator(".extract-column").nth(i)
                    row.locator(".extract-name").fill(field["name"])
                    row.locator(".extract-type").select_option(field["type"])
                    row.locator(".extract-description").fill(field["description"])
                page.locator("#extract-source").fill(payload["text"])
                page.locator("#extract-run").click()
                expect(page.locator("#extract-commit")).to_be_visible()
                assert not Catalog(db).list("a")
                expect(page.locator("#extract-results tbody tr")).to_have_count(2)
                page.locator("#extract-results details").first.click()
                expect(page.locator("#extract-results blockquote").first).to_have_text("Northwind")
                page.locator("#extract-row").fill(payload["row_description"] + ".")
                expect(page.locator("#extract-commit")).to_be_hidden()
                page.locator("#extract-run").click()
                expect(page.locator("#extract-commit")).to_be_visible()
                page.locator("#extract-commit").click()
                expect(page.locator("#extract-status")).to_contain_text("2")
                dataset = Catalog(db).list("a")[0]
                assert len(Catalog(db).rows("a", dataset)) == 2
                page.locator("#extract-destination").select_option(dataset["id"])
                page.locator("#extract-automatic").check()
                page.locator("#extract-run").click()
                expect(page.locator("#extract-status")).to_have_text(
                    ("Entries imported: " if locale == "en" else "已导入记录数：") + "2"
                )
                assert len(Catalog(db).rows("a", dataset)) == 4
                expect(page.locator("#extract-commit")).to_be_hidden()
                page.screenshot(path=".runtime/text-import-ui-" + locale + ".png", full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.close()
            db.engine.dispose()
        browser.close()
    assert not errors, errors
    print(
        "English/Chinese preview, exact excerpts, edit invalidation, create, automatic append and mobile layout passed"
    )


if __name__ == "__main__":
    main()
