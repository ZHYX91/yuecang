from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from yuecang.jobs import JobQueue, run_isolated_one
from yuecang.storage.project import DomainError, Project


@pytest.fixture
def project(tmp_path):
    return Project(tmp_path / "project")


@pytest.fixture
def clock():
    value = [1000.0]
    return value


def _refs(project, job_id):
    with project.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM object_refs WHERE owner_id=?", (job_id,))]


def test_enqueue_idempotence_key_conflict_and_result_reuse(project):
    queue = JobQueue(project)
    first = queue.enqueue("analyze", {"source_id": "s"}, input_key="input")
    assert queue.enqueue("analyze", {"source_id": "s"}, input_key="input")["id"] == first["id"]
    with pytest.raises(DomainError) as exc:
        queue.enqueue("analyze", {"source_id": "different"}, input_key="input")
    assert exc.value.code == "job_key_conflict"
    done = queue.run_one(lambda job: {"ok": job["payload"]["source_id"]})
    assert done["state"] == "succeeded"
    assert queue.enqueue("analyze", {"source_id": "s"}, input_key="input")["result"] == {"ok": "s"}
    assert queue.claim() is None


def test_concurrent_claim_has_one_winner(project):
    queue = JobQueue(project)
    queued = queue.enqueue("analyze", {})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda n: JobQueue(project).claim(str(n)), range(4)))
    winners = [c for c in claims if c]
    assert len(winners) == 1
    assert winners[0]["id"] == queued["id"]


def test_lease_recovery_fences_old_worker_and_preserves_checkpoint(project, clock):
    queue = JobQueue(project, clock=lambda: clock[0])
    job = queue.enqueue("analyze", {})
    old = queue.claim("first", lease_seconds=5)
    queue.heartbeat(job["id"], old["token"], lease_seconds=5, progress=0.25, checkpoint={"completed_source": "s1"})
    clock[0] += 6
    assert queue.recover_expired() == [job["id"]]
    current = queue.claim("second")
    assert current["token"] != old["token"]
    assert current["attempt"] == 2
    assert current["checkpoint"]["completed_source"] == "s1"
    assert current["progress"] == 0.25
    for method in (lambda: queue.finish(job["id"], old["token"], {}),
                   lambda: queue.heartbeat(job["id"], old["token"]),
                   lambda: queue.fail(job["id"], old["token"])):
        with pytest.raises(DomainError) as exc:
            method()
        assert exc.value.code == "stale_job"
    assert queue.finish(job["id"], current["token"], {"done": True})["state"] == "succeeded"


def test_expired_lease_requires_recovery_even_before_other_claim(project, clock):
    queue = JobQueue(project, clock=lambda: clock[0])
    queue.enqueue("analyze", {})
    job = queue.claim(lease_seconds=1)
    clock[0] += 2
    with pytest.raises(DomainError) as exc:
        queue.heartbeat(job["id"], job["token"])
    assert exc.value.code == "stale_job"


def test_pause_resume_cancel_fence_running_callbacks(project):
    queue = JobQueue(project)
    job = queue.enqueue("analyze", {})

    def callback(claimed):
        queue.pause(claimed["id"])
        return {"should_not_commit": True}

    assert queue.run_one(callback)["state"] == "paused"
    assert queue.claim() is None
    queue.resume(job["id"])
    old = queue.claim()
    assert queue.cancel(job["id"])["state"] == "cancelled"
    with pytest.raises(DomainError):
        queue.finish(job["id"], old["token"], {})
    assert queue.get(job["id"])["result"] is None


def test_failed_import_retains_retry_input_then_releases_on_success(project):
    queue = JobQueue(project)
    sha = project.put_bytes(b"synthetic book", "source")
    payload = {"asset_sha": sha, "name": "book.txt"}
    job = queue.enqueue("import", payload)
    assert {r["owner"] for r in _refs(project, job["id"])} == {"job"}
    claimed = queue.claim()
    queue.fail(job["id"], claimed["token"], "parser_failure")
    assert {r["owner"] for r in _refs(project, job["id"])} == {"job_retry"}
    assert queue.enqueue("import", payload)["state"] == "failed"
    queue.enqueue("import", payload, retry=True)
    assert {r["owner"] for r in _refs(project, job["id"])} == {"job"}
    claimed = queue.claim()
    queue.finish(job["id"], claimed["token"], {"source_id": "s"})
    assert _refs(project, job["id"]) == []


def test_cancel_and_forget_explicitly_release_input_refs(project):
    queue = JobQueue(project)
    sha = project.put_bytes(b"fixture", "source")
    job = queue.enqueue("import", {"asset_sha": sha, "name": "fixture.txt"})
    with pytest.raises(DomainError):
        queue.forget(job["id"])
    claimed = queue.claim()
    queue.fail(job["id"], claimed["token"])
    queue.forget(job["id"])
    assert _refs(project, job["id"]) == []
    queue.retry(job["id"])
    assert _refs(project, job["id"])
    queue.cancel(job["id"])
    assert _refs(project, job["id"]) == []


def test_external_unknown_never_requeues_automatically(project, clock):
    queue = JobQueue(project, clock=lambda: clock[0])
    job = queue.enqueue("analyze", {})
    claimed = queue.claim()
    queue.mark_external_started(job["id"], claimed["token"])
    clock[0] += 31
    queue.recover_expired()
    assert queue.get(job["id"])["state"] == "needs_attention"
    assert queue.claim() is None
    with pytest.raises(DomainError) as exc:
        queue.retry(job["id"])
    assert exc.value.code == "job_external_unknown"
    queue.retry(job["id"], acknowledge_external=True)
    assert queue.get(job["id"])["state"] == "queued"
    assert queue.claim()["checkpoint"]["retry_acknowledged"] is True


def test_finished_external_call_and_local_recovery_limit(project, clock):
    queue = JobQueue(project, clock=lambda: clock[0], max_attempts=2)
    job = queue.enqueue("analyze", {})
    claimed = queue.claim()
    queue.mark_external_started(job["id"], claimed["token"])
    queue.mark_external_finished(job["id"], claimed["token"])
    clock[0] += 31
    queue.recover_expired()
    assert queue.get(job["id"])["state"] == "queued"
    queue.claim()
    clock[0] += 31
    queue.recover_expired()
    assert queue.get(job["id"])["state"] == "failed"
    assert queue.get(job["id"])["error"] == "recovery_attempt_limit"


@pytest.mark.parametrize("request_state,has_response,expected", [("sending", False, "ready"), ("needs_attention", False, "ready"), ("completed", True, "completed"), ("needs_attention", True, "needs_attention")])
def test_acknowledged_review_retry_only_resets_unanswered_request(project, tmp_path, request_state, has_response, expected):
    from yuecang.application.service import Workbench
    from yuecang.ai import ReviewService
    wb = Workbench(project)
    source_file = tmp_path / "review-retry.txt"
    source_file.write_text("合成重试资料。", encoding="utf-8")
    source = wb.source(wb.import_file(source_file)["source_id"])
    book = wb.create_book("合成重试")
    draft = wb.assemble(book["id"], [{"source_id": source["id"], "block_id": source["blocks"][0]["id"]}])
    request = ReviewService(wb).create(draft["revision_id"], provider="http")
    response_sha = project.put_json({"corrections": []}, "ai_response") if has_response else None
    with project.transaction() as db:
        db.execute("UPDATE review_requests SET status=?,response_sha=? WHERE id=?", (request_state, response_sha, request["id"]))
        if response_sha:
            project.ref(db, "review_request", request["id"], response_sha, "response")
    queue = JobQueue(project)
    job = queue.enqueue("review", {"request_id": request["id"]})
    claimed = queue.claim()
    queue.fail(job["id"], claimed["token"], "synthetic_interruption")
    if request_state != "completed":
        with pytest.raises(DomainError) as exc:
            queue.retry(job["id"])
        assert exc.value.code == "job_external_unknown"
        assert project.row("review_requests", request["id"])["status"] == request_state
    retried = queue.retry(job["id"], acknowledge_external=True)
    assert retried["state"] == "queued"
    current = project.row("review_requests", request["id"])
    assert current["status"] == expected
    assert current["response_sha"] == response_sha


def test_callback_exception_is_safe_and_explicit_retry_is_required(project):
    queue = JobQueue(project)
    queue.enqueue("analyze", {})

    def fail(job):
        raise RuntimeError("credential-like secret should never be logged")

    result = queue.run_one(fail)
    assert result["state"] == "failed"
    assert result["error"] == "RuntimeError"
    assert "secret" not in json.dumps(result)
    assert queue.run_one(lambda job: {}) is None
    queue.retry(result["id"])
    assert queue.run_one(lambda job: {"retried": True})["state"] == "succeeded"


def test_isolated_worker_runs_synthetic_import(project):
    sha = project.put_bytes("合成教材\n\n原文。".encode("utf-8"), "source")
    queue = JobQueue(project)
    queued = queue.enqueue("import", {"asset_sha": sha, "name": "fixture.txt"})
    result = run_isolated_one(queue, max_seconds=30)
    assert result["state"] == "succeeded", result["error"]
    assert result["id"] == queued["id"]
    source_id = result["result"]["source_id"]
    assert project.row("sources", source_id)["status"] == "ready"
    assert _refs(project, queued["id"]) == []


def _sleeping_external_tool(project, job):
    project.reserve("sleep-fixture-" + job["token"], 64, job["id"] + ":" + job["token"])
    tool = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"])
    (project.root / "synthetic-processes.json").write_text(json.dumps({"task": os.getpid(), "tool": tool.pid}), encoding="utf-8")
    while True:
        time.sleep(0.1)


def _run_sleeping_worker(root):
    run_isolated_one(JobQueue(root), callback=_sleeping_external_tool)


def _running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        # A reparented zombie has ended and holds no running task or files.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _wait_for_processes(project):
    path = project.root / "synthetic-processes.json"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        time.sleep(0.05)
    raise AssertionError("synthetic external tool did not start")


def _assert_ended(pids):
    deadline = time.monotonic() + 10
    while any(_running(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(_running(p) for p in pids), "owned process survived task closeout"


def test_cancel_ends_actual_external_tool_process_tree(project):
    queue = JobQueue(project)
    job = queue.enqueue("analyze", {})
    results = []
    thread = threading.Thread(target=lambda: results.append(run_isolated_one(queue, callback=_sleeping_external_tool)), daemon=True)
    thread.start()
    try:
        pids = _wait_for_processes(project)
        assert _running(pids["tool"])
        queue.cancel(job["id"])
        thread.join(15)
        assert not thread.is_alive()
        assert results[0]["state"] == "cancelled"
        _assert_ended(pids.values())
        queue.recover_expired()
        with project.connect() as db:
            assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
    finally:
        if queue.get(job["id"])["state"] == "running":
            queue.cancel(job["id"])
        thread.join(15)


def test_killed_coordinator_releases_tools_and_recovers_with_new_token(project):
    queue = JobQueue(project)
    job = queue.enqueue("analyze", {})
    coordinator = multiprocessing.get_context("spawn").Process(target=_run_sleeping_worker, args=(str(project.root),))
    coordinator.start()
    try:
        pids = _wait_for_processes(project)
        old = queue.get(job["id"])
        assert old["state"] == "running"
        coordinator.terminate()
        coordinator.join(10)
        assert not coordinator.is_alive()
        _assert_ended(pids.values())
        with project.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"]))
        assert queue.recover_expired() == [job["id"]]
        with project.connect() as db:
            assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        recovered = queue.claim()
        assert recovered["token"] != old["token"]
        project.reserve("new-attempt-fixture", 1, job["id"] + ":" + recovered["token"])
        queue.release_attempt_reservations(old, old["checkpoint"]["task_process"])
        with project.connect() as db:
            assert db.execute("SELECT owner FROM reservations").fetchone()[0] == job["id"] + ":" + recovered["token"]
        with project.transaction() as db:
            db.execute("DELETE FROM reservations WHERE id='new-attempt-fixture'")
        with pytest.raises(DomainError):
            queue.finish(job["id"], old["token"], {})
        assert queue.finish(job["id"], recovered["token"], {"recovered": True})["state"] == "succeeded"
    finally:
        if coordinator.is_alive():
            coordinator.terminate()
            coordinator.join(10)
