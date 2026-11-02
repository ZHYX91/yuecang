from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path


SCHEMA_VERSION = 1


def uid() -> str:
    return uuid.uuid4().hex


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256((value if isinstance(value, bytes) else canonical(value).encode("utf-8"))).hexdigest()


class DomainError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def linked(path):
    return path.is_symlink() or path.exists() and bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects(sha TEXT PRIMARY KEY,size INTEGER NOT NULL,kind TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS object_refs(owner TEXT NOT NULL,owner_id TEXT NOT NULL,sha TEXT NOT NULL REFERENCES objects(sha),role TEXT NOT NULL,PRIMARY KEY(owner,owner_id,sha,role));
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,asset_sha TEXT NOT NULL REFERENCES objects(sha),name TEXT NOT NULL,status TEXT NOT NULL,blocks_sha TEXT REFERENCES objects(sha),block_count INTEGER NOT NULL DEFAULT 0,metadata TEXT NOT NULL DEFAULT '{}',issues TEXT NOT NULL DEFAULT '[]',parser TEXT NOT NULL DEFAULT '',created REAL NOT NULL,UNIQUE(asset_sha,parser));
CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),name TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fingerprints(source_id TEXT NOT NULL REFERENCES sources(id),token TEXT NOT NULL,PRIMARY KEY(source_id,token));
CREATE INDEX IF NOT EXISTS fingerprints_token ON fingerprints(token);
CREATE TABLE IF NOT EXISTS books(id TEXT PRIMARY KEY,title TEXT NOT NULL,author TEXT NOT NULL,language TEXT NOT NULL DEFAULT 'zh',default_revision_id TEXT,version INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS editions(id TEXT PRIMARY KEY,book_id TEXT NOT NULL REFERENCES books(id),label TEXT NOT NULL,language TEXT NOT NULL,translator TEXT NOT NULL DEFAULT '',metadata TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),block_id TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,book_id TEXT NOT NULL REFERENCES books(id),edition_id TEXT NOT NULL REFERENCES editions(id),created REAL NOT NULL,UNIQUE(source_id,block_id,start,end,book_id,edition_id));
CREATE TABLE IF NOT EXISTS dispositions(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),block_id TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY,book_id TEXT NOT NULL REFERENCES books(id),edition_id TEXT NOT NULL REFERENCES editions(id),parent_id TEXT REFERENCES revisions(id),document_sha TEXT NOT NULL REFERENCES objects(sha),metadata TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',issues TEXT NOT NULL DEFAULT '[]',created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS branches(id TEXT PRIMARY KEY,book_id TEXT NOT NULL REFERENCES books(id),name TEXT NOT NULL,head_revision_id TEXT REFERENCES revisions(id),version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,input_revision TEXT REFERENCES revisions(id),output_revision TEXT NOT NULL REFERENCES revisions(id),actor TEXT NOT NULL,request_id TEXT,payload TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_requests(id TEXT PRIMARY KEY,revision_id TEXT NOT NULL REFERENCES revisions(id),branch_id TEXT NOT NULL REFERENCES branches(id),payload_sha TEXT NOT NULL REFERENCES objects(sha),response_sha TEXT REFERENCES objects(sha),status TEXT NOT NULL,provider TEXT NOT NULL,created REAL NOT NULL,UNIQUE(revision_id,branch_id,payload_sha,provider));
CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES review_requests(id),revision_id TEXT NOT NULL REFERENCES revisions(id),branch_id TEXT NOT NULL REFERENCES branches(id),operations TEXT NOT NULL,reason TEXT NOT NULL,risk TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',progress REAL NOT NULL DEFAULT 0,attempt INTEGER NOT NULL DEFAULT 0,token TEXT,lease_until REAL,checkpoint TEXT NOT NULL DEFAULT '{}',result TEXT,error TEXT,input_key TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state,created);
CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY,revision_id TEXT NOT NULL REFERENCES revisions(id),format TEXT NOT NULL,path TEXT NOT NULL,report TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),other_source_id TEXT,kind TEXT NOT NULL,evidence TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,kind TEXT NOT NULL,bytes INTEGER NOT NULL CHECK(bytes>=0),owner TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,object_id TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
"""


class Project:
    def __init__(self, root: str | Path):
        raw = Path(root).expanduser().absolute()
        for p in (raw, *raw.parents):
            if p.exists() and (p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 0x400):
                raise DomainError("linked_project", "项目路径不能经过链接或重解析点")
        self.root = raw.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = self.root / "project.json"
        self.db_path = self.root / "library.sqlite3"
        if linked(self.db_path) or linked(manifest):
            raise DomainError("linked_database", "数据库路径不能是链接")
        if self.db_path.exists():
            if not manifest.is_file():
                raise DomainError("unknown_database", "目录已有未识别的数据库；请选择新的书库目录")
            marker = json.loads(manifest.read_text(encoding="utf-8"))
            if marker.get("kind") != "yuecang-project" or marker.get("schema_version") != SCHEMA_VERSION:
                raise DomainError("schema_version", "不支持此项目格式或版本")
            with contextlib.closing(sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)) as check:
                try:
                    version = check.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                    identity = check.execute("SELECT value FROM meta WHERE key='project_id'").fetchone()
                except sqlite3.DatabaseError:
                    raise DomainError("unknown_database", "数据库不是可识别的阅藏书库") from None
                if not version or int(version[0]) != SCHEMA_VERSION or not identity or marker.get("id") != identity[0]:
                    raise DomainError("schema_version", "数据库与项目标记不一致")
        for name in ("objects", "tmp", "exports", "backups", "logs"):
            target = self.root / name
            if linked(target):
                raise DomainError("linked_directory", "项目子目录不能是链接")
            target.mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row and int(row[0]) != SCHEMA_VERSION:
                raise DomainError("schema_version", "不支持此项目格式或版本")
            db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            db.execute("INSERT OR IGNORE INTO meta VALUES('project_id',?)", (uid(),))
            db.execute("INSERT OR IGNORE INTO meta VALUES('settings',?)", (canonical({"ai": {"endpoint": "", "model": "", "token_budget": 20000, "max_output_tokens": 3000}, "storage": {"temporary_bytes": 2 * 1024**3, "minimum_free_bytes": 256 * 1024**2}}),))
        manifest = self.root / "project.json"
        if not manifest.exists():
            manifest.write_text(canonical({"kind": "yuecang-project", "schema_version": SCHEMA_VERSION, "id": self.meta("project_id")}), encoding="utf-8")

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=5, isolation_level="DEFERRED", factory=Connection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @contextlib.contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def meta(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def settings(self, private=False):
        value = json.loads(self.meta("settings"))
        value["ai"]["configured"] = bool(value["ai"].get("endpoint") and value["ai"].get("model"))
        if private:
            keyfile = self.root / "provider-key"
            if linked(keyfile):
                raise DomainError("linked_key", "模型凭据文件不能是链接")
            value["ai"]["api_key"] = keyfile.read_text(encoding="utf-8") if keyfile.exists() else ""
        return value

    def save_settings(self, payload):
        current = self.settings()
        for section in ("ai", "storage"):
            if section in payload:
                if not isinstance(payload[section], dict):
                    raise DomainError("settings", "设置项必须为对象")
                if section == "storage" and set(payload[section]) - {"temporary_bytes", "minimum_free_bytes"}:
                    raise DomainError("settings", "未知存储预算设置")
                current[section].update(payload[section])
        ai = current["ai"]
        ai.pop("configured", None)
        key = ai.pop("api_key", None)
        if ai.get("endpoint") and not ai["endpoint"].startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
            raise DomainError("endpoint", "模型服务应使用 HTTPS 或本机 HTTP 地址")
        for field in ("token_budget", "max_output_tokens"):
            if not isinstance(ai.get(field), int) or ai[field] < 1:
                raise DomainError("budget", "模型预算必须为正整数")
        for v in current["storage"].values():
            if not isinstance(v, int) or v < 0:
                raise DomainError("budget", "空间预算必须为非负整数")
        if key is not None:
            target = self.root / "provider-key"
            if linked(target):
                raise DomainError("linked_key", "模型凭据文件不能是链接")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if os.name != "nt":
                    os.fchmod(f.fileno(), 0o600)
                f.write(str(key))
        with self.transaction() as db:
            db.execute("UPDATE meta SET value=? WHERE key='settings'", (canonical(current),))
        return self.settings()

    def object_path(self, sha: str) -> Path:
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise DomainError("object_hash", "非法内容对象标识")
        path = self.root / "objects" / sha[:2] / sha
        if linked(path) or linked(path.parent):
            raise DomainError("linked_object", "内容对象不能是链接")
        return path

    def put_stream(self, stream, kind="content", *, owner="write", max_bytes=None, reservation=None):
        limit = self.settings()["storage"]["temporary_bytes"] if max_bytes is None else max_bytes
        own_reservation = reservation is None
        reservation = reservation or uid()
        if own_reservation:
            self.reserve(reservation, 0, owner)
        else:
            self.check_reservation(reservation, limit, owner)
        temp = self.root / "tmp" / (uid() + ".partial")
        h, size = hashlib.sha256(), 0
        temporary_token = None
        try:
            with temp.open("xb") as out:
                owner_parts = owner.split(":")
                if len(owner_parts) == 2 and all(len(part) == 32 and all(c in "0123456789abcdef" for c in part) for part in owner_parts):
                    from .maintenance import register_temporary
                    temporary_token = register_temporary(self, temp, owner_parts[0])
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise DomainError("space_budget", "文件超过本项目临时空间预算，可在设置中调整", 413)
                    if own_reservation:
                        self.reserve(reservation, size, owner)
                    else:
                        self.check_reservation(reservation, size, owner, additional_bytes=len(chunk))
                    out.write(chunk)
                    h.update(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha = h.hexdigest()
            target = self.object_path(sha)
            target.parent.mkdir(exist_ok=True)
            try:
                os.link(temp, target)
            except FileExistsError:
                if target.stat().st_size != size or self.hash_file(target) != sha:
                    raise DomainError("corrupt_object", "已有内容对象校验失败")
            except OSError:
                # Both paths are in the same validated project: rename is atomic.
                if target.exists() and self.hash_file(target) != sha:
                    raise DomainError("corrupt_object", "已有内容对象校验失败")
                os.replace(temp, target)
            with self.transaction() as db:
                db.execute("INSERT OR IGNORE INTO objects VALUES(?,?,?,?)", (sha, size, kind, time.time()))
            return sha
        finally:
            try:
                temp.unlink(missing_ok=True)
                if temporary_token:
                    from .maintenance import forget_temporary
                    forget_temporary(self, temporary_token)
            finally:
                if own_reservation:
                    self.release_reservation(reservation, owner)

    def put_bytes(self, payload: bytes, kind="content", *, owner="write"):
        import io
        return self.put_stream(io.BytesIO(payload), kind, owner=owner)

    def put_json(self, value, kind="json", *, owner="write"):
        return self.put_bytes(canonical(value).encode("utf-8"), kind, owner=owner)

    def read_json(self, sha):
        p = self.object_path(sha)
        raw = p.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise DomainError("corrupt_object", "内容对象哈希不匹配")
        return json.loads(raw)

    @staticmethod
    def hash_file(path):
        h = hashlib.sha256()
        with Path(path).open("rb") as f:
            for part in iter(lambda: f.read(1024 * 1024), b""):
                h.update(part)
        return h.hexdigest()

    def reserve(self, reservation, size, owner):
        if type(size) is not int or size < 0:
            raise DomainError("reservation", "空间预留必须为非负整数字节")
        settings = self.settings()["storage"]
        with self.transaction() as db:
            current = db.execute("SELECT bytes,owner FROM reservations WHERE id=?", (reservation,)).fetchone()
            if current and current["owner"] != owner:
                raise DomainError("reservation_owner", "空间预留不属于当前任务", 409)
            total = db.execute("SELECT coalesce(sum(bytes),0) FROM reservations WHERE id<>?", (reservation,)).fetchone()[0]
            if total + size > settings["temporary_bytes"]:
                raise DomainError("space_budget", "并发任务已达到项目临时空间上限", 507)
            # Conservative: all claims, including already materialized bytes, must
            # fit the remaining volume. Check while serialized with other claims.
            if (current is None or size > current["bytes"]) and shutil.disk_usage(self.root).free < settings["minimum_free_bytes"] + total + size:
                raise DomainError("disk_space", "磁盘可用空间不足，任务已停止新增写入", 507)
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET bytes=excluded.bytes", (reservation, "temporary", size, owner, time.time()))

    def check_reservation(self, reservation, size, owner, *, additional_bytes=0):
        with self.connect() as db:
            row = db.execute("SELECT bytes,owner FROM reservations WHERE id=?", (reservation,)).fetchone()
        if not row or row["owner"] != owner or row["bytes"] < size:
            raise DomainError("reservation", "写入没有有效空间预留", 409)
        if shutil.disk_usage(self.root).free < self.settings()["storage"]["minimum_free_bytes"] + additional_bytes:
            raise DomainError("disk_space", "磁盘可用空间不足，任务已停止新增写入", 507)

    def release_reservation(self, reservation, owner):
        with self.transaction() as db:
            db.execute("DELETE FROM reservations WHERE id=? AND owner=?", (reservation, owner))

    @staticmethod
    def ref(db, owner, owner_id, sha, role):
        db.execute("INSERT OR IGNORE INTO object_refs VALUES(?,?,?,?)", (owner, owner_id, sha, role))

    @staticmethod
    def event(db, kind, object_id, payload=None):
        db.execute("INSERT INTO events(kind,object_id,payload,created) VALUES(?,?,?,?)", (kind, object_id, canonical(payload or {}), time.time()))

    def storage_status(self):
        with self.connect() as db:
            objects = db.execute("SELECT count(*),coalesce(sum(size),0) FROM objects").fetchone()
            reserved = db.execute("SELECT coalesce(sum(bytes),0) FROM reservations").fetchone()[0]
        disk = shutil.disk_usage(self.root)
        return {"object_count": objects[0], "object_bytes": objects[1], "reserved_bytes": reserved, "free_bytes": disk.free, "database_bytes": self.db_path.stat().st_size, "limits": self.settings()["storage"]}

    def row(self, table, identifier, db=None):
        allowed = {"sources", "books", "editions", "revisions", "branches", "review_requests", "proposals", "jobs", "exports"}
        if table not in allowed:
            raise ValueError(table)
        if db is None:
            with self.connect() as connection:
                return self.row(table, identifier, connection)
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise DomainError("not_found", "对象不存在", 404)
        return dict(row)
