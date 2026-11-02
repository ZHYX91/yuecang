import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request, ClientDisconnect

from yuecang.api import create_app
from yuecang.application.service import Workbench
from yuecang.formats import ExtractionResult
from yuecang.formats.common import resource_budget, resource_bytes, temporary_workspace, ResourceBudgetExceeded
from yuecang.jobs import JobQueue
from yuecang.storage.project import Project, DomainError
from yuecang.storage.maintenance import cleanup_cache


def upload_client(tmp_path):
    app = create_app(tmp_path / "project", start_worker=False)
    client = TestClient(app)
    headers = {"X-Yuecang-CSRF": client.get("/api/session").json()["csrf_token"]}
    return app.state.workbench.project, client, headers


def counts(project):
    with project.connect() as db:
        return (db.execute("SELECT count(*) FROM reservations").fetchone()[0],
                db.execute("SELECT count(*) FROM meta WHERE key LIKE 'maintenance_tmp:%'").fetchone()[0])


def multipart(body=b"synthetic text"):
    return b'--boundary\r\nContent-Disposition: form-data; name="files"; filename="book.txt"\r\n\r\n' + body + b'\r\n--boundary--\r\n'


def test_upload_rejects_unknown_or_excess_length_before_creating_spools(tmp_path):
    project, client, headers = upload_client(tmp_path)
    project.save_settings({"storage": {"temporary_bytes": 64}})
    with patch("yuecang.api.tempfile.TemporaryFile") as create:
        result = client.post("/api/imports", headers=headers, files={"files": ("book.txt", b"content")})
        assert result.status_code == 413
        result = client.post("/api/imports", headers={**headers, "Content-Type": "multipart/form-data; boundary=boundary"}, content=iter([multipart()]))
        assert result.status_code == 411
        create.assert_not_called()
    assert counts(project) == (0, 0)


def test_upload_uses_project_spools_holds_peak_claim_and_releases(tmp_path):
    project, client, headers = upload_client(tmp_path)
    import tempfile
    temporary_file, put = tempfile.TemporaryFile, project.put_stream
    files = []

    def spool(*args, **kwargs):
        assert Path(kwargs["dir"]) == project.root / "tmp"
        result = temporary_file(*args, **kwargs)
        files.append(result)
        return result

    def copy(stream, *args, **kwargs):
        assert kwargs["reservation"]
        with project.connect() as db:
            claims = db.execute("SELECT bytes,owner FROM reservations").fetchall()
        assert len(claims) == 1 and claims[0]["bytes"] >= 2 * len(b"synthetic text")
        assert claims[0]["owner"] == kwargs["owner"]
        return put(stream, *args, **kwargs)

    with patch("yuecang.api.tempfile.TemporaryFile", side_effect=spool), patch.object(project, "put_stream", side_effect=copy):
        result = client.post("/api/imports", headers=headers, files={"files": ("book.txt", b"synthetic text")})
    assert result.status_code == 200
    assert files and all(f.closed for f in files)
    assert counts(project) == (0, 0)
    assert list((project.root / "tmp").iterdir()) == []


@pytest.mark.parametrize("delta", [-1, 1])
def test_upload_declared_length_mismatch_does_not_enqueue(tmp_path, delta):
    project, client, headers = upload_client(tmp_path)
    body = multipart()
    result = client.post("/api/imports", content=body, headers={**headers, "Content-Type": "multipart/form-data; boundary=boundary", "Content-Length": str(len(body) + delta)})
    assert result.status_code == 400
    assert counts(project) == (0, 0)
    assert list((project.root / "tmp").iterdir()) == []
    with project.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_truncated_multipart_and_copy_failure_release_claim(tmp_path):
    project, client, headers = upload_client(tmp_path)
    body = multipart()[:-8]
    result = client.post("/api/imports", content=body, headers={**headers, "Content-Type": "multipart/form-data; boundary=boundary"})
    assert result.status_code == 400
    with patch.object(project, "put_stream", side_effect=DomainError("disk_space", "synthetic full disk", 507)):
        result = client.post("/api/imports", headers=headers, files={"files": ("book.txt", b"content")})
    assert result.status_code == 507
    assert counts(project) == (0, 0)
    assert list((project.root / "tmp").iterdir()) == []


def test_disconnected_upload_closes_already_open_spool(tmp_path):
    app = create_app(tmp_path / "project", start_worker=False)
    project = app.state.workbench.project
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/imports")
    messages = iter([{"type": "http.request", "body": multipart()[:-16], "more_body": True}, {"type": "http.disconnect"}])

    async def receive():
        return next(messages)

    request = Request({"type": "http", "method": "POST", "path": "/api/imports", "headers": [(b"content-type", b"multipart/form-data; boundary=boundary"), (b"content-length", b"1000")]}, receive=receive)
    with pytest.raises(ClientDisconnect):
        asyncio.run(endpoint(request))
    assert counts(project) == (0, 0)
    assert list((project.root / "tmp").iterdir()) == []


def test_upload_respects_other_task_claim_and_per_source_limit(tmp_path):
    project, client, headers = upload_client(tmp_path)
    body = multipart()
    project.save_settings({"storage": {"temporary_bytes": 2 * len(body)}})
    project.reserve("other", 1, "other")
    result = client.post("/api/imports", content=body, headers={**headers, "Content-Type": "multipart/form-data; boundary=boundary"})
    assert result.status_code == 507
    project.release_reservation("other", "other")
    with patch("yuecang.formats.common.MAX_INPUT_BYTES", 4):
        result = client.post("/api/imports", content=body, headers={**headers, "Content-Type": "multipart/form-data; boundary=boundary"})
    assert result.status_code == 413
    assert counts(project) == (0, 0)


def test_duplicate_successful_upload_has_no_new_job_refs_and_long_name_works(tmp_path):
    project, client, headers = upload_client(tmp_path)
    files = {"files": ("x" * 250 + ".txt", b"synthetic text")}
    first = client.post("/api/imports", headers=headers, files=files)
    assert first.status_code == 200
    queue = JobQueue(project)
    job = queue.claim()
    queue.finish(job["id"], job["token"], {"synthetic": True})
    second = client.post("/api/imports", headers=headers, files=files)
    assert second.status_code == 200
    assert second.json()["jobs"][0]["id"] == job["id"]
    with project.connect() as db:
        assert db.execute("SELECT count(*) FROM object_refs WHERE owner='job'").fetchone()[0] == 0


def test_cancelled_upload_becomes_collectable_but_originals_remain(tmp_path):
    project, client, headers = upload_client(tmp_path)
    response = client.post("/api/imports", headers=headers, files={"files": ("book.txt", b"cancelled upload")})
    job = response.json()["jobs"][0]
    sha = job["payload"]["asset_sha"]
    JobQueue(project).cancel(job["id"])
    original = project.put_bytes(b"unreferenced but protected original", "source")
    unknown = project.put_bytes(b"unclassified content", "content")
    result = cleanup_cache(project, apply=True, minimum_age_seconds=0)
    assert result["deleted_objects"] == 1
    assert not project.object_path(sha).exists()
    assert project.object_path(original).exists() and project.object_path(unknown).exists()


def import_job(project):
    sha = project.put_bytes(b"synthetic input", "upload")
    queue = JobQueue(project)
    queue.enqueue("import", {"asset_sha": sha, "name": "book.txt"})
    return sha, queue, queue.claim()


def test_extraction_registers_scratch_and_stops_cumulative_resources_before_write(tmp_path):
    wb = Workbench(tmp_path / "project")
    project = wb.project
    sha, queue, job = import_job(project)
    project.save_settings({"storage": {"temporary_bytes": 1000}})

    def extract(path, *, ocr, resource_dir):
        assert counts(project) == (1, 1)
        resource_bytes(b"a" * 600, "first.bin", resource_dir)
        assert project.storage_status()["reserved_bytes"] == path.stat().st_size + 600
        resource_bytes(b"b" * 500, "second.bin", resource_dir)
        pytest.fail("resource budget was bypassed")

    with patch("yuecang.formats.extract", side_effect=extract), pytest.raises(DomainError, match="临时空间"):
        wb.import_file(project.object_path(sha), name="book.txt", job=job)
    assert counts(project) == (0, 0)
    assert list((project.root / "tmp").iterdir()) == []


def test_cancelled_extraction_keeps_registration_when_cleanup_fails(tmp_path):
    wb = Workbench(tmp_path / "project")
    project = wb.project
    sha, queue, job = import_job(project)

    def extract(path, *, ocr, resource_dir):
        queue.cancel(job["id"])
        resource_bytes(b"should never be written", "cancelled.bin", resource_dir)

    with patch("yuecang.formats.extract", side_effect=extract), patch("yuecang.application.service.shutil.rmtree", side_effect=PermissionError("synthetic busy file")), pytest.raises(PermissionError):
        wb.import_file(project.object_path(sha), name="book.txt", job=job)
    assert counts(project) == (0, 1)
    scratch = next((project.root / "tmp").iterdir())
    assert list((scratch / "resources").iterdir()) == []
    cleanup_cache(project, apply=True, minimum_age_seconds=0)
    assert counts(project) == (0, 0)
    assert not scratch.exists()


def test_import_writes_all_use_attempt_owner_and_forget_completed_temps(tmp_path):
    wb = Workbench(tmp_path / "project")
    project = wb.project
    sha, queue, job = import_job(project)
    owners, reserve = [], project.reserve

    def record_claim(identifier, size, owner):
        owners.append(owner)
        return reserve(identifier, size, owner)

    def extract(path, *, ocr, resource_dir):
        resource = resource_bytes(b"synthetic resource", "image.bin", resource_dir)
        result = ExtractionResult("Synthetic")
        result.add("paragraph", "synthetic text", {}, resources=[resource])
        return result

    with patch.object(project, "reserve", side_effect=record_claim), patch("yuecang.formats.extract", side_effect=extract):
        wb.import_file(project.object_path(sha), name="book.txt", job=job)
    assert owners and set(owners) == {job["id"] + ":" + job["token"]}
    assert counts(project) == (0, 0)
    assert list((project.root / "tmp").iterdir()) == []


def test_failed_object_partial_keeps_cleanup_registration_but_releases_claim(tmp_path):
    project = Project(tmp_path / "project")
    sha, queue, job = import_job(project)

    class InterruptedStream:
        calls = 0
        def read(self, _size):
            self.calls += 1
            if self.calls == 1:
                return b"partial synthetic resource"
            raise ValueError("synthetic interrupted read")

    unlink = Path.unlink
    def busy(path, *args, **kwargs):
        if path.suffix == ".partial":
            raise PermissionError("synthetic busy temp")
        return unlink(path, *args, **kwargs)

    with patch.object(Path, "unlink", busy), pytest.raises(PermissionError):
        project.put_stream(InterruptedStream(), "resource", owner=job["id"] + ":" + job["token"])
    assert counts(project) == (0, 1)
    queue.cancel(job["id"])
    cleanup_cache(project, apply=True, minimum_age_seconds=0)
    assert counts(project) == (0, 0)
    assert list((project.root / "tmp").iterdir()) == []


def test_resource_total_deduplicates_and_temporary_claim_is_released(tmp_path):
    seen = []
    with resource_budget(tmp_path, reserve=seen.append, initial_bytes=2, maximum=5):
        resource_bytes(b"abc", "first.bin", tmp_path)
        resource_bytes(b"abc", "same.bin", tmp_path)
        with temporary_workspace(tmp_path, maximum=4, prefix="page-") as directory:
            assert directory.parent == tmp_path and seen[-1] == 9
        assert seen[-1] == 5
        with pytest.raises(ResourceBudgetExceeded):
            resource_bytes(b"def", "second.bin", tmp_path)
    assert len(list(tmp_path.iterdir())) == 1
    assert seen == [2, 5, 9, 5]


def test_png_and_ocr_stdout_limits_stop_before_excess_disk_write(tmp_path):
    from PIL import Image
    from yuecang.formats import pdf
    with Image.new("RGB", (20, 20)) as image, pytest.raises(ResourceBudgetExceeded):
        pdf._write_png(image, tmp_path / "page.png", 16)
    assert (tmp_path / "page.png").stat().st_size <= 16

    class Process:
        stdout = io.BytesIO(b"x" * 128)
        killed = False
        def poll(self):
            return 0 if self.killed else None
        def kill(self):
            self.killed = True
        def wait(self, **kwargs):
            self.killed = True
            return 0

    process = Process()
    with patch.object(pdf.subprocess, "Popen", return_value=process), patch.object(pdf, "MAX_OCR_BYTES", 16), pytest.raises(ResourceBudgetExceeded):
        pdf._ocr_to_file("tesseract", tmp_path / "page.png", tmp_path / "result.tsv", "eng")
    assert process.killed and process.stdout.closed
    assert (tmp_path / "result.tsv").stat().st_size <= 16


def test_shared_disk_claims_and_released_claim_cannot_be_reused(tmp_path):
    project = Project(tmp_path / "project")
    project.save_settings({"storage": {"minimum_free_bytes": 0, "temporary_bytes": 100}})
    with patch("yuecang.storage.project.shutil.disk_usage", return_value=SimpleNamespace(free=10)):
        project.reserve("first", 6, "one")
        with pytest.raises(DomainError) as raised:
            project.reserve("second", 5, "two")
        assert raised.value.code == "disk_space"
        with pytest.raises(DomainError):
            project.reserve("first", 1, "not-owner")
        project.release_reservation("first", "one")
        with pytest.raises(DomainError):
            project.put_stream(io.BytesIO(b"x"), reservation="first", owner="one", max_bytes=1)
    assert counts(project) == (0, 0)


def test_real_ocr_uses_local_bounded_stdout_and_removes_page_temps(tmp_path, monkeypatch):
    import shutil
    from PIL import Image, ImageDraw, ImageFont
    from yuecang.formats import extract
    if not shutil.which("tesseract"):
        pytest.skip("optional Tesseract tool unavailable")
    monkeypatch.setenv("YUECANG_OCR_LANG", "eng")
    source, resources = tmp_path / "synthetic.png", tmp_path / "resources"
    with Image.new("RGB", (500, 100), "white") as image:
        ImageDraw.Draw(image).text((20, 25), "SYNTHETIC TEST 123", fill="black", font=ImageFont.load_default(size=32))
        image.save(source)
    result = extract(source, ocr=True, resource_dir=resources)
    assert any("SYNTHETIC" in block["text"] for block in result.blocks)
    assert not any(path.is_dir() for path in resources.iterdir())
    assert all(path.suffix == ".png" for path in resources.iterdir())


def test_provider_key_links_are_rejected_and_cache_limit_not_advertised(tmp_path):
    project = Project(tmp_path / "project")
    assert "cache_bytes" not in project.settings()["storage"]
    with pytest.raises(DomainError):
        project.save_settings({"storage": {"cache_bytes": 123}})
    outside = tmp_path / "synthetic-key"
    outside.write_text("unchanged")
    try:
        (project.root / "provider-key").symlink_to(outside)
    except OSError:
        pytest.skip("symlink unavailable on this host")
    with pytest.raises(DomainError):
        project.settings(private=True)
    with pytest.raises(DomainError):
        project.save_settings({"ai": {"api_key": "must not replace"}})
    assert outside.read_text() == "unchanged"
