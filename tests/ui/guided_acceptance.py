"""Browser acceptance for guided organization and curation in a fresh synthetic library."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


ANCHOR_A = "风帆记录从第一座灯塔开始，所有测量都以港口水位和潮汐观测为参照。"
ANCHOR_B = "第二座灯塔采用白色石灰岩修建，正门向海，北窗可用于记录晨间风向。"
ANCHOR_C = "回程日志按巡查日历整理，入港前再次复核灯号及当日水位观测记录。"
ORIGINAL = "第三天记录水位三尺，船员随后补抄了船尾标尺读数与样本序号。"
VARIANT = "第三天记录水位五尺，船员随后补抄了船尾标尺读数与样本序号。"
ADDITION = "检修员在第二座灯塔附记备用油箱的位置，交班时还要复核库房钥匙。"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-python", required=True)
    parser.add_argument("--product-src", type=Path, default=Path(__file__).resolve().parents[2] / "src")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--port", type=int, default=18766)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    library = output / "library"
    url = f"http://127.0.0.1:{args.port}"
    checks, errors = [], []
    receipt = {"passed": False, "synthetic_only": True, "checks": checks, "page_errors": errors}
    env = {**os.environ, "PYTHONPATH": str(args.product_src.resolve())}
    with (output / "server.log").open("x", encoding="utf-8") as log:
        server = subprocess.Popen(
            [args.product_python, "-B", "-m", "yuecang", "--project", str(library),
             "serve", "--port", str(args.port), "--no-browser"],
            env=env, stdout=log, stderr=log, start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert server.poll() is None, "Owned server exited"
                try:
                    with urlopen(url + "/api/session", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("Owned server startup timed out")

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=args.browser)
                context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
                page = context.new_page()
                page.set_default_timeout(20000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                session = context.request.get(url + "/api/session").json()
                headers = {"X-Yuecang-CSRF": session["csrf_token"]}

                def get(path):
                    response = context.request.get(url + path)
                    assert response.ok, response.text()
                    return response.json()

                def post(path, data):
                    response = context.request.post(url + path, headers=headers, data=data)
                    assert response.ok, response.text()
                    return response.json()

                def import_source(name, text):
                    response = context.request.post(url + "/api/imports", headers=headers,
                        multipart={"files": {"name": name, "mimeType": "text/plain", "buffer": text.encode()},
                                   "encoding": "utf-8"})
                    assert response.ok, response.text()
                    job_id = response.json()["jobs"][0]["id"]
                    timeout = time.monotonic() + 60
                    while time.monotonic() < timeout:
                        job = get(f"/api/jobs/{job_id}")
                        assert job["state"] not in {"failed", "cancelled", "needs_attention"}, job
                        if job["state"] == "succeeded":
                            return get("/api/sources/" + job["result"]["source_id"])
                        time.sleep(0.1)
                    raise AssertionError(f"Import timed out: {name}")

                def nav(label):
                    page.get_by_role("navigation", name="工作台导航").get_by_role("button", name=label, exact=True).click()

                def source_row(source):
                    return page.locator(f'.source-list-row[data-source-id="{source["id"]}"]')

                cold = import_source("冷启动来源.txt", "合成资料仅用于浏览器验收。日出后开启箱盖，观察种子萌发与培养皿中水滴的变化。")
                page.goto(url)
                expect(source_row(cold)).to_be_visible()
                page.get_by_role("button", name="批量归属", exact=True).click()
                page.get_by_label("选择归属检查来源 冷启动来源.txt").check()
                page.get_by_role("button", name="检查所选来源", exact=True).click()
                panel = page.locator(".guided-panel").filter(has=page.get_by_role("heading", name="批量检查归属"))
                expect(panel.get_by_text("冷启动来源.txt · 暂无匹配")).to_be_visible()
                expect(panel.get_by_role("button", name="保存 0 项决定", exact=True)).to_be_disabled()
                panel.get_by_label("本条决定").select_option("confirm")
                panel.get_by_label("归属目标").select_option("new")
                panel.get_by_label("新书名").fill("冷启动新书")
                panel.get_by_label("版本名称").fill("初校版")
                panel.screenshot(path=str(output / "assignment-confirmation.png"))
                panel.get_by_role("button", name="保存 1 项决定", exact=True).click()
                expect(panel.get_by_text("已确认 1 项，已拒绝 0 项，待处理 0 项。")).to_be_visible()
                cold_assignment = get(f"/api/sources/{cold['id']}/assignments")["items"][0]
                assert cold_assignment["book_title"] == "冷启动新书"
                checks.append("cold_start_explicit_confirm")

                page.reload()
                page.get_by_role("button", name="批量归属", exact=True).click()
                panel.locator("summary", has_text="历史计划").click()
                panel.get_by_role("button", name="打开计划", exact=True).first.click()
                expect(panel.get_by_text("已确认 1 项，已拒绝 0 项，待处理 0 项。")).to_be_visible()
                checks.append("assignment_plan_history_after_reload")

                book = post("/api/books", {"title": "灯塔整理验收", "author": "检索专用作者", "edition": "同版初校"})
                base = import_source("底本灯塔.txt", "\n\n".join((ANCHOR_A, ORIGINAL, ANCHOR_B, ANCHOR_C)))
                donor = import_source("补充灯塔.txt", "\n\n".join((ANCHOR_A, VARIANT, ANCHOR_B, ADDITION, ANCHOR_C)))
                for source in (base, donor):
                    post("/api/assignments", {"book_id": book["id"], "edition_id": book["edition_id"], "spans": [
                        {"source_id": source["id"], "block_id": block["id"]} for block in source["blocks"]
                    ]})

                page.reload()
                source_row(cold).click()
                assignments = page.locator(".source-preview details").filter(
                    has=page.locator("summary", has_text="当前归属")).first
                assignments.locator("summary").first.click()
                expect(assignments.get_by_text("冷启动新书 · 初校版")).to_be_visible()
                assignments.get_by_role("button", name="更正归属", exact=True).click()
                assignments.get_by_label("搜索图书", exact=True).fill("检索专用作者")
                assignments.get_by_label("目标图书", exact=True).select_option(book["id"])
                assignments.get_by_label("目标版本", exact=True).select_option(book["edition_id"])
                assignments.get_by_role("button", name="保存更正", exact=True).click()
                expect(assignments.get_by_text("灯塔整理验收 · 同版初校")).to_be_visible()
                assignments.get_by_role("button", name="解除归属", exact=True).click()
                assignments.get_by_role("button", name="确认解除", exact=True).click()
                expect(assignments.get_by_text("这份来源暂无已确认的归属区间。")).to_be_visible()
                assert get(f"/api/sources/{cold['id']}")["asset_sha"] == cold["asset_sha"]
                checks.append("rebind_then_unassign_exact_range")

                nav("整理工作台")
                page.get_by_label("目标图书", exact=True).select_option(book["id"])
                page.get_by_label("目标版本", exact=True).select_option(book["edition_id"])
                curation = page.locator("details.guided-panel", has=page.locator("summary", has_text="以底本组稿"))
                curation.locator("summary").first.click()
                curation.get_by_label("底本来源", exact=True).select_option(base["id"])
                curation.get_by_label("补充来源（最多四份）", exact=True).select_option(donor["id"])
                curation.get_by_role("button", name="生成组稿计划", exact=True).click()
                expect(curation.get_by_text("内容一致，保留底本", exact=True).first).to_be_visible()
                conflict = curation.locator("article").filter(has_text="有差异，待选择")
                addition = curation.locator("article").filter(has_text="有可选补充")
                expect(conflict).to_be_visible()
                expect(addition).to_be_visible()
                conflict.locator("summary", has_text="查看原文区间与资源").last.click()
                expect(conflict.locator("details[open] pre")).to_have_text(VARIANT)
                expect(curation.get_by_role("button", name="确认生成整理稿", exact=True)).to_be_disabled()
                conflict.get_by_role("radio", name="保留底本", exact=True).check()
                addition.get_by_role("radio", name="采用 补充灯塔.txt", exact=True).check()
                curation.get_by_label("整理稿标题（可选）").fill("灯塔验收整理稿")
                curation.get_by_role("button", name="确认生成整理稿", exact=True).click()
                expect(page.get_by_label("编辑第 1 块", exact=True)).to_be_visible()
                revisions = get(f"/api/revisions?book_id={book['id']}")["items"]
                revision = get(f"/api/revisions/{revisions[0]['id']}")
                expected = [block["text"] for block in base["blocks"]]
                expected.insert(3, donor["blocks"][3]["text"])
                assert [block["text"] for block in revision["blocks"]] == expected
                assert revision["blocks"][3]["source_span"]["source_id"] == donor["id"]
                checks.append("curation_exact_duplicate_conflict_and_anchored_addition")

                page.reload()
                nav("整理工作台")
                curation.locator("summary").first.click()
                curation.locator("summary", has_text="历史计划").click()
                curation.get_by_role("button", name="打开计划", exact=True).first.click()
                curation.get_by_role("button", name="打开整理稿", exact=True).click()
                expect(page.get_by_label("编辑第 1 块", exact=True)).to_be_visible()
                checks.append("curation_history_opens_existing_draft")

                alternate = post(f"/api/books/{book['id']}/editions", {"label": "同书另一版"})
                assigned = get(f"/api/sources/{donor['id']}/assignments")
                for row in assigned["items"]:
                    expected = {key: row[key] for key in ("source_id", "block_id", "start", "end", "book_id", "edition_id")}
                    response = context.request.patch(url + f"/api/assignments/{row['id']}", headers=headers, data={
                        "expected": {**expected, "blocks_sha": assigned["blocks_sha"]},
                        "target": {"book_id": book["id"], "edition_id": alternate["id"]},
                    })
                    assert response.ok, response.text()
                page.reload()
                nav("整理工作台")
                page.get_by_label("目标图书", exact=True).select_option(book["id"])
                page.get_by_label("目标版本", exact=True).select_option(book["edition_id"])
                curation.locator("summary").first.click()
                donor_select = curation.get_by_label("补充来源（最多四份）", exact=True)
                curation.get_by_label("底本来源", exact=True).select_option(base["id"])
                expect(donor_select.locator(f'option[value="{donor["id"]}"]')).to_have_count(0)
                curation.get_by_label("允许同一本书不同版本参与整理，保留版本区别", exact=True).check()
                donor_select.select_option(donor["id"])
                curation.get_by_role("button", name="生成组稿计划", exact=True).click()
                expect(curation.get_by_text("跨版本综合整理", exact=True)).to_be_visible()
                expect(curation.get_by_text("补充灯塔.txt（同书另一版）", exact=False)).to_be_visible()
                conflict.get_by_role("radio", name="保留底本", exact=True).check()
                addition.get_by_role("radio", name="采用 补充灯塔.txt", exact=True).check()
                curation.screenshot(path=str(output / "composite-curation.png"))
                curation.get_by_role("button", name="确认生成整理稿", exact=True).click()
                expect(page.get_by_label("编辑第 1 块", exact=True)).to_be_visible()
                revisions = get(f"/api/revisions?book_id={book['id']}")["items"]
                composite = get(f"/api/revisions/{revisions[0]['id']}")
                assert composite["metadata"]["assembly_policy"] == "composite-confirmed"
                assert composite["metadata"]["source_editions"][alternate["id"]]["label"] == "同书另一版"
                assert composite["blocks"][3]["source_edition_ranges"] == [{
                    **composite["blocks"][3]["source_span"], "edition_id": alternate["id"],
                }]
                assert {row["edition_id"] for row in get(f"/api/sources/{donor['id']}/assignments")["items"]} == {alternate["id"]}
                checks.append("explicit_composite_curation_preserves_original_edition")

                corrected = ANCHOR_A + "整理者复核。"
                page.get_by_label("编辑第 1 块", exact=True).fill(corrected)
                page.get_by_role("button", name="保存修改（1）", exact=True).click()
                expect(page.get_by_role("button", name="保存修改", exact=True)).to_be_disabled()
                page.get_by_role("button", name="定版", exact=True).click()
                expect(page.locator(".editor-heading .badge")).to_have_text("已定版")
                final_id = get(f"/api/revisions?book_id={book['id']}")["items"][0]["id"]
                final = get(f"/api/revisions/{final_id}")
                assert final["blocks"][3]["source_edition_ranges"] == composite["blocks"][3]["source_edition_ranges"]
                nav("整理稿导出")
                page.get_by_role("button", name="生成导出", exact=True).click()
                with page.expect_download() as downloaded:
                    page.locator(".export-record").first.get_by_role("link", name="下载").click(timeout=60000)
                assert corrected in Path(downloaded.value.path()).read_text(encoding="utf-8")
                checks.append("guided_draft_edit_finalize_export")

                with sqlite3.connect(library / "library.sqlite3") as db:
                    db.executemany("INSERT INTO books(id,title,author,created) VALUES(?,?,?,?)",
                        [(f"filler-book-{i}", f"合成填充图书 {i}", "", time.time()) for i in range(1001)])
                    db.executemany("INSERT INTO editions(id,book_id,label,language) VALUES(?,?,?,?)",
                        [(f"filler-edition-{i}", f"filler-book-{i}", "合成填充版本", "zh") for i in range(1001)])
                    db.executemany(
                        "INSERT INTO sources(id,asset_sha,name,status,blocks_sha,block_count,metadata,issues,parser,created) "
                        "SELECT ?,asset_sha,?,status,blocks_sha,block_count,'{}',issues,?,? FROM sources WHERE id=?",
                        [(f"filler-source-{i}", f"合成填充来源 {i}", f"synthetic-filler-{i}", time.time(), cold["id"])
                         for i in range(1001)])
                snapshot = get("/api/state")
                assert cold["id"] not in {item["id"] for item in snapshot["sources"]}
                assert book["id"] not in {item["id"] for item in snapshot["books"]}
                assert book["edition_id"] not in {item["id"] for item in snapshot["editions"]}
                page.reload()
                page.get_by_placeholder("查找文件").fill("冷启动来源")
                expect(source_row(cold)).to_be_visible()
                source_row(cold).click()
                expect(page.locator(".source-preview .source-block").first).to_be_visible()
                nav("图书与版本")
                page.get_by_placeholder("搜索书名或作者").fill("检索专用作者")
                page.locator(".book-card", has_text="灯塔整理验收").click()
                page.get_by_role("button", name="进入整理台", exact=True).click()
                expect(page.get_by_label("目标图书", exact=True)).to_have_value(book["id"])
                page.get_by_label("目标版本", exact=True).select_option(book["edition_id"])
                checks.append("server_search_beyond_snapshot_by_filename_and_author")
                assert not errors, errors
                page.screenshot(path=str(output / "guided-workbench.png"), full_page=True)
                context.close()
                browser.close()
                receipt["passed"] = True
        except Exception as failure:
            receipt["failure"] = str(failure)
            raise
        finally:
            if server.poll() is None:
                server.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.terminate()
                    server.wait(timeout=10)
            (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
