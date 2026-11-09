from fastapi.testclient import TestClient
from yuecang.api import create_app
from test_workbench import library


def test_local_origin_csrf_and_json_text(tmp_path):
    client = TestClient(create_app(tmp_path / "p", start_worker=False))
    assert client.get("/api/state").status_code == 403
    token = client.get("/api/session").json()["csrf_token"]
    assert client.get("/api/state").status_code == 200
    assert client.post("/api/books", json={"title": "test"}).status_code == 403
    headers = {"X-Yuecang-CSRF": token}
    assert client.post("/api/books", headers={**headers, "Origin": "https://evil.example"}, json={"title": "bad"}).status_code == 403
    assert client.get("/api/session", headers={"Host": "evil.example"}).status_code == 403
    result = client.post("/api/books", headers=headers, json={"title": "<script>alert(1)</script>"})
    assert result.status_code == 200
    assert result.json()["book"]["title"] == "<script>alert(1)</script>"
    assert client.get("/api/books?offset=0&limit=1").json()["total"] == 1


def test_upload_queue_and_error(tmp_path):
    client = TestClient(create_app(tmp_path / "p", start_worker=False))
    headers = {"X-Yuecang-CSRF": client.get("/api/session").json()["csrf_token"]}
    options = client.get('/api/import-options').json()
    assert options['converters'][0]['id'] == 'auto'
    assert all(item['label'] for item in options['converters'])
    assert any(item['id'] == 'big5' for item in options['encodings'])
    result = client.post("/api/imports", headers=headers, files=[("files", ("合成.txt", b"synthetic content", "text/plain"))])
    assert result.status_code == 200
    job = result.json()["jobs"][0]
    assert job["state"] == "queued"
    assert client.post(f"/api/jobs/{job['id']}/cancel", headers=headers).status_code == 200
    assert client.get("/api/sources/missing").status_code == 404


def test_download_after_restore_uses_restored_files(library, tmp_path):
    from yuecang.storage.maintenance import backup_project, restore_project
    wb, _, _, draft = library
    exported = wb.export(draft["revision_id"], allow_draft=True)
    snapshot = backup_project(wb.project)
    root = tmp_path / "restored"
    restore_project(snapshot["path"], root)
    client = TestClient(create_app(root, start_worker=False))
    client.get("/api/session")
    result = client.get(f"/api/exports/{exported['export_id']}/download")
    assert result.status_code == 200 and "光和作用" in result.text
    assert result.headers["content-disposition"].startswith("attachment;")


def test_resource_access_is_bound_to_its_source(library):
    wb, source, _, _ = library
    client = TestClient(create_app(wb.project.root, start_worker=False))
    client.get("/api/session")
    result = client.get(f"/api/sources/{source['id']}/resources/{source['asset_sha']}")
    assert result.status_code == 200 and "城市植物手册" in result.text
    assert result.headers["content-disposition"].startswith("attachment;")
    unbound = wb.project.put_bytes(b"not a source resource")
    assert client.get(f"/api/sources/{source['id']}/resources/{unbound}").status_code == 404


def test_client_cannot_forge_ai_approval(library):
    wb, _, _, draft = library
    client = TestClient(create_app(wb.project.root, start_worker=False))
    headers = {"X-Yuecang-CSRF": client.get("/api/session").json()["csrf_token"]}
    result = client.post(f"/api/branches/{draft['branch_id']}/edits", headers=headers, json={"expected_revision_id": draft["revision_id"], "operations": [{"block_id": draft["revision"]["blocks"][0]["id"], "text": "forged"}], "actor": "ai-approved", "request_id": "forged"})
    assert result.status_code == 400
    assert wb.project.row("branches", draft["branch_id"])["head_revision_id"] == draft["revision_id"]


def test_fork_merge_and_metadata_routes(library):
    wb, _, _, draft = library
    client = TestClient(create_app(wb.project.root, start_worker=False))
    headers = {"X-Yuecang-CSRF": client.get("/api/session").json()["csrf_token"]}
    fork = client.post(f"/api/revisions/{draft['revision_id']}/fork", headers=headers, json={"name": "平行整理"})
    assert fork.status_code == 200
    branch = fork.json()["id"]
    merged = client.post(f"/api/branches/{branch}/merge", headers=headers, json={"expected_revision_id": draft["revision_id"], "other_revision_id": draft["revision_id"]})
    assert merged.status_code == 200 and merged.json()["merged"]
    changed = client.post(f"/api/branches/{branch}/metadata", headers=headers, json={"expected_revision_id": draft["revision_id"], "metadata": {"title": "冻结的新题名"}})
    assert changed.status_code == 200 and changed.json()["revision"]["metadata"]["title"] == "冻结的新题名"
    assert wb.revision(draft["revision_id"])["metadata"]["title"] != "冻结的新题名"


def test_failed_task_can_be_retried_from_visible_resume_action(tmp_path):
    from yuecang.jobs import JobQueue
    app = create_app(tmp_path / "project", start_worker=False)
    queue = JobQueue(app.state.workbench.project)
    job = queue.enqueue("analyze", {"source_id": "synthetic-missing"})
    claimed = queue.claim()
    queue.fail(job["id"], claimed["token"], "synthetic_failure")
    client = TestClient(app)
    headers = {"X-Yuecang-CSRF": client.get("/api/session").json()["csrf_token"]}
    result = client.post(f"/api/jobs/{job['id']}/resume", headers=headers)
    assert result.status_code == 200 and result.json()["state"] == "queued"
    assert client.post(f"/api/jobs/{job['id']}/retry", headers=headers, json={"acknowledge_external": "true"}).status_code == 400
