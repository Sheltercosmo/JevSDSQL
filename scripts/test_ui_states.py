"""Browser checks for localized validation, review states, and narrow layouts."""

import json
from playwright.sync_api import sync_playwright, expect


COPY = {
    "en": {
        "token": "Access token not recognized. Check your connection settings.",
        "json": "Invalid JSON. Check quotation marks, commas, and brackets.",
        "feature": "Choose a semantic feature first.",
        "review": "The interpretation needs review. Inspect the proposed SQL or make your request more specific.",
        "partial": "Partial result · unresolved decisions",
    },
    "zh": {
        "token": "访问令牌无效，请检查连接设置。",
        "json": "JSON 格式有误，请检查引号、逗号和括号。",
        "feature": "请先选择语义特征。",
        "review": "查询含义需要确认。请检查拟执行的 SQL，或更明确地描述问题。",
        "partial": "部分结果，仍有未确定的判断",
    },
}


def main():
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        for locale, copy in COPY.items():
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:8000/ask/{locale}")
            expect(page.locator("#output-empty")).to_be_visible()
            page.locator("#token").fill("invalid-ui-test-token")
            page.locator("#token").press("Enter")
            expect(page.locator("#connection-error")).to_have_text(copy["token"])
            page.locator("#import-panel > summary").click()
            page.locator("#import-json").fill("{")
            page.locator("#import").click()
            expect(page.locator("#import-status")).to_have_text(copy["json"])
            page.locator("#feature-panel > summary").click()
            page.locator("#preview-feature").click()
            expect(page.locator("#feature-status")).to_have_text(copy["feature"])
            page.locator("#import-panel > summary").click()
            page.locator("#feature-panel > summary").click()

            # These transport fixtures isolate UI state from model variability.
            page.route(
                "**/ask",
                lambda route: route.fulfill(
                    status=400,
                    json={
                        "detail": "The complete-request check was uncertain.",
                        "review_required": True,
                        "plan": {"logical_sql": "SELECT id FROM records"},
                    },
                ),
            )
            page.locator("#question").fill("test")
            page.locator("#run").click()
            expect(page.locator("#status")).to_have_text(copy["review"])
            expect(page.locator("#panel-sql")).to_be_visible()
            page.locator("#review-sql").click()
            expect(page.locator("#question")).to_have_value("SELECT id FROM records")
            expect(page.locator("#preview")).to_be_disabled()
            page.route(
                "**/data/sql",
                lambda route: route.fulfill(
                    json={
                        "logical_sql": "SELECT id FROM records",
                        "result": [],
                        "manifest": {
                            "complete": False,
                            "source_rows": 2,
                            "semantic_coverage": {"unknown": 2},
                        },
                    }
                ),
            )
            page.locator("#run").click()
            expect(page.locator("#status")).to_have_text(copy["partial"])
            expect(page.locator("#mutation")).to_be_hidden()
            for width in (1280, 768, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=f"artifacts/ui-{locale}-partial-mobile.png", full_page=True)
            page.close()
        browser.close()
    assert not errors, errors
    print(
        json.dumps(
            {
                "locales": list(COPY),
                "validation_and_review_states": True,
                "widths": [1280, 768, 390, 320],
                "page_errors": errors,
            }
        )
    )


if __name__ == "__main__":
    main()
