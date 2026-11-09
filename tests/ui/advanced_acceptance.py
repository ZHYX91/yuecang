"""Targeted real-browser checks for batching, branch merging and source assets.

Only synthetic fixtures are seeded through the real local API. No external AI.
"""
import argparse
import json
from pathlib import Path
import struct
import time
import zlib

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
    steps, errors = [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=args.browser)
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
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

            def upload(name, data, mime="text/plain"):
                response = context.request.post(args.base_url + "/api/imports", headers=headers,
                    multipart={"files": {"name": name, "mimeType": mime, "buffer": data}})
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

            def seed(title, text):
                source = upload(f"synthetic-advanced-{title}-{marker}.txt", text.encode())
                book = post("/api/books", {"title": title + marker, "author": "合成作者"})
                draft = post(f"/api/books/{book['id']}/assemble", {"selections": [
                    {"source_id": source["id"], "block_id": b["id"]} for b in source["blocks"]]})
                return book, draft

            def nav(label):
                page.get_by_role("navigation", name="工作台导航").get_by_role("button", name=label, exact=True).click()

            def open_book(book):
                nav("图书与版本")
                page.get_by_placeholder("搜索书名或作者").fill(book["book"]["title"])
                page.locator(".book-card").filter(has_text=book["book"]["title"]).click()
                page.locator(".branch-row").first.click()
                expect(page.get_by_label("编辑第 1 块", exact=True)).to_be_visible()

            short, draft = seed("分支验收", "清晨，甲乙丙丁戊己庚辛壬癸。")
            long_book, long_draft = seed("长书验收", "甲😀乙丙丁。" * 1600)
            post("/api/settings", {"ai": {"token_budget": 20000, "max_output_tokens": 3000}})
            # A tiny valid raster is sufficient to verify actual protected bytes
            # decode in an img element, independently of OCR availability.
            def chunk(kind, payload):
                return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
            png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 20, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\0" + bytes([100, 140, 80]) * 32) * 20)) + chunk(b"IEND", b"")
            image_name = f"synthetic-page-{marker}.png"
            image_name = upload(image_name, png, "image/png")["name"]
            page.reload(wait_until="domcontentloaded")
            expect(page.locator(".version-label")).to_have_text("LOCAL WORKBENCH · 0.2.0")
            page.locator(".source-list-row").filter(has_text=image_name).click()
            page.get_by_role("button", name="核对原图", exact=True).click()
            image = page.locator(".source-resource img")
            expect(image).to_be_visible()
            assert image.evaluate("element => element.complete && element.naturalWidth === 32")
            with page.expect_download() as download:
                page.get_by_role("link", name="下载原件", exact=True).click()
            download.value.save_as(output / "synthetic-original.png")
            steps.append("authorized_source_raster_preview_and_original_download")

            open_book(short)
            page.locator(".revision-advanced > summary").click()
            page.get_by_label("新分支名称", exact=True).fill("UI 校订支线")
            page.get_by_role("button", name="从当前稿创建分支", exact=True).click()
            expect(page.get_by_label("整理分支", exact=True)).not_to_have_value(draft["branch_id"])
            fork_id = page.get_by_label("整理分支", exact=True).input_value()
            page.get_by_label("编辑第 1 块", exact=True).fill("清晨，右乙丙丁戊己庚辛壬癸。")
            page.get_by_role("button", name="保存修改（1）", exact=True).click()
            expect(page.get_by_label("查看修订", exact=True)).not_to_have_value(draft["revision_id"])
            expect(page.get_by_role("button", name="保存修改", exact=True)).to_be_disabled()
            right_id = page.get_by_label("查看修订", exact=True).input_value()
            page.get_by_label("整理分支", exact=True).select_option(draft["branch_id"])
            expect(page.get_by_label("编辑第 1 块", exact=True)).to_have_value("清晨，甲乙丙丁戊己庚辛壬癸。")
            page.get_by_label("编辑第 1 块", exact=True).fill("清晨，左乙丙丁戊己庚辛壬癸。")
            page.get_by_role("button", name="保存修改（1）", exact=True).click()
            expect(page.get_by_label("查看修订", exact=True)).not_to_have_value(draft["revision_id"])
            expect(page.get_by_role("button", name="保存修改", exact=True)).to_be_disabled()
            page.locator(".revision-advanced > summary").click()
            page.get_by_label("合入其他修订", exact=True).select_option(right_id)
            page.get_by_role("button", name="比较并合并", exact=True).click()
            expect(page.locator(".merge-conflict")).to_have_count(1)
            page.locator(".merge-conflict select").select_option("right")
            page.get_by_role("button", name="应用冲突选择并合并", exact=True).click()
            expect(page.get_by_label("编辑第 1 块", exact=True)).to_have_value("清晨，右乙丙丁戊己庚辛壬癸。")
            page.locator(".revision-advanced > summary").click()
            page.get_by_label("整理稿题名", exact=True).fill("已校订元数据" + marker)
            page.get_by_role("button", name="保存整理稿元数据", exact=True).click()
            expect(page.locator(".editor-heading h2")).to_have_text("已校订元数据" + marker)
            steps.append("fork_edit_select_merge_conflict_resolution_and_frozen_metadata")
            assert get("/api/revisions/" + right_id)["branch_id"] == fork_id
            while page.locator(".notice button").count():
                page.locator(".notice button").first.click()
            page.screenshot(path=str(output / "01-advanced.png"), full_page=True)

            open_book(long_book)
            page.get_by_role("button", name="AI 校订", exact=True).click()
            page.get_by_role("button", name="创建审阅请求", exact=True).click()
            expect(page.get_by_role("heading", name="离线审阅", exact=True)).to_be_visible()
            request_id = page.get_by_label("审阅请求", exact=True).input_value()
            request = get("/api/review-requests/" + request_id)
            count = request["payload"]["batch"]["count"]
            assert count > 1
            expect(page.get_by_text(f"共 {count} 片 · 当前第 1 片", exact=True)).to_be_visible()
            ids = [r["id"] for r in get("/api/state")["review_requests"] if r["revision_id"] == long_draft["revision_id"]]
            assert len(ids) == count
            last = next(get("/api/review-requests/" + i) for i in ids if get("/api/review-requests/" + i)["payload"]["batch"]["index"] == count)
            page.get_by_label("审阅请求", exact=True).select_option(last["id"])
            expect(page.get_by_text(f"共 {count} 片 · 当前第 {count} 片", exact=True)).to_be_visible()
            page.get_by_text("本片范围（原始块的 Unicode 码点）", exact=True).click()
            start = last["payload"]["blocks"][0]["start"]
            assert start > 0
            expect(page.locator(".provenance-details")).to_contain_text(str(start))
            with page.expect_download() as download:
                page.get_by_role("button", name="下载请求 JSON", exact=True).click()
            path = output / "last-slice-request.json"
            download.value.save_as(path)
            assert json.loads(path.read_text(encoding="utf-8"))["id"] == last["id"]
            steps.append("long_book_batch_count_status_original_ranges_and_last_slice_download")
            while page.locator(".notice button").count():
                page.locator(".notice button").first.click()
            page.screenshot(path=str(output / "02-batched-review.png"), full_page=True)
            assert not errors, errors
            receipt = {"passed": True, "synthetic_only": True, "external_ai_called": False, "steps": steps, "batch_count": count, "duration_seconds": round(time.time() - started, 2), "browser_errors": errors}
            (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(receipt, ensure_ascii=False))
        except Exception as exc:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            (output / "failure.json").write_text(json.dumps({"steps": steps, "error": str(exc), "browser_errors": errors}, ensure_ascii=False), encoding="utf-8")
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
