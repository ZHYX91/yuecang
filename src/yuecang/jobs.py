"""SQLite jobs with leases, fenced commits, explicit retries, and isolated workers.

Workers never hold a database transaction while parsing or calling a provider.
The job token must also be checked by the application at each durable mutation.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
from multiprocessing.connection import wait as wait_connections
import os
import signal
import socket
from pathlib import Path
import threading
import time
from collections.abc import Callable

from yuecang.storage.project import DomainError, Project, canonical, digest, uid

ACTIVE = {"queued", "running", "paused"}
TERMINAL = {"succeeded", "failed", "cancelled", "needs_attention"}
KINDS = {"import", "analyze", "export", "review", "backup", "assignment_plan", "curation_plan"}


def _process_stamp(pid):
    host = socket.gethostname()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() in {87, 1168}:
                return None
            raise OSError("process_identity_unavailable")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            code = wintypes.DWORD()
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)) or not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise OSError("process_identity_unavailable")
            born = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return {"host": host, "pid": pid, "birth": str(born), "running": code.value == 259}
        finally:
            kernel.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return {"host": host, "pid": pid, "birth": boot + ":" + fields[19], "running": fields[0] not in {"Z", "X"}}


def _task_has_running_process(task_pid, *, include_other_groups=False):
    id_index = 3 if include_other_groups else 2  # /proc stat session or process group
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[id_index]) == task_pid and fields[0] not in {"Z", "X"}:
                return True
        except FileNotFoundError:
            continue
    return False


def _attempt_stopped(stamp):
    if not isinstance(stamp, dict) or stamp.get("host") != socket.gethostname() or type(stamp.get("pid")) is not int:
        return False
    try:
        current = _process_stamp(stamp["pid"])
        if current and current["birth"] == stamp.get("birth") and current["running"]:
            return False
        # Windows's task Job Object closes with its owning task process. Linux
        # also tracks the DOC supervisor's separate group within this session.
        return os.name == "nt" or not _task_has_running_process(stamp["pid"], include_other_groups=True)
    except (OSError, ValueError, IndexError):
        return False


def _unpack(row):
    result = dict(row)
    for field in ("payload", "checkpoint", "result"):
        if result.get(field) is not None:
            result[field] = json.loads(result[field]) if isinstance(result[field], str) else result[field]
    return result


class JobQueue:
    def __init__(self, project: Project | str, *, clock: Callable[[], float] = time.time, max_attempts=3):
        self.project = project if isinstance(project, Project) else Project(project)
        self.clock = clock
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts")
        self.max_attempts = max_attempts

    def get(self, job_id):
        return _unpack(self.project.row("jobs", job_id))

    def enqueue(self, kind, payload, input_key=None, *, retry=False, object_shas=()):
        if kind not in KINDS or not isinstance(payload, dict):
            raise DomainError("job_kind", "未知任务类型或非法任务参数")
        encoded = canonical(payload)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise DomainError("job_payload_limit", "任务参数过大；正文应保存为内容对象")
        key = input_key or digest([kind, payload])
        if not isinstance(key, str) or not key or len(key) > 256:
            raise DomainError("job_input_key", "任务幂等键非法")
        refs = set(object_shas)
        if kind == "import":
            if not isinstance(payload.get("asset_sha"), str) or not payload.get("name"):
                raise DomainError("job_import", "导入任务必须引用已保存的原件和文件名")
            refs.add(payload["asset_sha"])
        now = self.clock()
        with self.project.transaction() as db:
            old = db.execute("SELECT * FROM jobs WHERE kind=? AND input_key=? ORDER BY created DESC LIMIT 1", (kind, key)).fetchone()
            if old:
                if old["payload"] != encoded:
                    raise DomainError("job_key_conflict", "相同任务键对应不同参数", 409)
                if retry and old["state"] in {"failed", "cancelled", "needs_attention"}:
                    self._retry(db, dict(old), now, acknowledge_external=False)
                return _unpack(self.project.row("jobs", old["id"], db))
            for sha in refs:
                self.project.object_path(sha)
                if not db.execute("SELECT 1 FROM objects WHERE sha=?", (sha,)).fetchone():
                    raise DomainError("job_input_missing", "任务引用的原件不存在", 409)
            job_id = uid()
            db.execute("INSERT INTO jobs(id,kind,payload,state,input_key,created,updated) VALUES(?,?,?,'queued',?,?,?)",
                       (job_id, kind, encoded, key, now, now))
            for sha in refs:
                self.project.ref(db, "job", job_id, sha, "input")
            self.project.event(db, "job.queued", job_id, {"kind": kind})
            return _unpack(self.project.row("jobs", job_id, db))

    @staticmethod
    def _guard(row, token, now):
        if row["state"] != "running" or row["token"] != token or (row["lease_until"] or 0) <= now:
            raise DomainError("stale_job", "任务租约失效，旧执行者不能继续提交", 409)

    def claim(self, worker_id=None, *, lease_seconds=30):
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds")
        now = self.clock()
        with self.project.transaction() as db:
            self._recover_expired(db, now)
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row is None:
                return None
            checkpoint = json.loads(row["checkpoint"])
            checkpoint["worker_id"] = worker_id or f"worker-{os.getpid()}"
            token = uid()
            db.execute("UPDATE jobs SET state='running',token=?,lease_until=?,attempt=attempt+1,checkpoint=?,updated=? WHERE id=? AND state='queued'",
                       (token, now + lease_seconds, canonical(checkpoint), now, row["id"]))
            self.project.event(db, "job.running", row["id"], {"attempt": row["attempt"] + 1})
            return _unpack(self.project.row("jobs", row["id"], db))

    def heartbeat(self, job_id, token, *, lease_seconds=30, progress=None, checkpoint=None):
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds")
        if progress is not None and (isinstance(progress, bool) or not isinstance(progress, (float, int)) or not 0 <= progress <= 1):
            raise DomainError("job_progress", "任务进度必须在0到1之间")
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise DomainError("job_checkpoint", "检查点必须为对象")
        now = self.clock()
        with self.project.transaction() as db:
            row = self.project.row("jobs", job_id, db)
            self._guard(row, token, now)
            saved = json.loads(row["checkpoint"])
            saved.update(checkpoint or {})
            db.execute("UPDATE jobs SET lease_until=?,progress=?,checkpoint=?,updated=? WHERE id=?",
                       (now + lease_seconds, row["progress"] if progress is None else progress, canonical(saved), now, job_id))
            return _unpack(self.project.row("jobs", job_id, db))

    def mark_external_started(self, job_id, token):
        return self.heartbeat(job_id, token, checkpoint={"external_started": True, "external_finished": False})

    def mark_external_finished(self, job_id, token):
        return self.heartbeat(job_id, token, checkpoint={"external_finished": True})

    def _external_unknown(self, db, row):
        # The provider response can be durably committed just before a worker
        # dies. A completed review is safe to replay: run_http returns it locally.
        if row["kind"] == "review":
            request_id = json.loads(row["payload"]).get("request_id")
            request = db.execute("SELECT status FROM review_requests WHERE id=?", (request_id,)).fetchone()
            if request and request[0] == "completed":
                return False
        checkpoint = json.loads(row["checkpoint"])
        if checkpoint.get("external_started") and not checkpoint.get("external_finished"):
            return True
        if row["kind"] == "review":
            request_id = json.loads(row["payload"]).get("request_id")
            request = db.execute("SELECT status FROM review_requests WHERE id=?", (request_id,)).fetchone()
            return bool(request and request[0] in {"sending", "needs_attention"})
        return False

    def _terminal(self, db, row, state, now, *, result=None, error=None):
        # Failed inputs remain reclaimable only after explicit forget/cancel;
        # ordinary GC must honor job_retry refs just like all other owners.
        if state in {"failed", "needs_attention"}:
            db.execute("INSERT OR IGNORE INTO object_refs(owner,owner_id,sha,role) SELECT 'job_retry',owner_id,sha,role FROM object_refs WHERE owner='job' AND owner_id=?", (row["id"],))
        else:
            db.execute("DELETE FROM object_refs WHERE owner='job_retry' AND owner_id=?", (row["id"],))
        db.execute("DELETE FROM object_refs WHERE owner='job' AND owner_id=?", (row["id"],))
        db.execute("UPDATE jobs SET state=?,token=NULL,lease_until=NULL,result=?,error=?,progress=?,updated=? WHERE id=?",
                   (state, canonical(result) if result is not None else None, error, 1 if state == "succeeded" else row["progress"], now, row["id"]))
        self.project.event(db, "job." + state, row["id"], {"error": error} if error else {})

    def finish(self, job_id, token, result):
        now = self.clock()
        with self.project.transaction() as db:
            row = self.project.row("jobs", job_id, db)
            self._guard(row, token, now)
            self._terminal(db, row, "succeeded", now, result=result)
            return _unpack(self.project.row("jobs", job_id, db))

    def fail(self, job_id, token, error="task_failed"):
        # Error codes only: never persist arbitrary provider exception strings.
        error = str(error)[:100]
        if not all(c.isalnum() or c in "_.-" for c in error):
            error = "task_failed"
        now = self.clock()
        with self.project.transaction() as db:
            row = self.project.row("jobs", job_id, db)
            self._guard(row, token, now)
            state = "needs_attention" if self._external_unknown(db, row) else "failed"
            self._terminal(db, row, state, now, error=error)
            return _unpack(self.project.row("jobs", job_id, db))

    def pause(self, job_id):
        now = self.clock()
        with self.project.transaction() as db:
            row = self.project.row("jobs", job_id, db)
            if row["state"] == "paused":
                return _unpack(row)
            if row["state"] not in {"queued", "running"}:
                raise DomainError("job_state", "此状态的任务不能暂停", 409)
            if self._external_unknown(db, row):
                self._terminal(db, row, "needs_attention", now, error="external_outcome_unknown")
            else:
                db.execute("UPDATE jobs SET state='paused',token=NULL,lease_until=NULL,updated=? WHERE id=?", (now, job_id))
                self.project.event(db, "job.paused", job_id)
            return _unpack(self.project.row("jobs", job_id, db))

    def resume(self, job_id):
        with self.project.transaction() as db:
            row = self.project.row("jobs", job_id, db)
            if row["state"] != "paused":
                raise DomainError("job_state", "只能继续已暂停的任务", 409)
            db.execute("UPDATE jobs SET state='queued',updated=? WHERE id=?", (self.clock(), job_id))
            self.project.event(db, "job.resumed", job_id)
            return _unpack(self.project.row("jobs", job_id, db))

    def cancel(self, job_id):
        with self.project.transaction() as db:
            row = self.project.row("jobs", job_id, db)
            if row["state"] == "succeeded":
                raise DomainError("job_state", "任务已完成，取消不会删除其结果", 409)
            if row["state"] == "cancelled":
                return _unpack(row)
            self._terminal(db, row, "cancelled", self.clock(),
                           error="external_outcome_unknown" if self._external_unknown(db, row) else None)
            return _unpack(self.project.row("jobs", job_id, db))

    def _retry(self, db, row, now, *, acknowledge_external):
        if row["state"] not in {"failed", "cancelled", "needs_attention"}:
            raise DomainError("job_state", "只有失败、取消或待核实任务可重试", 409)
        if self._external_unknown(db, row) or row["state"] == "needs_attention" or row["error"] == "external_outcome_unknown":
            if not acknowledge_external:
                raise DomainError("job_external_unknown", "前次外部调用结果未知；须核实服务状态后明确重试", 409)
        payload = json.loads(row["payload"])
        if acknowledge_external is True and row["kind"] == "review":
            # Reset the provider request in the same transaction as the job.
            # A persisted response is never discarded or charged for again.
            changed = db.execute("UPDATE review_requests SET status='ready' WHERE id=? AND status IN ('sending','needs_attention') AND response_sha IS NULL",
                                 (payload.get("request_id"),)).rowcount
            if changed:
                self.project.event(db, "review.retry_authorized", payload["request_id"], {"job_id": row["id"]})
        if row["kind"] == "import":
            sha = payload["asset_sha"]
            if not db.execute("SELECT 1 FROM objects WHERE sha=?", (sha,)).fetchone() or not self.project.object_path(sha).is_file():
                raise DomainError("job_input_missing", "重试所需原件已清理，请重新导入", 409)
            self.project.ref(db, "job", row["id"], sha, "input")
        db.execute("INSERT OR IGNORE INTO object_refs(owner,owner_id,sha,role) SELECT 'job',owner_id,sha,role FROM object_refs WHERE owner='job_retry' AND owner_id=?", (row["id"],))
        db.execute("DELETE FROM object_refs WHERE owner='job_retry' AND owner_id=?", (row["id"],))
        checkpoint = json.loads(row["checkpoint"])
        for field in ("external_started", "external_finished", "worker_id"):
            checkpoint.pop(field, None)
        checkpoint["retry_acknowledged"] = bool(acknowledge_external)
        db.execute("UPDATE jobs SET state='queued',token=NULL,lease_until=NULL,result=NULL,error=NULL,progress=0,checkpoint=?,updated=? WHERE id=?",
                   (canonical(checkpoint), now, row["id"]))
        self.project.event(db, "job.retried", row["id"])

    def retry(self, job_id, *, acknowledge_external=False):
        with self.project.transaction() as db:
            row = self.project.row("jobs", job_id, db)
            self._retry(db, row, self.clock(), acknowledge_external=acknowledge_external)
            return _unpack(self.project.row("jobs", job_id, db))

    def forget(self, job_id):
        """Release retry inputs after the user explicitly discards a terminal job."""
        with self.project.transaction() as db:
            row = self.project.row("jobs", job_id, db)
            if row["state"] not in TERMINAL:
                raise DomainError("job_state", "活动任务不能释放原件", 409)
            db.execute("DELETE FROM object_refs WHERE owner IN ('job','job_retry') AND owner_id=?", (job_id,))
            self.project.event(db, "job.inputs_released", job_id)
        return self.get(job_id)

    def _recover_expired(self, db, now):
        recovered = []
        # A terminal/cancelled task can also outlive a killed coordinator. Only
        # release an exact attempt whose recorded native identity is now gone.
        for reservation in db.execute("SELECT DISTINCT owner FROM reservations").fetchall():
            parts = reservation["owner"].split(":")
            if len(parts) != 2 or any(len(p) != 32 or any(c not in "0123456789abcdef" for c in p) for p in parts):
                continue
            old_job = db.execute("SELECT checkpoint FROM jobs WHERE id=?", (parts[0],)).fetchone()
            if old_job:
                stamp = json.loads(old_job["checkpoint"]).get("attempt_processes", {}).get(parts[1])
                if _attempt_stopped(stamp):
                    db.execute("DELETE FROM reservations WHERE owner=?", (reservation["owner"],))
        for raw in db.execute("SELECT * FROM jobs WHERE state='running' AND coalesce(lease_until,0)<=?", (now,)).fetchall():
            row = dict(raw)
            stamp = json.loads(row["checkpoint"]).get("task_process")
            if stamp and not _attempt_stopped(stamp):
                continue
            if stamp:
                db.execute("DELETE FROM reservations WHERE owner=?", (row["id"] + ":" + row["token"],))
            if self._external_unknown(db, row):
                self._terminal(db, row, "needs_attention", now, error="external_outcome_unknown")
            elif row["attempt"] >= self.max_attempts:
                self._terminal(db, row, "failed", now, error="recovery_attempt_limit")
            else:
                db.execute("UPDATE jobs SET state='queued',token=NULL,lease_until=NULL,error='lease_expired',updated=? WHERE id=?", (now, row["id"]))
                self.project.event(db, "job.recovered", row["id"])
            recovered.append(row["id"])
        return recovered

    def recover_expired(self):
        with self.project.transaction() as db:
            return self._recover_expired(db, self.clock())

    def release_attempt_reservations(self, job, stamp):
        if not _attempt_stopped(stamp):
            return False
        with self.project.transaction() as db:
            db.execute("DELETE FROM reservations WHERE owner=?", (job["id"] + ":" + job["token"],))
            current = self.project.row("jobs", job["id"], db)
            checkpoint = json.loads(current["checkpoint"])
            checkpoint.get("attempt_processes", {}).pop(job["token"], None)
            db.execute("UPDATE jobs SET checkpoint=? WHERE id=?", (canonical(checkpoint), job["id"]))
        return True

    def run_one(self, callback=None, *, worker_id=None, lease_seconds=30):
        """Run a callback(job) with automatic heartbeat; useful for embedding/tests.

        Production worker_main uses a child process so cancellation can stop work.
        """
        job = self.claim(worker_id, lease_seconds=lease_seconds)
        if job is None:
            return None
        stop = threading.Event()

        def beat():
            while not stop.wait(min(5, lease_seconds / 3)):
                try:
                    self.heartbeat(job["id"], job["token"], lease_seconds=lease_seconds)
                except DomainError:
                    return

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            result = callback(job) if callback else dispatch(self.project, job)
            return self.finish(job["id"], job["token"], result)
        except Exception as exc:
            try:
                return self.fail(job["id"], job["token"], exc.code if isinstance(exc, DomainError) else type(exc).__name__)
            except DomainError as stale:
                if stale.code != "stale_job":
                    raise
                return self.get(job["id"])
        finally:
            stop.set()
            thread.join(timeout=1)


def dispatch(project: Project, job):
    from yuecang.application.service import Workbench
    wb = Workbench(project)
    payload = job["payload"]
    if job["kind"] == "import":
        return wb.import_file(project.object_path(payload["asset_sha"]), name=payload["name"], ocr=bool(payload.get("ocr", False)),
                              encoding=payload.get("encoding", "auto"), repair=payload.get("repair", "none"),
                              doc_converter=payload.get("doc_converter", "auto"), derived_from=payload.get("derived_from"),
                              expected_blocks_sha=payload.get("expected_blocks_sha"), job=job)
    if job["kind"] == "analyze":
        return wb.analyze(source_id=payload.get("source_id"), book_id=payload.get("book_id"), job=job)
    if job["kind"] == "assignment_plan":
        from yuecang.application.organization import Organization
        return {"plan_id": Organization(wb).create(payload["source_ids"], job=job)["id"]}
    if job["kind"] == "curation_plan":
        from yuecang.application.curation import Curation
        return {"plan_id": Curation(wb).create(**payload, job=job)["id"]}
    if job["kind"] == "export":
        return wb.export(payload["revision_id"], format=payload.get("format", "markdown"), allow_draft=bool(payload.get("allow_draft", False)), job=job)
    if job["kind"] == "review":
        from yuecang.ai import ReviewService
        return ReviewService(wb).run_http(payload["request_id"], job=job)
    if job["kind"] == "backup":
        from yuecang.storage.maintenance import backup_project
        return backup_project(project, job=job)
    raise DomainError("job_kind", "未知任务类型")


_WINDOWS_TASK_JOB = None
_WINDOWS_CONSOLE_HANDLER = None


def _ignore_windows_console_interrupts():
    import ctypes
    from ctypes import wintypes

    # A Ctrl+Break event can invoke the Win32 default termination handler
    # without raising a Python exception. Only the API receives console input.
    global _WINDOWS_CONSOLE_HANDLER
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    handler = handler_type(lambda event: event in (0, 1))  # CTRL_C_EVENT, CTRL_BREAK_EVENT
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if kernel.SetConsoleCtrlHandler(handler, True):
        _WINDOWS_CONSOLE_HANDLER = handler


def _bind_process_tree():
    """Tie external OCR/parser descendants to this exact task process."""
    if os.name != "nt":
        os.setsid()
        return
    import ctypes
    from ctypes import wintypes
    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle:
        raise DomainError("worker_process_boundary", "无法建立任务进程边界")
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        kernel.CloseHandle(handle)
        raise DomainError("worker_process_boundary", "无法绑定任务及外部工具的生命周期")
    # Non-inheritable handle is closed by the OS when this process exits or is
    # terminated. Any remaining descendants then terminate with this task.
    global _WINDOWS_TASK_JOB
    _WINDOWS_TASK_JOB = handle


def _watch_task_owner(project, job, stop, parent_id):
    """End this task if its coordinator dies or its execution authority expires."""
    parent_handle = kernel = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        parent_handle = kernel.OpenProcess(0x100000, False, parent_id)  # SYNCHRONIZE
    try:
        while not stop.wait(0.4):
            alive = (bool(parent_handle) and kernel.WaitForSingleObject(parent_handle, 0) == 258) if os.name == "nt" else os.getppid() == parent_id
            if alive:
                try:
                    with project.connect() as db:
                        row = project.row("jobs", job["id"], db)
                        JobQueue._guard(row, job["token"], time.time())
                except DomainError:
                    alive = False
            if not alive:
                if os.name != "nt":
                    os.killpg(os.getpid(), signal.SIGKILL)
                # Windows closes this process's non-inherited Job Object handle.
                os._exit(70)
    finally:
        if parent_handle:
            kernel.CloseHandle(parent_handle)


def _child_execute(root, job, sender, parent_id, gate, callback=None):
    stop = threading.Event()
    try:
        if os.name == "nt":
            _ignore_windows_console_interrupts()
        _bind_process_tree()
        project = Project(root)
        threading.Thread(target=_watch_task_owner, args=(project, job, stop, parent_id), daemon=True).start()
        if not gate.wait(15):
            raise DomainError("worker_start_gate", "协调器未登记执行身份，任务未开始")
        result = callback(project, job) if callback else dispatch(project, job)
        sender.send({"ok": True, "result": result})
    except Exception as exc:
        sender.send({"ok": False, "error": exc.code if isinstance(exc, DomainError) else type(exc).__name__})
    finally:
        stop.set()
        sender.close()


def run_isolated_one(queue: JobQueue, *, max_seconds=3600, stop_reader=None, callback=None):
    """Claim one job and execute in a bounded, killable spawned process."""
    job = queue.claim()
    if job is None:
        return None
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    gate = context.Event()
    process = context.Process(target=_child_execute, args=(str(queue.project.root), job, sender, os.getpid(), gate, callback), daemon=True)
    started = time.monotonic()
    process.start()
    sender.close()
    outcome = None
    stamp = None
    try:
        stamp = _process_stamp(process.pid)
        if stamp is None:
            raise DomainError("worker_process_identity", "任务进程未能启动")
        attempts = dict(job["checkpoint"].get("attempt_processes", {}))
        attempts[job["token"]] = stamp
        queue.heartbeat(job["id"], job["token"], checkpoint={"task_process": stamp, "attempt_processes": attempts})
        gate.set()
        while True:
            if receiver.poll(0.5):
                try:
                    outcome = receiver.recv()
                except EOFError:
                    pass
                break
            if not process.is_alive():
                break
            if stop_reader is not None and wait_connections([stop_reader], timeout=0):
                queue.pause(job["id"])
                return queue.get(job["id"])
            if time.monotonic() - started > max_seconds:
                outcome = {"ok": False, "error": "task_timeout"}
                break
            try:
                queue.heartbeat(job["id"], job["token"])
            except DomainError as exc:
                if exc.code != "stale_job":
                    raise
                return queue.get(job["id"])
        if outcome and outcome.get("ok"):
            try:
                return queue.finish(job["id"], job["token"], outcome["result"])
            except DomainError as exc:
                if exc.code != "stale_job":
                    raise
                return queue.get(job["id"])
        try:
            return queue.fail(job["id"], job["token"], (outcome or {}).get("error", "worker_process_lost"))
        except DomainError as exc:
            if exc.code != "stale_job":
                raise
            return queue.get(job["id"])
    finally:
        if os.name != "nt":
            try:
                # The child called setsid before dispatch, so this is its own
                # process group, never the coordinator's or another task's.
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        if os.name != "nt" and stamp:
            try:
                if _task_has_running_process(process.pid):
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # The DOC supervisor watches the task pipe and can outlive the
            # task's group briefly while it drains detached converter children.
            deadline = time.monotonic() + 2
            while _task_has_running_process(process.pid, include_other_groups=True) and time.monotonic() < deadline:
                time.sleep(0.05)
        if not process.is_alive() and stamp:
            queue.release_attempt_reservations(job, stamp)
        receiver.close()


def worker_main(project, stop_reader=None, *, once=False, poll_seconds=1):
    if stop_reader is not None:
        if os.name == "nt":
            _ignore_windows_console_interrupts()
        else:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        queue = JobQueue(project)
        queue.recover_expired()
        # Windows PipeConnection.poll can raise BrokenPipeError on EOF, while
        # connection.wait treats the closed endpoint as ready on both platforms.
        while stop_reader is None or not wait_connections([stop_reader], timeout=0):
            result = run_isolated_one(queue, stop_reader=stop_reader)
            if once:
                return result
            if result is None:
                if stop_reader is not None:
                    if wait_connections([stop_reader], timeout=poll_seconds):
                        break
                else:
                    time.sleep(poll_seconds)
    finally:
        if stop_reader is not None:
            stop_reader.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YueCang local background worker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--once", action="store_true")
    options = parser.parse_args()
    worker_main(options.project, once=options.once)
