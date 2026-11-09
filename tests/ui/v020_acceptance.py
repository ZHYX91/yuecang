"""Synthetic 0.2 browser acceptance; owns one local server and one new output root.

Run with an existing Playwright environment, passing the product interpreter.
No remote model service is contacted. Existing libraries are never opened.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


ALIGNMENT_TAIL = "合成验收尾部：新叶展开后复核记录。"
A1 = ("城市植物手册记载幼苗每日需观察叶片颜色、土壤湿度和根部状态，避免连续大量浇水。"
      + "记录光照时段和盆土变化。" * 24 + ALIGNMENT_TAIL)
A2 = "种植者应在清晨记录植株高度和新叶数量，根据当地季节安排移栽并保留观察日志。"
B3 = "远航记录第三节说明船员如何利用星图校验方向，夜间轮班需检查罗盘与甲板灯光。"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-python", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--libreoffice", default="")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixtures = output / "fixtures"
    fixtures.mkdir()
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", args.port))
    started = time.monotonic()
    base = f"http://127.0.0.1:{args.port}"
    checks, errors = [], []
    receipt = {"passed": False, "synthetic_only": True, "external_ai_called": False, "checks": checks}
    server_environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    with (output / "server.log").open("x", encoding="utf-8") as log:
        server = subprocess.Popen([args.product_python, "-B", "-m", "yuecang", "--project", str(output / "library"),
                                   "serve", "--port", str(args.port), "--no-browser"], stdout=log, stderr=log,
                                  env=server_environment, start_new_session=os.name != "nt")
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise AssertionError("owned server exited during startup")
                try:
                    with urlopen(base + "/api/session", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(.1)
            else:
                raise AssertionError("owned server startup timeout")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=args.browser)
                context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
                page = context.new_page()
                page.set_default_timeout(20000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    page.goto(base, wait_until="domcontentloaded")
                    expect(page.get_by_role("heading", name="从每一份来源开始")).to_be_visible()
                    headers = {"X-Yuecang-CSRF": context.request.get(base + "/api/session").json()["csrf_token"]}

                    def get(path):
                        response = context.request.get(base + path)
                        assert response.ok, response.text()
                        return response.json()

                    def post(path, body):
                        response = context.request.post(base + path, headers=headers, data=body)
                        assert response.ok, response.text()
                        return response.json()

                    def job_result(job):
                        deadline = time.monotonic() + 60
                        while time.monotonic() < deadline:
                            row = next(item for item in get("/api/state")["jobs"] if item["id"] == job["id"])
                            assert row["state"] not in {"failed", "cancelled", "needs_attention"}, row
                            if row["state"] == "succeeded":
                                return get("/api/sources/" + row["result"]["source_id"])
                            page.wait_for_timeout(100)
                        raise AssertionError("synthetic job timeout")

                    def upload_api(name, text):
                        response = context.request.post(base + "/api/imports", headers=headers,
                            multipart={"files": {"name": name, "mimeType": "text/plain", "buffer": text.encode()}, "encoding": "utf-8"})
                        assert response.ok, response.text()
                        return job_result(response.json()["jobs"][0])

                    def upload_ui(path):
                        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/imports")) as uploaded:
                            page.get_by_label("导入来源文件", exact=True).set_input_files(str(path))
                        assert uploaded.value.ok, uploaded.value.text()
                        return job_result(uploaded.value.json()["jobs"][0])

                    def show_source(source_id):
                        page.reload(wait_until="domcontentloaded")
                        page.locator(f'.source-list-row[data-source-id="{source_id}"]').click()
                        expect(page.locator(".source-preview .source-block").first).to_be_visible()

                    def recovery():
                        panel = page.locator(".source-tools").filter(has=page.locator("summary", has_text="重新解码与乱码修复"))
                        panel.locator("summary").first.click()
                        return panel

                    def preview(panel):
                        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/text-preview")) as completed:
                            panel.get_by_role("button", name="预览处理结果", exact=True).click()
                        assert completed.value.ok, completed.value.text()
                        expect(panel.get_by_label("修复后预览", exact=True)).to_be_visible()
                        return completed.value.json()

                    def apply(panel):
                        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/text-apply")) as completed:
                            panel.get_by_role("button", name="确认生成新来源", exact=True).click()
                        assert completed.value.ok, completed.value.text()
                        return job_result(completed.value.json()["job"])

                    page.locator(".import-options > summary").click()
                    page.get_by_label("导入文本编码", exact=True).select_option("gb18030")
                    gb = fixtures / "synthetic-gb18030.txt"
                    gb.write_bytes("中文编码验收，原件保持不变。".encode("gb18030"))
                    gb_source = upload_ui(gb)
                    assert "中文编码验收" in "\n".join(block["text"] for block in gb_source["blocks"])
                    checks.append("ui_import_explicit_gb18030")

                    page.get_by_label("导入文本编码", exact=True).select_option("latin-1")
                    wrong = fixtures / "synthetic-wrong-decoding.txt"
                    correct = "中文测试"
                    wrong.write_bytes(correct.encode())
                    original = upload_ui(wrong)
                    assert original["blocks"][0]["text"] != correct
                    show_source(original["id"])
                    panel = recovery()
                    panel.get_by_label("重新读取编码", exact=True).select_option("utf-8")
                    data = preview(panel)
                    assert data["changed"] is True and data["preview"] == correct
                    expect(panel.get_by_label("修复前预览", exact=True)).not_to_have_value(correct)
                    panel.get_by_label("重新读取编码", exact=True).select_option("big5")
                    expect(panel.get_by_role("button", name="确认生成新来源", exact=True)).to_be_disabled()
                    panel.get_by_label("重新读取编码", exact=True).select_option("utf-8")
                    preview(panel)
                    page.screenshot(path=str(output / "01-redecode-preview.png"), full_page=True)
                    derived = apply(panel)
                    assert derived["id"] != original["id"] and derived["asset_sha"] == original["asset_sha"]
                    assert derived["blocks"][0]["text"] == correct
                    assert get("/api/sources/" + original["id"])["blocks"] == original["blocks"]
                    assert derived["name"] == original["name"]
                    show_source(derived["id"])
                    expect(page.locator(f'.source-list-row[data-source-id="{derived["id"]}"]')).to_contain_text("重新解码")
                    expect(page.locator(f'.source-list-row[data-source-id="{derived["id"]}"]')).to_contain_text(derived["id"][:8])
                    checks.append("preview_bound_redecode_creates_new_source_and_preserves_original")

                    mojibake = upload_api("synthetic-mojibake.txt", correct.encode().decode("latin-1"))
                    show_source(mojibake["id"])
                    panel = recovery()
                    panel.get_by_label("重新读取编码", exact=True).select_option("utf-8")
                    panel.get_by_label("文本修复方式", exact=True).select_option("utf8-as-latin1")
                    assert preview(panel)["preview"] == correct
                    repaired = apply(panel)
                    assert repaired["blocks"][0]["text"] == correct
                    assert repaired["metadata"]["derived_from"] == mojibake["id"]
                    assert repaired["name"] == mojibake["name"]
                    show_source(repaired["id"])
                    expect(page.locator(f'.source-list-row[data-source-id="{repaired["id"]}"]')).to_contain_text("文本修复")
                    page.screenshot(path=str(output / "01b-derived-source-labels.png"), full_page=True)
                    checks.append("mojibake_preview_and_explicit_new_source")

                    if args.libreoffice:
                        html = fixtures / "synthetic-legacy.html"
                        html.write_text('<html><meta charset="utf-8"><body><p>旧版文档合成验收。</p></body></html>', encoding="utf-8")
                        converted = subprocess.run([args.libreoffice, "-env:UserInstallation=" + (output / "lo-profile").as_uri(),
                            "--headless", "--convert-to", "doc:MS Word 97", "--outdir", str(fixtures), str(html)],
                            capture_output=True, text=True, timeout=60)
                        assert converted.returncode == 0, converted.stderr
                        legacy = fixtures / "synthetic-legacy.doc"
                        assert legacy.read_bytes().startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
                        page.reload(wait_until="domcontentloaded")
                        page.locator(".import-options > summary").click()
                        page.get_by_label("DOC 转换器", exact=True).select_option("libreoffice")
                        imported = upload_ui(legacy)
                        assert "旧版文档合成验收" in "\n".join(block["text"] for block in imported["blocks"])
                        checks.append("real_doc_fixture_ui_explicit_libreoffice_import")

                    left = upload_api("synthetic-known-plants.txt", A1 + "\n\n" + A2)
                    right = upload_api("synthetic-variant-plants.txt", A1 + "\n\n" + A2.replace("清晨", "上午"))
                    other = upload_api("synthetic-known-navigation.txt", B3)
                    book = post("/api/books", {"title": "合成植物手册", "edition": "观察版"})
                    second = post("/api/books", {"title": "合成航行记录", "edition": "记录版"})
                    for source, target in ((left, book), (right, book), (other, second)):
                        post("/api/assignments", {"book_id": target["id"], "edition_id": target["edition_id"],
                            "spans": [{"source_id": source["id"], "block_id": block["id"]} for block in source["blocks"]]})
                    mixed = upload_api("synthetic-interleaved-books.txt", A1 + "\n\n" + B3 + "\n\n" + A2)
                    assignments_before = get("/api/state")["assignments"]
                    with page.expect_response(lambda response: response.url.endswith(f"/sources/{mixed['id']}/inspect") and response.request.method == "POST") as automatically_inspected:
                        show_source(mixed["id"])
                    assert automatically_inspected.value.ok
                    automatic_report = automatically_inspected.value.json()
                    assert len(automatic_report["boundaries"]) >= 2
                    expect(page.locator(".source-inspection-alert")).to_contain_text("可能含不同图书")
                    inspection = page.locator(".source-tools").filter(has=page.locator("summary", has_text="混书线索与已知图书匹配"))
                    expect(inspection.locator("summary").first).to_contain_text("处待核查")
                    inspection.locator("summary").first.click()
                    with page.expect_response(lambda response: response.url.endswith("/inspect") and response.request.method == "POST") as inspected:
                        inspection.get_by_role("button", name="检查来源线索", exact=True).click()
                    report = inspected.value.json()
                    assert len(report["boundaries"]) >= 2 and len(report["matches"]) >= 3
                    inspection.get_by_role("button", name="忽略此线索", exact=True).first.click()
                    expect(inspection.get_by_role("button", name="忽略此线索", exact=True).first).to_be_disabled()
                    inspection.get_by_role("button", name="恢复待核查", exact=True).first.click()
                    expect(inspection.get_by_role("button", name="恢复待核查", exact=True)).to_have_count(0)
                    inspection.get_by_label("核查线索 1", exact=True).check()
                    inspection.get_by_role("button", name="记为已核查", exact=True).first.click()
                    with page.expect_response(lambda response: response.url.endswith("/boundary-reviews") and response.request.method == "POST") as created:
                        inspection.get_by_role("button", name="生成边界离线核查请求", exact=True).click()
                    request = created.value.json()
                    with page.expect_download() as download:
                        inspection.get_by_role("button", name="下载边界核查 JSON", exact=True).click()
                    downloaded = output / "boundary-request.json"
                    download.value.save_as(str(downloaded))
                    assert json.loads(downloaded.read_text())["request_id"] == request["id"]
                    show_source(mixed["id"])
                    inspection = page.locator(".source-tools").filter(has=page.locator("summary", has_text="混书线索与已知图书匹配"))
                    inspection.locator("summary").first.click()
                    inspection.get_by_label("已保存的边界核查请求", exact=True).select_option(request["id"])
                    response = {"request_id": request["id"], "opinions": [{"boundary_id": boundary["id"], "verdict": "uncertain", "reason": "合成核查：局部匹配也可能是引用，保留人工判断。"} for boundary in request["request"]["boundaries"]]}
                    inspection.get_by_label("边界核查结果", exact=True).fill(json.dumps(response, ensure_ascii=False))
                    inspection.get_by_role("button", name="校验并保存核查意见", exact=True).click()
                    expect(inspection.get_by_label("边界核查结果", exact=True)).to_have_count(0)
                    saved = get("/api/boundary-reviews/" + request["id"])
                    assert saved["status"] == "completed" and saved["applied"] is False
                    assert get("/api/state")["assignments"] == assignments_before
                    page.screenshot(path=str(output / "02-boundary-review.png"), full_page=True)
                    checks.append("boundary_decisions_and_offline_review_reload_without_auto_assignment")
                    checks.append("automatic_bounded_inspection_alert_visible_when_details_collapsed")

                    page.get_by_role("navigation", name="工作台导航").get_by_role("button", name="整理工作台", exact=True).click()
                    page.get_by_label("目标图书", exact=True).select_option(book["id"])
                    page.get_by_label("目标版本", exact=True).select_option(book["edition_id"])
                    page.get_by_label("左侧来源", exact=True).select_option(left["id"])
                    page.get_by_label("右侧来源", exact=True).select_option(right["id"])
                    expect(page.get_by_label("左侧来源", exact=True).locator(f'option[value="{derived["id"]}"]')).to_contain_text("重新解码")
                    expect(page.get_by_label("右侧来源", exact=True).locator(f'option[value="{repaired["id"]}"]')).to_contain_text("文本修复")
                    checks.append("derived_source_labels_distinguish_list_and_selectors_without_renaming_files")
                    with page.expect_response(lambda response: response.url.endswith("/alignment") and response.request.method == "POST") as aligned:
                        page.get_by_role("button", name="对齐已归属内容", exact=True).click()
                    alignment = aligned.value.json()
                    assert alignment["automatic_merge"] is False and len(alignment["rows"]) >= 2
                    first_row = page.locator(".source-alignment .difference-card").first
                    expect(first_row.locator(".diff-before pre")).to_contain_text(ALIGNMENT_TAIL)
                    assert ALIGNMENT_TAIL not in alignment["rows"][0]["left_preview"]
                    left_panel = page.locator(".comparison-column").first
                    left_panel.get_by_label("选择第 1 块", exact=True).check()
                    selected = []
                    for index, row in enumerate(alignment["rows"][:2]):
                        side = "left" if index == 0 and row["left"] else "right"
                        assert row[side]
                        selected.extend(row[side])
                        page.locator(".source-alignment .difference-card").nth(index).get_by_role("button", name="采用左侧范围" if side == "left" else "采用右侧范围", exact=True).click()
                    expect(page.locator(".tray-item")).to_have_count(len(selected))
                    with page.expect_response(lambda response: response.url.endswith("/assemble") and response.request.method == "POST") as assembled:
                        page.get_by_role("button", name="生成整理稿", exact=True).click()
                    payload = assembled.value.request.post_data_json
                    assert payload["selections"] == selected and payload["edition_id"] == book["edition_id"]
                    assert assembled.value.ok, assembled.value.text()
                    assembled_result = assembled.value.json()
                    revision_id = assembled_result.get("revision_id") or assembled_result.get("revision", {}).get("id") or assembled_result.get("id")
                    assert revision_id, assembled_result
                    revision = get("/api/revisions/" + revision_id)
                    assert [block["source_span"] for block in revision["blocks"]] == selected
                    expect(page.get_by_label("编辑第 1 块", exact=True)).to_be_visible()
                    page.screenshot(path=str(output / "03-aligned-assembly.png"), full_page=True)
                    checks.append("same_edition_alignment_ui_selection_and_provenance_assembly")
                    checks.append("complete_alignment_range_preview_and_whole_block_deduplication")
                    assert not errors, errors
                    receipt["passed"] = True
                except Exception as exc:
                    receipt["error"] = str(exc)
                    page.screenshot(path=str(output / "failure.png"), full_page=True)
                    raise
                finally:
                    context.close()
                    browser.close()
        finally:
            if server.poll() is None:
                server.send_signal(signal.SIGINT if os.name != "nt" else signal.SIGTERM)
                try:
                    server.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    receipt["passed"] = False
                    receipt["forced_server_cleanup"] = True
                    if os.name != "nt":
                        os.killpg(server.pid, signal.SIGKILL)
                    else:
                        server.kill()
                    server.wait(timeout=5)
            receipt.update(server_exit_code=server.returncode, browser_errors=errors, duration_seconds=round(time.monotonic() - started, 2))
            if server.returncode != 0:
                receipt["passed"] = False
            (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(receipt, ensure_ascii=False))
    if not receipt["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
