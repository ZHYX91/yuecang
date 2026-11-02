from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing
import secrets
import tempfile
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from anyio import to_thread
from starlette.formparsers import MultiPartParser, MultiPartException
from yuecang.ai import ReviewService
from yuecang.application.service import Workbench
from yuecang.storage.project import DomainError, uid, linked, digest


def fields(payload, allowed, required=()):
    if set(payload) - set(allowed) or set(required) - set(payload):
        raise DomainError("request_fields", "请求字段不完整或包含不允许的字段")
    return payload


def create_app(project_root, *, start_worker=True):
    wb = Workbench(project_root)
    project, review, sessions = wb.project, ReviewService(wb), {}

    @contextlib.asynccontextmanager
    async def lifespan(app):
        process = stop = None
        if start_worker:
            from yuecang.worker import worker_main
            context = multiprocessing.get_context("spawn")
            stop = context.Event()
            process = context.Process(target=worker_main, args=(str(project.root), stop))
            process.start()
        yield
        if stop:
            stop.set()
            await asyncio.to_thread(process.join, 4)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 3)

    app = FastAPI(title="阅藏", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.workbench = wb

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc), "details": exc.details}}, status_code=exc.status)

    @app.middleware("http")
    async def local_guard(request, call_next):
        host = request.headers.get("host", "")
        if urlsplit("http://" + host).hostname not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"error": {"code": "host", "message": "仅接受本机访问"}}, 403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{host}":
            return JSONResponse({"error": {"code": "origin", "message": "拒绝跨站访问"}}, 403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/session":
            token = sessions.get(request.cookies.get("yuecang-session", ""))
            if not token or request.method not in {"GET", "HEAD"} and not secrets.compare_digest(request.headers.get("X-Yuecang-CSRF", ""), token):
                return JSONResponse({"error": {"code": "session", "message": "请重新打开阅藏页面"}}, 403)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
        return response

    @app.get("/api/session")
    def session(request: Request):
        current = request.cookies.get("yuecang-session")
        if current in sessions:
            token = sessions[current]
        else:
            current, token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            if len(sessions) > 100:
                sessions.pop(next(iter(sessions)))
            sessions[current] = token
        response = JSONResponse({"csrf_token": token})
        response.set_cookie("yuecang-session", current, httponly=True, samesite="strict")
        return response

    @app.get("/api/state")
    def state():
        return wb.state()

    @app.get("/api/events")
    async def events(request: Request):
        async def stream():
            try:
                after = int(request.headers.get("last-event-id", "0"))
            except ValueError:
                after = 0
            while not await request.is_disconnected():
                with project.connect() as db:
                    rows = db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 100", (after,)).fetchall()
                for row in rows:
                    after = row["id"]
                    yield f"id: {after}\ndata: {json.dumps(dict(row), ensure_ascii=False)}\n\n"
                if not rows:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/imports")
    async def imports(request: Request):
        from yuecang.formats.common import MAX_INPUT_BYTES
        from yuecang.jobs import JobQueue
        lengths = request.headers.getlist("content-length")
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or request.headers.get("transfer-encoding"):
            raise DomainError("upload_length", "上传必须提供明确的 Content-Length", 411)
        length = int(lengths[0])
        # Keep multipart spools and the object-store copy covered simultaneously.
        if length <= 0 or 2 * length > project.settings()["storage"]["temporary_bytes"]:
            raise DomainError("space_budget", "上传及暂存副本超过项目临时空间预算", 413)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
            raise DomainError("multipart", "请使用 multipart 文件上传", 415)
        if linked(project.root / "tmp"):
            raise DomainError("linked_directory", "项目临时目录不能是链接")
        reservation, owner = uid(), "upload:" + uid()
        project.reserve(reservation, 2 * length, owner)

        class ProjectMultipartParser(MultiPartParser):
            complete = False

            def on_part_begin(self):
                super().on_part_begin()
                self.part_bytes = 0

            def on_headers_finished(self):
                super().on_headers_finished()
                part = self._current_part
                if part.file is not None:
                    # The base parser has only created an empty in-memory spool.
                    # Replace it before any data arrives; never roll to system tmp.
                    spool = tempfile.TemporaryFile(mode="w+b", dir=project.root / "tmp")
                    self._files_to_close_on_error.append(spool)
                    part.file.file.close()
                    part.file.file = spool

            def on_part_data(self, data, start, end):
                self.part_bytes += end - start
                if self._current_part.file is not None and self.part_bytes > MAX_INPUT_BYTES:
                    raise DomainError("input_size", "单个来源超过格式处理上限", 413)
                super().on_part_data(data, start, end)

            def on_end(self):
                self.complete = True

        async def bounded_body():
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > length:
                    raise DomainError("upload_length", "上传数据超过声明长度", 400)
                for offset in range(0, len(chunk), 1024 * 1024):
                    part = chunk[offset:offset + 1024 * 1024]
                    project.check_reservation(reservation, 2 * length, owner, additional_bytes=len(part))
                    yield part
            if received != length:
                raise DomainError("upload_length", "上传数据长度不完整", 400)

        parser = ProjectMultipartParser(request.headers, bounded_body(), max_files=100, max_fields=10, max_part_size=64 * 1024)
        try:
            try:
                form = await parser.parse()
            except MultiPartException:
                raise DomainError("multipart", "文件上传格式无效或字段过多", 400) from None
            if not parser.complete:
                raise DomainError("multipart", "文件上传未完整结束", 400)
            jobs = []
            ocr = str(form.get("ocr", "false")).lower() == "true"
            for _, value in form.multi_items():
                if hasattr(value, "file"):
                    name = Path(value.filename or "source.txt").name
                    # AnyIO waits for the copy even if the request is cancelled;
                    # the reservation cannot be released under an active writer.
                    sha = await to_thread.run_sync(partial(project.put_stream, value.file, "upload", owner=owner,
                                                          max_bytes=min(length, MAX_INPUT_BYTES), reservation=reservation))
                    job = JobQueue(project).enqueue("import", {"asset_sha": sha, "name": name, "ocr": ocr}, input_key=digest(["import", sha, ocr, name]))
                    jobs.append(job)
            if not jobs:
                raise DomainError("files", "请选择要导入的文件")
            return {"jobs": jobs}
        finally:
            for spool in parser._files_to_close_on_error:
                spool.close()
            project.release_reservation(reservation, owner)

    @app.get("/api/sources/{source_id}")
    def source(source_id: str):
        return wb.source(source_id)

    @app.get("/api/sources/{source_id}/resources/{sha}")
    def source_resource(source_id: str, sha: str):
        import mimetypes
        source = wb.source(source_id)
        resource = next((r for b in source["blocks"] for r in b.get("resources", []) if r.get("sha256") == sha), None)
        if source["asset_sha"] == sha:
            name = source["name"]
        elif resource:
            name = resource.get("name", "resource.bin")
        else:
            raise DomainError("resource_scope", "资源不属于当前来源", 404)
        path = project.object_path(sha)
        if project.hash_file(path) != sha:
            raise DomainError("corrupt_object", "资源校验失败")
        mime = (resource or {}).get("mime") or mimetypes.guess_type(name)[0]
        inline = mime in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
        return FileResponse(path, filename=Path(name).name, media_type=mime if inline else "application/octet-stream", content_disposition_type="inline" if inline else "attachment")

    @app.post("/api/sources/{source_id}/analyze")
    def source_analyze(source_id: str):
        return wb.analyze(source_id=source_id)

    @app.post("/api/books")
    def book(payload: dict):
        return wb.create_book(**fields(payload, {"title", "author", "language", "edition"}, {"title"}))

    @app.post("/api/books/{book_id}/editions")
    def edition(book_id: str, payload: dict):
        return wb.create_edition(book_id, **fields(payload, {"label", "language", "translator"}, {"label"}))

    @app.post("/api/books/{book_id}/analyze")
    def analyze(book_id: str):
        return wb.analyze(book_id=book_id)

    @app.post("/api/assignments")
    def assign(payload: dict):
        return wb.assign(**fields(payload, {"book_id", "edition_id", "spans"}, {"book_id", "spans"}))

    @app.post("/api/dispositions")
    def dispose(payload: dict):
        return wb.dispose(**fields(payload, {"source_id", "block_id", "start", "end", "kind", "reason"}, {"source_id", "block_id", "start", "end", "kind"}))

    @app.post("/api/books/{book_id}/assemble")
    def assemble(book_id: str, payload: dict):
        return wb.assemble(book_id, **fields(payload, {"selections", "edition_id", "title", "allow_composite"}, {"selections"}))

    @app.get("/api/revisions/{revision_id}")
    def revision(revision_id: str):
        return wb.revision(revision_id)

    @app.post("/api/branches/{branch_id}/edits")
    def edit(branch_id: str, payload: dict):
        return wb.edit(branch_id, **fields(payload, {"expected_revision_id", "operations"}, {"expected_revision_id", "operations"}))

    @app.post("/api/branches/{branch_id}/select")
    def select(branch_id: str, payload: dict):
        return wb.select_revision(branch_id, **fields(payload, {"expected_revision_id", "revision_id"}, {"expected_revision_id", "revision_id"}))

    @app.post("/api/revisions/{revision_id}/finalize")
    def finalize(revision_id: str):
        return wb.finalize(revision_id)

    @app.post("/api/revisions/{revision_id}/fork")
    def fork(revision_id: str, payload: dict):
        from yuecang.application.merge import fork_revision
        fields(payload, {"name"}, {"name"})
        return fork_revision(wb, revision_id, payload["name"])

    @app.post("/api/branches/{branch_id}/merge")
    def merge(branch_id: str, payload: dict):
        from yuecang.application.merge import merge_branch
        return merge_branch(wb, branch_id, **fields(payload, {"expected_revision_id", "other_revision_id", "resolutions"}, {"expected_revision_id", "other_revision_id"}))

    @app.post("/api/branches/{branch_id}/metadata")
    def metadata(branch_id: str, payload: dict):
        from yuecang.application.merge import edit_metadata
        fields(payload, {"expected_revision_id", "metadata"}, {"expected_revision_id", "metadata"})
        return edit_metadata(wb, branch_id, payload["expected_revision_id"], payload["metadata"])

    @app.post("/api/revisions/{revision_id}/review")
    def review_create(revision_id: str, payload: dict):
        from yuecang.jobs import JobQueue
        result = review.create(revision_id, **fields(payload, {"provider", "instruction", "branch_id"}))
        if result["provider"] == "http":
            result["jobs"] = [JobQueue(project).enqueue("review", {"request_id": request_id}, input_key="review:" + request_id)
                              for request_id in result["request_ids"]]
            result["job"] = result["jobs"][0]
        return result

    @app.get("/api/review-requests/{request_id}")
    def review_get(request_id: str):
        return review.get(request_id)

    @app.post("/api/review-requests/{request_id}/response")
    def review_receive(request_id: str, payload: dict):
        return review.receive(request_id, payload.get("response", payload))

    @app.post("/api/proposals/{proposal_id}/decide")
    def decide(proposal_id: str, payload: dict):
        return review.decide(proposal_id, payload.get("decision"))

    @app.post("/api/compare")
    def compare(payload: dict):
        return wb.compare(**fields(payload, {"left_source_id", "right_source_id"}, {"left_source_id", "right_source_id"}))

    @app.post("/api/exports")
    def export(payload: dict):
        from yuecang.jobs import JobQueue
        fields(payload, {"revision_id", "format", "allow_draft"}, {"revision_id"})
        return {"job": JobQueue(project).enqueue("export", payload)}

    @app.get("/api/exports/{export_id}/download")
    def download(export_id: str):
        from yuecang.export import verify_export
        row = project.row("exports", export_id)
        base = (project.root / row["path"]).resolve()
        if not base.is_relative_to(project.root / "exports"):
            raise DomainError("export_path", "导出文件路径异常")
        manifest = verify_export(base)
        if manifest["revision_id"] != row["revision_id"]:
            raise DomainError("export_identity", "导出文件与登记修订不一致")
        filename = {"markdown": "book.md", "md": "book.md", "txt": "book.txt", "epub": "book.epub"}.get(row["format"])
        if filename is None:
            raise DomainError("export_format", "未知导出格式")
        path = base / filename
        return FileResponse(path, filename=path.name)

    @app.post("/api/jobs/{job_id}/{action}")
    def job_action(job_id: str, action: str, payload: dict | None = None):
        from yuecang.jobs import JobQueue
        if action not in {"pause", "resume", "cancel", "retry"}:
            raise DomainError("job_action", "未知任务操作")
        queue = JobQueue(project)
        if action == "retry":
            payload = fields(payload or {}, {"acknowledge_external"})
            acknowledged = payload.get("acknowledge_external", False)
            if type(acknowledged) is not bool:
                raise DomainError("job_acknowledgment", "重试确认必须为布尔值")
            return queue.retry(job_id, acknowledge_external=acknowledged)
        fields(payload or {}, set())
        if action == "resume" and queue.get(job_id)["state"] == "failed":
            return queue.retry(job_id)
        return getattr(queue, action)(job_id)

    @app.post("/api/settings")
    def settings(payload: dict):
        return project.save_settings(payload)

    @app.post("/api/maintenance/backup")
    def backup():
        from yuecang.jobs import JobQueue
        return {"job": JobQueue(project).enqueue("backup", {})}

    @app.post("/api/maintenance/cache")
    def cache():
        from yuecang.storage.maintenance import cleanup_cache
        return cleanup_cache(project, apply=True)

    @app.get("/api/{collection}")
    def collection(collection: str, offset: int = 0, limit: int = 100, search: str = ""):
        return wb.list_page(collection, offset=offset, limit=limit, search=search)

    dist = Path(__file__).parent / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="workbench")
    else:
        @app.get("/")
        def missing_frontend():
            return JSONResponse({"error": {"code": "frontend_missing", "message": "尚未构建管理界面，请按开发文档构建"}}, 503)
    return app
