"""Real-browser acceptance against a running, disposable local workbench.

Run with the QA guest's Playwright Python; only synthetic books are imported.
No real AI endpoint is contacted. The local HTTP backend and worker must run.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import time

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--output", required=True)
    parser.add_argument("--browser", default="")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = str(time.time_ns())[-9:]
    book_title = f"山海札记 · UI 验收 {run_id}"
    fixture = output / f"synthetic-source-{run_id}.md"
    fixture.write_text(f"# 山海札记 {run_id}\n\n晨光从窗前洒落，照亮了书页。\n\n我们在沿海的小镇记下了这次旅程。\n", encoding="utf-8")
    second = output / f"synthetic-variant-{run_id}.md"
    second.write_text(f"# 山海札记 {run_id}\n\n晨光从窗前洒落，照亮了书页。\n\n我们在沿海的小镇记下了这次旅程。\n\n远方的灯塔已经亮起。\n", encoding="utf-8")
    events = []
    errors = []
    started = time.time()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, **({"executable_path": args.browser} if args.browser else {}))
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def step(name):
            events.append({"step": name, "elapsed_seconds": round(time.time() - started, 2)})

        def nav(label):
            page.get_by_role("navigation", name="工作台导航").get_by_role("button", name=label, exact=True).click()

        def state():
            return page.evaluate("async () => (await fetch('/api/state')).json()")

        try:
            page.goto(args.base_url, wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="从每一份来源开始")).to_be_visible()
            step("initial_screen")
            page.get_by_label("导入来源文件").set_input_files([str(fixture), str(second)])
            expect(page.locator(".source-list-row").filter(has_text=fixture.name)).to_be_visible(timeout=60000)
            expect(page.locator(".source-list-row").filter(has_text=second.name)).to_be_visible(timeout=60000)
            page.locator(".source-list-row").filter(has_text=fixture.name).click()
            expect(page.locator(".source-preview .source-block").first).to_be_visible()
            source_blocks = page.locator(".source-preview .source-block")
            source_block_count = source_blocks.count()
            target_ordinal = next(index + 1 for index in range(source_block_count)
                if source_blocks.nth(index).locator("textarea").count()
                and "晨光从窗前洒落" in source_blocks.nth(index).locator("textarea").input_value())
            editor_label = f"编辑第 {target_ordinal} 块"
            page.get_by_role("button", name="分析候选", exact=True).click()
            expect(page.locator(".related-candidates")).to_be_visible(timeout=60000)
            page.get_by_label("来源块 2 处置", exact=True).select_option("ignored")
            expect(page.locator(".source-preview .source-block").nth(1).locator(".disposition-row > span")).to_have_text("明确忽略")
            page.get_by_role("button", name="选择本页", exact=True).click()
            step("import_and_select_source_blocks")

            nav("图书与版本")
            page.get_by_role("button", name="建立图书", exact=True).click()
            page.get_by_label("书名", exact=True).fill(book_title)
            page.get_by_label("作者", exact=True).fill("合成作者")
            page.get_by_role("button", name="建立", exact=True).click()
            expect(page.locator(".book-detail").get_by_role("heading", name=book_title)).to_be_visible()
            page.get_by_label("添加版本", exact=True).fill("对照版")
            page.get_by_role("button", name="添加版本", exact=True).click()
            expect(page.locator(".edition-row").filter(has_text="对照版")).to_be_visible()
            page.get_by_role("button", name="进入整理台", exact=True).click()
            expect(page.locator(".tray-item")).to_have_count(source_block_count)
            page.get_by_label("右侧来源", exact=True).select_option(label=second.name)
            page.get_by_role("button", name="比较左右来源", exact=True).click()
            expect(page.get_by_text("存在差异，需确认版本关系", exact=True)).to_be_visible()
            while page.locator(".notice button").count():
                page.locator(".notice button").first.click()
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "01-comparison.png"), full_page=True)
            step("book_edition_and_comparison")

            page.get_by_role("button", name="生成整理稿", exact=True).click()
            expect(page.get_by_label(editor_label, exact=True)).to_be_visible()
            page.get_by_label(editor_label, exact=True).fill("辰光从窗前洒落，照亮了书页。")
            page.get_by_role("button", name="来源对照", exact=True).click()
            page.locator(".tab-switch").get_by_role("button", name=re.compile("^整理稿")).click()
            expect(page.get_by_label(editor_label, exact=True)).to_have_value("辰光从窗前洒落，照亮了书页。")
            page.get_by_role("button", name="保存修改（1）", exact=True).click()
            expect(page.get_by_role("button", name="保存修改", exact=True)).to_be_disabled()
            step("assemble_edit_and_save_revision")

            page.get_by_role("button", name="AI 校订", exact=True).click()
            page.get_by_label("审阅方式", exact=True).select_option("offline")
            page.get_by_role("button", name="创建审阅请求", exact=True).click()
            expect(page.get_by_role("heading", name="离线审阅", exact=True)).to_be_visible()
            request_id = page.get_by_label("审阅请求", exact=True).input_value()
            request = page.evaluate("async id => (await fetch('/api/review-requests/' + id)).json()", request_id)
            payload = request.get("payload", request)
            target = next(block for block in payload["blocks"] if block["text"].startswith("辰光"))
            def evidence_span(block):
                return next((span for span in block.get("spans", [{"start": 0, "text": block.get("text", "")}]) if span["text"].startswith("晨光")), None)
            source = next(source for source in payload["sources"] if any(evidence_span(block) for block in source["blocks"]))
            evidence = next(block for block in source["blocks"] if evidence_span(block))
            evidence_start = evidence_span(evidence)["start"]
            response = {"corrections": [{"block_id": target["id"], "start": 0, "end": 1,
                "old_text": "辰", "new_text": "晨", "reason": "与导入来源逐字对照，恢复原字。",
                "evidence": [{"source_id": source["id"], "block_id": evidence["id"], "start": evidence_start, "end": evidence_start + 1}]}]}
            page.get_by_label("导入结构化响应", exact=True).fill(json.dumps(response, ensure_ascii=False))
            page.get_by_role("button", name="校验并导入提案", exact=True).click()
            expect(page.locator(".proposal-card").filter(has_text="恢复原字")).to_be_visible()
            expect(page.locator(".diff-before").filter(has_text="辰")).to_be_visible()
            while page.locator(".notice button").count():
                page.locator(".notice button").first.click()
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "02-review.png"), full_page=True)
            page.get_by_role("button", name="接受修改", exact=True).click()
            expect(page.get_by_role("heading", name="没有待审阅的校订", exact=True)).to_be_visible()
            step("offline_review_and_accept_evidence_bound_patch")

            nav("图书与版本")
            page.locator(".branch-row").first.click()
            expect(page.get_by_label(editor_label, exact=True)).to_have_value("晨光从窗前洒落，照亮了书页。")
            page.get_by_role("button", name="定版", exact=True).click()
            expect(page.locator(".editor-heading .badge")).to_have_text("已定版")
            nav("整理稿导出")
            page.get_by_role("button", name="生成导出", exact=True).click()
            download_link = page.locator(".export-record").first.get_by_role("link", name="下载文件")
            expect(download_link).to_be_visible(timeout=60000)
            with page.expect_download() as download_info:
                download_link.click()
            download = download_info.value
            download.save_as(output / ("download-" + download.suggested_filename))
            step("finalize_export_and_download")

            nav("项目设置")
            page.get_by_label("服务地址", exact=True).fill("http://127.0.0.1:1/v1/chat/completions")
            page.get_by_label("模型名称", exact=True).fill("synthetic-never-called")
            page.get_by_label("单次请求 token 预算", exact=True).fill("25000")
            page.get_by_role("button", name="保存模型设置", exact=True).click()
            expect(page.get_by_text("模型设置已保存。", exact=True)).to_be_visible()
            page.get_by_role("button", name="创建项目备份", exact=True).click()
            step("settings_and_backup_request")

            context.set_default_timeout(20000)
            page.set_viewport_size({"width": 900, "height": 850})
            nav("来源与导入")
            while page.locator(".notice button").count():
                page.locator(".notice button").first.click()
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "03-compact-layout.png"), full_page=True)
            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 2")
            assert not overflow, "900px viewport has horizontal page overflow"
            assert not errors, f"browser JavaScript errors: {errors}"
            report = {"passed": True, "synthetic_only": True, "external_ai_called": False,
                "steps": events, "browser_errors": errors, "duration_seconds": round(time.time() - started, 2),
                "counts": {key: len(value) for key, value in state().items() if isinstance(value, list)}}
            (output / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"passed": True, "steps": len(events), "duration_seconds": report["duration_seconds"]}))
        except Exception as exc:
            page.screenshot(path=str(output / "failure.png"), full_page=True)
            (output / "failure.json").write_text(json.dumps({"passed": False, "steps": events,
                "error": str(exc), "browser_errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
