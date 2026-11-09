"""Synthetic integration checks for recovery and retained source-review evidence."""
import hashlib
import json
from pathlib import Path
import stat

from fastapi.testclient import TestClient
import pytest

from yuecang.api import create_app
from yuecang.application.service import Workbench
from yuecang.application.source_analysis import SourceAnalysis
from yuecang.formats.common import ExtractionResult
from yuecang.jobs import JobQueue
from yuecang.storage import maintenance
from yuecang.storage.project import DomainError


def client_for(root):
    app = create_app(root, start_worker=False)
    client = TestClient(app)
    headers = {"X-Yuecang-CSRF": client.get("/api/session").json()["csrf_token"]}
    return app.state.workbench, client, headers


def test_api_zero_block_retry_reuses_active_and_successful_jobs(tmp_path, monkeypatch):
    import yuecang.formats
    original = yuecang.formats.extract

    def unavailable(*args, **kwargs):
        result = ExtractionResult("synthetic", parser="unavailable")
        result.issue("dependency_unavailable", "合成暂不可用解析器", severity="error")
        return result

    monkeypatch.setattr(yuecang.formats, "extract", unavailable)
    wb, client, headers = client_for(tmp_path / "project")
    queue = JobQueue(wb.project)

    def upload():
        response = client.post("/api/imports", headers=headers, files=[("files", ("fixture.txt", "合成导入正文，验证重复任务不会并行重建。".encode(), "text/plain"))])
        assert response.status_code == 200, response.text
        return response.json()["jobs"][0]

    first = upload()
    failed_extraction = queue.run_one()
    assert failed_extraction["state"] == "succeeded"
    first_source = failed_extraction["result"]["source_id"]
    assert wb.source(first_source)["block_count"] == 0
    second = upload()
    assert second["id"] != first["id"] and upload()["id"] == second["id"]
    assert queue.run_one()["state"] == "succeeded"  # Still unavailable; a new generation is explicit.
    third = upload()
    assert third["id"] != second["id"] and upload()["id"] == third["id"]
    monkeypatch.setattr(yuecang.formats, "extract", original)
    completed = queue.run_one()
    assert completed["state"] == "succeeded"
    assert wb.source(completed["result"]["source_id"])["block_count"] > 0
    assert upload()["id"] == third["id"] and queue.run_one() is None
    assert wb.source(first_source)["block_count"] == 0
    with wb.project.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3


def test_recovery_preview_apply_coordinates_immutable_original_and_stale_guard(tmp_path):
    wb, client, headers = client_for(tmp_path / "project")
    mojibake = "合成开头 " + "你好世界".encode().decode("latin-1") + " 😀 结束。\n"
    raw = mojibake.encode()
    path = tmp_path / "fixture.txt"
    path.write_bytes(raw)
    source = wb.source(wb.import_file(path)["source_id"])
    source_text = "".join(b["text"] for b in source["blocks"])
    response = client.post(f"/api/sources/{source['id']}/text-preview", headers=headers,
                           json={"encoding": "utf-8", "repair": "utf8-as-latin1"})
    assert response.status_code == 200
    preview = response.json()
    assert preview["changed"] and "你好世界" in preview["preview"]
    payload = {"encoding": preview["encoding"], "repair": preview["repair"], "expected_blocks_sha": preview["expected_blocks_sha"]}
    stale = client.post(f"/api/sources/{source['id']}/text-apply", headers=headers, json={**payload, "expected_blocks_sha": "0" * 64})
    assert stale.status_code == 409
    applied = client.post(f"/api/sources/{source['id']}/text-apply", headers=headers, json=payload)
    again = client.post(f"/api/sources/{source['id']}/text-apply", headers=headers, json=payload)
    assert applied.status_code == again.status_code == 200
    assert applied.json()["job"]["id"] == again.json()["job"]["id"]
    completed = JobQueue(wb.project).run_one()
    assert completed["state"] == "succeeded", completed
    derived = wb.source(completed["result"]["source_id"])
    fixed = "".join(b["text"] for b in derived["blocks"])
    assert hashlib.sha256(fixed.encode()).hexdigest() == preview["text_sha256"]
    assert fixed == preview["preview"] and derived["id"] != source["id"]
    assert derived["asset_sha"] == source["asset_sha"] and wb.project.object_path(source["asset_sha"]).read_bytes() == raw
    assert "".join(b["text"] for b in wb.source(source["id"])["blocks"]) == source_text
    for block in derived["blocks"]:
        locator = block["locator"]
        assert fixed[locator["start"]:locator["end"]] == block["text"]
    assert client.post(f"/api/sources/{source['id']}/text-apply", headers=headers, json=payload).json()["job"]["state"] == "succeeded"


def create_completed_review(wb, tmp_path):
    path = tmp_path / "mixed.txt"
    path.write_text("开头内容。\n\n《合成题名》\n\n这是用于提醒核查的合成材料，并无真实书籍内容。", encoding="utf-8")
    source = wb.source(wb.import_file(path)["source_id"])
    service = SourceAnalysis(wb)
    request = service.create_review(source["id"])
    response = {"request_id": request["id"], "opinions": [{"boundary_id": b["id"], "verdict": "uncertain", "reason": "题名可能是引用，需要人工核查。"} for b in request["request"]["boundaries"]]}
    completed = service.receive_review(request["id"], response)
    service.set_review_state(source["id"], request["request"]["boundaries"][0]["id"], "reviewed")
    return source, completed


def test_source_review_survives_verify_cache_backup_restore(tmp_path):
    wb = Workbench(tmp_path / "project")
    source, completed = create_completed_review(wb, tmp_path)
    assert maintenance.verify_project(wb.project)["ok"]
    maintenance.cleanup_cache(wb.project, apply=True)
    assert SourceAnalysis(wb).get_review(completed["id"]) == completed
    snapshot = maintenance.backup_project(wb.project)
    restored = tmp_path / "restored"
    assert maintenance.restore_project(snapshot["path"], restored)["ok"]
    other = Workbench(restored)
    assert maintenance.verify_project(other.project)["ok"]
    assert SourceAnalysis(other).get_review(completed["id"]) == completed
    assert SourceAnalysis(other).inspect(source["id"])["boundaries"][0]["state"] == "reviewed"
    assert other.state()["assignments"] == []


@pytest.mark.parametrize("role", ["request", "report", "response"])
def test_missing_source_review_ref_blocks_verify_and_backup(tmp_path, role):
    wb = Workbench(tmp_path / "project")
    _, completed = create_completed_review(wb, tmp_path)
    with wb.project.transaction() as db:
        sha = db.execute("SELECT sha FROM object_refs WHERE owner='source_review' AND owner_id=? AND role=?",
                         (completed["id"], role)).fetchone()[0]
        db.execute("DELETE FROM object_refs WHERE owner='source_review' AND owner_id=? AND role=?", (completed["id"], role))
        wb.project.ref(db, "source_review", "another-request", sha, role)
        wb.project.ref(db, "source_review", completed["id"], sha, "another-role")
    for action in [lambda: maintenance.verify_project(wb.project), lambda: maintenance.backup_project(wb.project)]:
        with pytest.raises(DomainError) as exc:
            action()
        assert exc.value.code == "object_reference_missing"


def test_review_result_checks_frozen_source_inside_write_transaction(tmp_path, monkeypatch):
    wb = Workbench(tmp_path / "project")
    source, completed = create_completed_review(wb, tmp_path)
    service = SourceAnalysis(wb)
    review = service.create_review(source["id"])
    response = {"request_id": review["id"], "opinions": completed["opinions"]}
    put_json = wb.project.put_json

    def store_then_change_source(value, kind, **kwargs):
        sha = put_json(value, kind, **kwargs)
        assert kind == "source_review_response"
        with wb.project.transaction() as db:
            db.execute("UPDATE sources SET blocks_sha=NULL WHERE id=?", (source["id"],))
        return sha

    monkeypatch.setattr(wb.project, "put_json", store_then_change_source)
    with pytest.raises(DomainError) as exc:
        service.receive_review(review["id"], response)
    assert exc.value.code == "source_review_stale"
    assert service.get_review(review["id"])["status"] == "ready"


def test_cancelled_converter_read_only_copy_can_be_cleaned(tmp_path):
    wb = Workbench(tmp_path / "project")
    queue = JobQueue(wb.project)
    sha = wb.project.put_bytes(b"synthetic input", "source")
    job = queue.enqueue("import", {"asset_sha": sha, "name": "fixture.doc"})
    scratch = wb.project.root / "tmp" / "extract-synthetic"
    work_copy = scratch / "converter" / "source.doc"
    work_copy.parent.mkdir(parents=True)
    work_copy.write_bytes(b"synthetic input")
    work_copy.chmod(stat.S_IREAD)
    maintenance.register_temporary(wb.project, scratch, job["id"])
    queue.cancel(job["id"])

    result = maintenance.cleanup_cache(wb.project, apply=True, minimum_age_seconds=0)
    assert result["deleted_temporary"] == 1 and not scratch.exists()
    assert wb.project.object_path(sha).read_bytes() == b"synthetic input"
