"""Exercise old revisions, branches, proposals and jobs through a real synthetic library."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


TEXT = "灯台值班记录：巡查员在清晨检查窗台和玻璃。"
UPDATED = TEXT.replace("清晨", "傍晚")
TITLE = "历史灯台验收整理稿"
SERVER = """
from pathlib import Path
import sys
import threading
import time
import uvicorn
from yuecang.api import create_app

library, port, marker = sys.argv[1:]
server = uvicorn.Server(uvicorn.Config(create_app(library), host='127.0.0.1', port=int(port), access_log=False))

def watch_for_shutdown():
    while not Path(marker).exists():
        time.sleep(0.1)
    server.should_exit = True

threading.Thread(target=watch_for_shutdown, daemon=True).start()
server.run()
"""


def hide_from_snapshot(library, draft, proposal_id):
    indexes = range(1001)
    now = time.time()
    with sqlite3.connect(library / "library.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executemany("INSERT INTO books(id,title,author,created) VALUES(?,?,?,?)",
                       [(f"catalog-book-{i}", f"合成占位图书 {i}", "", now) for i in indexes])
        db.executemany(
            "INSERT INTO revisions(id,book_id,edition_id,document_sha,metadata,created) "
            "SELECT ?,book_id,edition_id,document_sha,?,? FROM revisions WHERE id=?",
            [(f"catalog-revision-{i}", json.dumps({"title": f"合成占位修订 {i}"}), now, draft["revision_id"])
             for i in indexes],
        )
        db.executemany("INSERT INTO branches(id,book_id,name,head_revision_id) VALUES(?,?,?,?)",
                       [(f"catalog-branch-{i}", draft["revision"]["book_id"], f"合成占位分支 {i}",
                         f"catalog-revision-{i}") for i in indexes])
        db.executemany(
            "INSERT INTO proposals(id,request_id,revision_id,branch_id,operations,reason,risk,state,created) "
            "SELECT ?,request_id,revision_id,branch_id,operations,reason,risk,'rejected',? FROM proposals WHERE id=?",
            [(f"catalog-proposal-{i}", now, proposal_id) for i in indexes],
        )
        db.executemany(
            "INSERT INTO jobs(id,kind,payload,state,input_key,created,updated) VALUES(?,'import','{}','succeeded',?,?,?)",
            [(f"catalog-job-{i}", f"catalog-job-{i}", now, now) for i in indexes],
        )


def exercise(page, base, library, output, checks):
    request = page.context.request
    headers = {"X-Yuecang-CSRF": request.get(base + "/api/session").json()["csrf_token"]}

    def get(path):
        response = request.get(base + path)
        assert response.ok, response.text()
        return response.json()

    def post(path, data):
        response = request.post(base + path, headers=headers, data=data)
        assert response.ok, response.text()
        return response.json()

    def wait_job(job_id):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            job = get(f"/api/jobs/{job_id}")
            if job["state"] == "succeeded":
                return job["result"]
            assert job["state"] not in {"failed", "cancelled", "needs_attention"}, job
            time.sleep(0.1)
        raise AssertionError(f"Synthetic job did not complete: {job_id}")

    def nav(label):
        page.get_by_role("navigation", name="工作台导航").get_by_role("button", name=label, exact=True).click()

    upload = request.post(base + "/api/imports", headers=headers, multipart={
        "files": {"name": "synthetic-catalog.txt", "mimeType": "text/plain", "buffer": TEXT.encode()},
        "encoding": "utf-8",
    })
    assert upload.ok, upload.text()
    import_id = upload.json()["jobs"][0]["id"]
    source = get("/api/sources/" + wait_job(import_id)["source_id"])
    source_span = {"source_id": source["id"], "block_id": source["blocks"][0]["id"]}
    book = post("/api/books", {"title": "历史灯台验收", "edition": "旧版"})
    draft = post(f"/api/books/{book['id']}/assemble", {"selections": [source_span], "title": TITLE})
    revision_id, branch_id = draft["revision_id"], draft["branch_id"]
    review = post(f"/api/revisions/{revision_id}/review", {"provider": "offline", "branch_id": branch_id})
    proposal_id = post(f"/api/review-requests/{review['id']}/response", {"corrections": [{
        "block_id": draft["revision"]["blocks"][0]["id"], "start": 0, "end": 2,
        "old_text": TEXT[:2], "new_text": "灯塔", "reason": "合成旧校订",
        "evidence": [{**source_span, "start": 0, "end": 2}],
    }]})["proposal_ids"][0]
    hide_from_snapshot(library, draft, proposal_id)
    state = get("/api/state")
    for collection, old_id in (("books", book["id"]), ("revisions", revision_id),
                               ("branches", branch_id), ("proposals", proposal_id), ("jobs", import_id)):
        assert state["totals"][collection] == 1002
        assert all(row["id"] != old_id for row in state[collection])

    page.goto(base)
    nav("校订待办")
    page.get_by_label("搜索修订或校订编号", exact=True).fill(proposal_id)
    proposal = page.locator(".proposal-card", has_text="合成旧校订")
    expect(proposal).to_have_count(1)
    proposal.get_by_role("button", name="查看整理稿", exact=True).click()
    expect(page.get_by_label("目标图书", exact=True)).to_have_value(book["id"])
    expect(page.get_by_label("整理分支", exact=True)).to_have_value(branch_id)
    editor = page.get_by_label("编辑第 1 块", exact=True)
    expect(editor).to_be_editable()
    expect(editor).to_have_value(TEXT)
    editor.fill(UPDATED)
    with page.expect_response(lambda response: response.url.endswith(f"/branches/{branch_id}/edits")) as saved:
        page.get_by_role("button", name=re.compile("^保存修改")).click()
    assert saved.value.ok, saved.value.text()
    new_id = saved.value.json()["revision_id"]
    assert get(f"/api/branches/{branch_id}")["head_revision_id"] == new_id
    assert get(f"/api/revisions/{revision_id}")["blocks"][0]["text"] == TEXT
    expect(editor).to_have_value(UPDATED)
    checks.append("old_proposal_opens_editable_branch_and_saves_new_revision")

    page.get_by_text("搜索与翻页 · 整理分支和修订", exact=True).click()
    page.get_by_label("按题名或编号搜索本书修订", exact=True).fill(revision_id)
    page.get_by_label("查看修订", exact=True).select_option(revision_id)
    expect(editor).to_have_value(TEXT)
    expect(editor).not_to_be_editable()
    page.get_by_role("button", name="查看当前稿", exact=True).click()
    expect(editor).to_have_value(UPDATED)
    checks.append("book_scoped_history_search_opens_old_revision_and_returns_to_head")

    nav("整理稿导出")
    page.get_by_label("按书名或编号搜索整理稿", exact=True).fill(revision_id)
    page.get_by_label("导出整理稿", exact=True).select_option(revision_id)
    page.get_by_role("button", name=re.compile("^纯文本")).click()
    page.get_by_label("导出带问题说明的草稿；此操作不会将其定版。", exact=True).check()
    with page.expect_response(lambda response: response.url.endswith("/api/exports") and response.request.method == "POST") as exported:
        page.get_by_role("button", name="生成导出", exact=True).click()
    assert exported.value.ok, exported.value.text()
    wait_job(exported.value.json()["job"]["id"])
    page.get_by_label("按书名或编号搜索导出记录", exact=True).fill(revision_id)
    entry = page.locator(".export-record")
    expect(entry).to_have_count(1)
    with page.expect_download() as download:
        entry.get_by_role("link", name="下载文件", exact=True).click()
    path = output / "old-revision.txt"
    download.value.save_as(path)
    contents = path.read_text(encoding="utf-8")
    assert TEXT in contents and UPDATED not in contents
    checks.append("search_select_export_and_download_old_revision")

    nav("任务中心")
    page.get_by_role("button", name="已完成", exact=True).click()
    page.get_by_label("搜索任务编号", exact=True).fill(import_id)
    expect(page.locator(".job-card")).to_have_count(1)
    expect(page.locator(".job-card")).to_contain_text(import_id[:8])
    checks.append("grouped_server_filter_and_search_find_old_job")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-python", required=True)
    parser.add_argument("--product-src", type=Path, default=Path(__file__).resolve().parents[2] / "src")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--port", type=int, default=18767)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    library = output / "library"
    shutdown = output / ".stop-server"
    base = f"http://127.0.0.1:{args.port}"
    checks, errors = [], []
    receipt = {"passed": False, "synthetic_only": True, "checks": checks, "page_errors": errors}
    with (output / "server.log").open("x", encoding="utf-8") as log:
        server = subprocess.Popen(
            [args.product_python, "-B", "-c", SERVER, str(library), str(args.port), str(shutdown)],
            env={**os.environ, "PYTHONPATH": str(args.product_src.resolve())}, stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert server.poll() is None, "Owned server exited during startup"
                try:
                    with urlopen(base + "/api/session", timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("Owned server startup timed out")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=args.browser)
                page = browser.new_page(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    exercise(page, base, library, output, checks)
                    assert not errors, errors
                    receipt["passed"] = True
                except Exception:
                    page.screenshot(path=output / "failure.png", full_page=True)
                    raise
                finally:
                    browser.close()
        except Exception as error:
            receipt["failure"] = str(error)
            raise
        finally:
            if server.poll() is None:
                shutdown.touch()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.terminate()
                    server.wait(timeout=10)
            receipt["server_exit_code"] = server.returncode
            receipt["passed"] &= server.returncode == 0
            (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    if not receipt["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
