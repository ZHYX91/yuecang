"""Small real-browser acceptance of an explicitly confirmed composite edition.

Bootstrap uses synthetic files and the real local API; selecting both sources,
confirming the policy, assembling and finalizing are browser interactions.
"""
import argparse
import json
from pathlib import Path
import time

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--browser", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    marker = str(time.time_ns())[-9:]
    started = time.time()
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=args.browser)
        context = browser.new_context(viewport={"width": 1440, "height": 1050})
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(args.base_url, wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="从每一份来源开始")).to_be_visible()
            headers = {"X-Yuecang-CSRF": context.request.get(args.base_url + "/api/session").json()["csrf_token"]}

            def get(path):
                response = context.request.get(args.base_url + path)
                assert response.ok, response.text()
                return response.json()

            def post(path, body):
                response = context.request.post(args.base_url + path, headers=headers, data=body)
                assert response.ok, response.text()
                return response.json()

            def upload(label, text):
                response = context.request.post(args.base_url + "/api/imports", headers=headers,
                    multipart={"files": {"name": f"synthetic-composite-{label}-{marker}.txt", "mimeType": "text/plain", "buffer": text.encode()}})
                assert response.ok, response.text()
                job_id = response.json()["jobs"][0]["id"]
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    job = next(j for j in get("/api/state")["jobs"] if j["id"] == job_id)
                    if job["state"] == "succeeded":
                        return get("/api/sources/" + job["result"]["source_id"])
                    assert job["state"] not in {"failed", "cancelled", "needs_attention"}, job
                    page.wait_for_timeout(100)
                raise AssertionError("synthetic import timed out")

            texts = ["版本 A 原文：江边的树木在春天长出新叶。", "版本 B 补充：沿岸居民记录了夏季的水位变化。"]
            sources = [upload(label, text) for label, text in zip(("A", "B"), texts)]
            book = post("/api/books", {"title": "综合整理版验收 " + marker, "author": "合成作者", "edition": "版本 A"})
            edition_b = post(f"/api/books/{book['id']}/editions", {"label": "版本 B", "language": "zh"})
            composite = post(f"/api/books/{book['id']}/editions", {"label": "综合整理版", "language": "zh"})
            for source, edition_id in zip(sources, (book["edition_id"], edition_b["id"])):
                post("/api/assignments", {"book_id": book["id"], "edition_id": edition_id,
                    "spans": [{"source_id": source["id"], "block_id": b["id"]} for b in source["blocks"]]})
            original_assignments = {a["id"]: a for a in get("/api/state")["assignments"] if a["book_id"] == book["id"]}
            assert len(original_assignments) == 2

            page.reload(wait_until="domcontentloaded")
            page.get_by_role("navigation", name="工作台导航").get_by_role("button", name="整理工作台", exact=True).click()
            page.get_by_label("目标图书", exact=True).select_option(book["id"])
            page.get_by_label("目标版本", exact=True).select_option(composite["id"])
            page.get_by_label("左侧来源", exact=True).select_option(sources[0]["id"])
            page.get_by_label("右侧来源", exact=True).select_option(sources[1]["id"])
            for panel in page.locator(".comparison-column").all():
                panel.get_by_role("button", name="选择本页", exact=True).click()
            expect(page.locator(".tray-item")).to_have_count(2)
            checkbox = page.get_by_role("checkbox", name="确认为综合整理版，保留各版本出处", exact=True)
            expect(checkbox).not_to_be_checked()
            checkbox.check()
            with page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/assemble")) as sent:
                page.get_by_role("button", name="生成整理稿", exact=True).click()
            submitted = sent.value.post_data_json
            assert submitted["allow_composite"] is True and submitted["edition_id"] == composite["id"]
            expect(page.get_by_label("编辑第 1 块", exact=True)).to_have_value(texts[0])
            expect(page.get_by_label("编辑第 2 块", exact=True)).to_have_value(texts[1])
            page.get_by_role("button", name="定版", exact=True).click()
            expect(page.locator(".editor-heading .badge")).to_have_text("已定版")
            revision_id = page.get_by_label("查看修订", exact=True).input_value()
            revision = get("/api/revisions/" + revision_id)
            assert revision["status"] == "final"
            assert revision["metadata"]["assembly_policy"] == "composite-confirmed"
            assert revision["edition_id"] == composite["id"]
            assert [block["text"] for block in revision["blocks"]] == texts
            assert set(revision["metadata"]["source_ids"]) == {s["id"] for s in sources}
            for block, source in zip(revision["blocks"], sources):
                assert block["source_span"] == {"source_id": source["id"], "block_id": source["blocks"][0]["id"], "start": 0, "end": len(source["blocks"][0]["text"])}
                assert block["provenance"][0]["source"] == block["source_span"]
            issues = [issue for issue in revision["issues"] if issue["code"] == "edition_conflict"]
            assert len(issues) == 2 and all(issue["severity"] == "warning" for issue in issues)
            after = {a["id"]: a for a in get("/api/state")["assignments"] if a["book_id"] == book["id"]}
            assert all(after.get(identifier) == original for identifier, original in original_assignments.items())
            assert {a["source_id"] for a in after.values() if a["edition_id"] == composite["id"]} == {s["id"] for s in sources}
            while page.locator(".notice button").count():
                page.locator(".notice button").first.click()
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "composite-finalized.png"), full_page=True)
            assert not errors, errors
            receipt = {"passed": True, "synthetic_only": True, "external_ai_called": False,
                "checks": {"default_unchecked": True, "ui_submitted_composite_true": True, "two_original_editions_preserved": True,
                    "two_sources_and_ranges_preserved": True, "both_texts_present": True, "composite_policy_frozen": True, "finalized": True},
                "revision_id": revision_id, "duration_seconds": round(time.time() - started, 2), "browser_errors": errors}
            (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(receipt, ensure_ascii=False))
        except Exception as exc:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            (output / "failure.json").write_text(json.dumps({"error": str(exc), "browser_errors": errors}, ensure_ascii=False), encoding="utf-8")
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
