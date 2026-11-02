"""Project-owned snapshots and bounded cache cleanup; no engineering dependencies."""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import time

from .project import DomainError, Project, SCHEMA_VERSION, canonical, digest, uid

SNAPSHOT_KIND = "yuecang-project-snapshot"
TMP_PREFIX = "maintenance_tmp:"
PENDING_GC = "maintenance_pending_gc"
TERMINAL = {"succeeded", "failed", "cancelled"}
TEMP_ROLES = {"temporary", "cache", "scratch"}
REBUILDABLE_KINDS = {"upload", "extraction", "resource", "ai_request", "ai_response", "cache"}
MAX_TREE_FILES = 100_000


def _plain(path):
    path = Path(path).expanduser().absolute()
    for item in (path, *path.parents):
        if not os.path.lexists(item):
            continue
        info = os.lstat(item)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DomainError("linked_maintenance_path", "维护路径不能经过链接或重解析点")
    return path


def _identity(path):
    info = os.lstat(_plain(path))
    return [info.st_dev, info.st_ino]


def _stable(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns)


def _file_hash(path):
    path = _plain(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise DomainError("maintenance_file", "维护输入不是普通文件")
    checksum, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
            size += len(chunk)
    if _stable(path.stat()) != _stable(before):
        raise DomainError("maintenance_drift", "维护期间文件发生变化", 409)
    return {"sha256": checksum.hexdigest(), "bytes": size}


def _json(path):
    path = _plain(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DomainError("maintenance_json", "维护清单缺失或非法") from exc
    if not isinstance(value, dict):
        raise DomainError("maintenance_json", "维护清单必须为对象")
    return value


def _write_json(path, value):
    path = _plain(path)
    with path.open("x", encoding="utf-8") as output:
        output.write(canonical(value) + "\n")
        output.flush()
        os.fsync(output.fileno())


@contextmanager
def _maintenance_lock(root):
    """OS releases this lock on a crash; the tiny lock file is deliberately retained."""
    path = _plain(Path(root) / ".maintenance.lock")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise DomainError("maintenance_lock", "维护锁不是独占普通文件")
    with path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DomainError("maintenance_busy", "另一个项目维护任务正在运行", 409) from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _readonly_db(path):
    path = _plain(path)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _db_identity(db, marker):
    try:
        version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        identity = db.execute("SELECT value FROM meta WHERE key='project_id'").fetchone()
        if (not version or int(version[0]) != SCHEMA_VERSION or not identity
                or marker != {"kind": "yuecang-project", "schema_version": SCHEMA_VERSION, "id": identity[0]}):
            raise DomainError("snapshot_identity", "数据库版本或项目身份不匹配")
        if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
            raise DomainError("database_integrity", "SQLite 完整性检查失败")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise DomainError("database_references", "数据库存在失效外键")
        return identity[0]
    except sqlite3.DatabaseError as exc:
        raise DomainError("unknown_database", "无法验证项目数据库") from exc


def _direct_object_shas(db):
    return {row[0] for row in db.execute("""
        SELECT asset_sha FROM sources UNION SELECT blocks_sha FROM sources WHERE blocks_sha IS NOT NULL
        UNION SELECT document_sha FROM revisions UNION SELECT payload_sha FROM review_requests
        UNION SELECT response_sha FROM review_requests WHERE response_sha IS NOT NULL
    """)}


def _required_objects(db):
    refs = {row[0] for row in db.execute("SELECT DISTINCT sha FROM object_refs")}
    if _direct_object_shas(db) - refs:
        raise DomainError("object_reference_missing", "原件、修订或审阅对象缺少保留引用")
    objects = {row["sha"]: dict(row) for row in db.execute("SELECT * FROM objects WHERE sha IN (SELECT sha FROM object_refs)")}
    if set(objects) != refs:
        raise DomainError("object_reference_missing", "保留引用指向缺失对象")
    return objects


def _resource_shas(value):
    if isinstance(value, list):
        for child in value:
            yield from _resource_shas(child)
    elif isinstance(value, dict):
        for resource in value.get("resources", []):
            if isinstance(resource, dict) and resource.get("sha256"):
                yield resource["sha256"]
        for key, child in value.items():
            if key != "resources" and isinstance(child, (list, dict)):
                yield from _resource_shas(child)


def _object_path(root, sha):
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise DomainError("object_hash", "非法内容对象标识")
    return _plain(Path(root) / "objects" / sha[:2] / sha)


def _verify_objects(root, objects):
    total = 0
    for sha, row in objects.items():
        path = _object_path(root, sha)
        try:
            actual = _file_hash(path)
        except OSError as exc:
            raise DomainError("object_missing", "保留对象文件缺失", details={"sha": sha}) from exc
        if actual != {"sha256": sha, "bytes": row["size"]}:
            raise DomainError("object_corrupt", "保留对象哈希或大小不符", details={"sha": sha})
        if row["kind"] in {"extraction", "revision"}:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise DomainError("object_corrupt", "结构文档无法读取") from exc
            if set(_resource_shas(document)) - set(objects):
                raise DomainError("resource_reference_missing", "结构文档资源未纳入保留引用")
        total += row["size"]
    return total


def _tree_files(root):
    root = _plain(root)
    result, stack = [], [root]
    while stack:
        current = stack.pop()
        _plain(current)
        with os.scandir(current) as entries:
            for entry in entries:
                path = _plain(Path(entry.path))
                mode = path.stat().st_mode
                if stat.S_ISDIR(mode):
                    stack.append(path)
                elif stat.S_ISREG(mode):
                    result.append(path)
                else:
                    raise DomainError("maintenance_file", "维护目录中存在非常规对象")
                if len(result) + len(stack) > MAX_TREE_FILES:
                    raise DomainError("maintenance_limit", "维护目录条目超过安全上限")
    return sorted(result)


def _exports(db, root):
    result = []
    for row in db.execute("SELECT id,path FROM exports"):
        relative = PurePosixPath(row["path"])
        if (relative.as_posix() != "exports/" + row["id"] or relative.is_absolute()
                or ".." in relative.parts or "\\" in row["id"] or "/" in row["id"]):
            raise DomainError("export_path", "已登记导出路径越界")
        path = _plain(Path(root) / relative)
        if not path.is_dir():
            raise DomainError("export_missing", "已登记导出目录缺失")
        result.extend(_tree_files(path))
    return result


def verify_project(project: Project):
    with _maintenance_lock(project.root), closing(_readonly_db(project.db_path)) as db:
        db.execute("BEGIN")
        project_id = _db_identity(db, _json(project.root / "project.json"))
        objects = _required_objects(db)
        total = _verify_objects(project.root, objects)
        exports = _exports(db, project.root)
        return {"ok": True, "project_id": project_id, "objects": len(objects), "object_bytes": total,
                "export_files": len(exports), "unreferenced_objects": db.execute("SELECT count(*) FROM objects WHERE sha NOT IN (SELECT sha FROM object_refs)").fetchone()[0],
                "scope": "database, retained object hashes/resource closure and registered export presence"}


def _copy_verified(source, destination, *, expected=None):
    source, destination = _plain(source), _plain(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    checksum, size = hashlib.sha256(), 0
    with source.open("rb") as inp, destination.open("xb") as out:
        for chunk in iter(lambda: inp.read(1024 * 1024), b""):
            out.write(chunk)
            checksum.update(chunk)
            size += len(chunk)
        out.flush()
        os.fsync(out.fileno())
    actual = {"sha256": checksum.hexdigest(), "bytes": size}
    if _stable(before) != _stable(source.stat()) or (expected is not None and actual != expected):
        raise DomainError("snapshot_drift", "复制过程中来源发生变化或哈希不符", 409)
    return actual


@contextmanager
def _fresh_directory(destination):
    destination = _plain(destination)
    if not destination.parent.is_dir():
        raise DomainError("snapshot_parent", "目标父目录不存在")
    if os.path.lexists(destination):
        raise DomainError("destination_exists", "目标必须是尚不存在的新目录", 409)
    destination.mkdir()
    identity = _identity(destination)
    try:
        yield destination
    except BaseException:
        # Only the directory exclusively created in this call is eligible.
        if destination.is_dir() and _identity(destination) == identity:
            try:
                _tree_files(destination)  # Refuse links before touching the new output tree.
                shutil.rmtree(destination)
            except (OSError, DomainError):
                pass  # Preserve the partial result if its ownership boundary changed.
        raise


def backup_project(project: Project, destination=None, *, job=None):
    destination = _plain(destination or (project.root / "backups" / uid()))
    if destination == project.root or project.root.is_relative_to(destination):
        raise DomainError("snapshot_destination", "备份目标不能覆盖项目或其父目录")
    if destination.is_relative_to(project.root) and destination.parent != project.root / "backups":
        raise DomainError("snapshot_destination", "项目内部备份只能写入 backups 的新子目录")
    with _maintenance_lock(project.root), _fresh_directory(destination) as target:
        snapshot_db = target / "library.sqlite3"
        with closing(project.connect()) as source, closing(sqlite3.connect(snapshot_db)) as copied:
            source.backup(copied)
            copied.execute("PRAGMA foreign_keys=ON")
            copied.row_factory = sqlite3.Row
            marker = _json(project.root / "project.json")
            project_id = _db_identity(copied, marker)
            objects = _required_objects(copied)
            _verify_objects(project.root, objects)
            exported_files = _exports(copied, project.root)
            # Do not retain metadata for in-flight/unreferenced physical objects.
            copied.execute("DELETE FROM objects WHERE sha NOT IN (SELECT sha FROM object_refs)")
            copied.execute("DELETE FROM reservations")
            copied.execute("DELETE FROM meta WHERE key LIKE ? OR key=?", (TMP_PREFIX + "%", PENDING_GC))
            settings = copied.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            if settings:
                config = json.loads(settings[0])
                config.get("ai", {}).pop("api_key", None)
                copied.execute("UPDATE meta SET value=? WHERE key='settings'", (canonical(config),))
            copied.commit()
            copied.execute("PRAGMA journal_mode=DELETE")
        _write_json(target / "project.json", marker)
        files = {"library.sqlite3": _file_hash(snapshot_db), "project.json": _file_hash(target / "project.json")}
        for sha, row in objects.items():
            relative = "objects/" + sha[:2] + "/" + sha
            files[relative] = _copy_verified(_object_path(project.root, sha), target / relative,
                                             expected={"sha256": sha, "bytes": row["size"]})
        for source in exported_files:
            relative = source.relative_to(project.root).as_posix()
            files[relative] = _copy_verified(source, target / relative)
        manifest = {"kind": SNAPSHOT_KIND, "schema_version": SCHEMA_VERSION, "project_id": project_id,
                    "created_at": time.time(), "files": files, "objects": len(objects),
                    "object_bytes": sum(row["size"] for row in objects.values()),
                    "excluded": ["provider-key", "tmp", "logs", "other backups", "unreferenced objects"]}
        manifest["manifest_sha256"] = digest(manifest)
        with project.transaction() as db:
            from ..application.service import Workbench
            Workbench(project)._job_guard(db, job)
            _write_json(target / "manifest.json", manifest)
            project.event(db, "project.backup", project_id, {"manifest_sha256": manifest["manifest_sha256"], "objects": len(objects)})
        return {"path": str(target), "project_id": project_id, "objects": len(objects),
                "bytes": sum(item["bytes"] for item in files.values()), "manifest_sha256": manifest["manifest_sha256"]}


def verify_snapshot(snapshot):
    root = _plain(snapshot)
    manifest = _json(root / "manifest.json")
    supplied = manifest.get("manifest_sha256")
    if (manifest.get("kind") != SNAPSHOT_KIND or manifest.get("schema_version") != SCHEMA_VERSION
            or supplied != digest({key: value for key, value in manifest.items() if key != "manifest_sha256"})
            or not isinstance(manifest.get("files"), dict)):
        raise DomainError("snapshot_manifest", "备份清单身份或哈希不符")
    expected = set(manifest["files"])
    if not {"library.sqlite3", "project.json"}.issubset(expected):
        raise DomainError("snapshot_manifest", "备份缺少数据库或项目标记")
    for name, entry in manifest["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise DomainError("snapshot_path", "备份清单路径越界")
        if name not in {"library.sqlite3", "project.json"} and not name.startswith(("objects/", "exports/")):
            raise DomainError("snapshot_path", "备份清单包含非产品内容")
        if _file_hash(root / relative) != entry:
            raise DomainError("snapshot_hash", "备份文件哈希或大小不符")
    actual = {path.relative_to(root).as_posix() for path in _tree_files(root)}
    if actual != expected | {"manifest.json"}:
        raise DomainError("snapshot_files", "备份文件集合与清单不一致")
    with closing(_readonly_db(root / "library.sqlite3")) as db:
        identity = _db_identity(db, _json(root / "project.json"))
        if identity != manifest["project_id"]:
            raise DomainError("snapshot_identity", "备份项目身份不符")
        objects = _required_objects(db)
        expected_objects = {"objects/" + sha[:2] + "/" + sha for sha in objects}
        if expected_objects != {name for name in expected if name.startswith("objects/")}:
            raise DomainError("snapshot_objects", "备份对象集合与引用闭包不符")
        if len(objects) != manifest["objects"] or _verify_objects(root, objects) != manifest["object_bytes"]:
            raise DomainError("snapshot_objects", "备份对象统计不符")
        if {path.relative_to(root).as_posix() for path in _exports(db, root)} != {name for name in expected if name.startswith("exports/")}:
            raise DomainError("snapshot_exports", "备份导出集合与登记不符")
    return manifest


def restore_project(snapshot, destination):
    snapshot, destination = _plain(snapshot), _plain(destination)
    if destination == snapshot or destination.is_relative_to(snapshot) or snapshot.is_relative_to(destination):
        raise DomainError("restore_destination", "恢复目标必须独立于备份目录")
    manifest = verify_snapshot(snapshot)
    with _fresh_directory(destination) as target:
        for name, expected in manifest["files"].items():
            _copy_verified(snapshot / name, target / name, expected=expected)
        with closing(sqlite3.connect(target / "library.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            _db_identity(db, _json(target / "project.json"))
            # Restored work must not auto-execute paths or reuse leases from another process.
            paused = db.execute("UPDATE jobs SET state='paused',token=NULL,lease_until=NULL,updated=? WHERE state IN ('queued','running')", (time.time(),)).rowcount
            db.execute("DELETE FROM reservations")
            for row in db.execute("SELECT id,path,report FROM exports").fetchall():
                report = json.loads(row["report"])
                for field in ("destination", "output_path", "manifest_path"):
                    if report.get(field):
                        if field == "destination":
                            report[field] = str(target / row["path"])
                        else:
                            # Only the leaf is needed; old snapshots may come
                            # from either Windows or POSIX hosts.
                            leaf = report[field].replace("\\", "/").rsplit("/", 1)[-1]
                            report[field] = str(target / row["path"] / leaf)
                db.execute("UPDATE exports SET report=? WHERE id=?", (canonical(report), row["id"]))
            db.commit()
        restored = Project(target)
        result = verify_project(restored)
        result.update(path=str(target), backup_manifest_sha256=manifest["manifest_sha256"], paused_jobs=paused,
                      provider_key_restored=False)
        _write_json(target / "restore-receipt.json", result)
        return result


def register_temporary(project: Project, path, job_id):
    path = _plain(path)
    if path.parent != project.root / "tmp" or not path.exists() or not (path.is_file() or path.is_dir()):
        raise DomainError("temporary_path", "只能登记项目 tmp 内直接创建的普通对象")
    token = uid()
    record = {"kind": "yuecang-temporary", "job_id": job_id, "relative": path.relative_to(project.root).as_posix(),
              "identity": _identity(path), "created_at": time.time()}
    with project.transaction() as db:
        if not db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise DomainError("temporary_owner", "临时对象必须绑定现有任务")
        db.execute("INSERT INTO meta VALUES(?,?)", (TMP_PREFIX + token, canonical(record)))
    return token


def forget_temporary(project: Project, token):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise DomainError("temporary_owner", "非法临时对象登记")
    with project.transaction() as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", (TMP_PREFIX + token,)).fetchone()
        if row:
            record = json.loads(row[0])
            path = _temporary_path(project, record)
            if os.path.lexists(path):
                raise DomainError("temporary_exists", "临时对象仍存在，不能丢弃归属记录")
            db.execute("DELETE FROM meta WHERE key=?", (TMP_PREFIX + token,))


def _temporary_path(project, record):
    relative = PurePosixPath(record.get("relative", ""))
    if (record.get("kind") != "yuecang-temporary" or len(relative.parts) != 2
            or relative.parts[0] != "tmp" or relative.is_absolute() or ".." in relative.parts
            or "\\" in relative.as_posix() or ":" in relative.as_posix()):
        raise DomainError("temporary_path", "临时对象登记越界")
    return _plain(project.root / relative)


def _idle(db):
    if db.execute("SELECT 1 FROM jobs WHERE state NOT IN ('succeeded','failed','cancelled') OR coalesce(lease_until,0)>? LIMIT 1", (time.time(),)).fetchone():
        raise DomainError("active_jobs", "存在未终结任务或有效租约，暂不清理", 409)
    if db.execute("SELECT 1 FROM reservations LIMIT 1").fetchone():
        raise DomainError("active_reservations", "存在空间预留，先由任务所有者收尾", 409)


def _pending(db):
    row = db.execute("SELECT value FROM meta WHERE key=?", (PENDING_GC,)).fetchone()
    return json.loads(row[0]) if row else []


def _set_pending(db, value):
    if value:
        db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (PENDING_GC, canonical(value)))
    else:
        db.execute("DELETE FROM meta WHERE key=?", (PENDING_GC,))


def _cache_candidates(project, db, cutoff):
    _idle(db)
    refs = [dict(row) for row in db.execute("""SELECT r.* FROM object_refs r JOIN jobs j ON j.id=r.owner_id
        WHERE r.owner='job' AND r.role IN ('temporary','cache','scratch')
          AND j.state IN ('succeeded','cancelled') AND j.updated<=?""", (cutoff,))]
    releasable = {(row["owner"], row["owner_id"], row["sha"], row["role"]) for row in refs}
    protected = _direct_object_shas(db)
    candidates = []
    # JobQueue drops input refs when terminal work succeeds or is cancelled.
    # Reclaim its aged, registered derivatives without scanning arbitrary files.
    unreferenced = {row["sha"] for row in db.execute("SELECT o.sha,o.kind FROM objects o WHERE o.created<=? AND NOT EXISTS (SELECT 1 FROM object_refs r WHERE r.sha=o.sha)", (cutoff,))
                    if row["kind"] in REBUILDABLE_KINDS}
    for sha in sorted({row["sha"] for row in refs} | unreferenced):
        row = db.execute("SELECT * FROM objects WHERE sha=?", (sha,)).fetchone()
        other = [tuple(item) for item in db.execute("SELECT owner,owner_id,sha,role FROM object_refs WHERE sha=?", (sha,))]
        if not row or sha in protected or row["kind"] in {"source", "revision"} or any(item not in releasable for item in other):
            continue
        path = _object_path(project.root, sha)
        if path.stat().st_nlink != 1 or _file_hash(path) != {"sha256": sha, "bytes": row["size"]}:
            raise DomainError("cache_identity", "待清理缓存对象存在链接或内容漂移")
        candidates.append({"sha": sha, "bytes": row["size"], "identity": _identity(path)})
    temporary = []
    for row in db.execute("SELECT key,value FROM meta WHERE key LIKE ?", (TMP_PREFIX + "%",)):
        record = json.loads(row["value"])
        job = db.execute("SELECT state,updated FROM jobs WHERE id=?", (record["job_id"],)).fetchone()
        if not job or job["state"] not in TERMINAL or job["updated"] > cutoff:
            continue
        if job["state"] == "failed" and job["updated"] > time.time() - 72 * 3600:
            continue
        path = _temporary_path(project, record)
        files = []
        if path.exists():
            if _identity(path) != record["identity"]:
                raise DomainError("temporary_identity", "临时对象已被替换")
            files = _tree_files(path) if path.is_dir() else [path]
            if any(item.stat().st_mtime > cutoff for item in files) or path.stat().st_mtime > cutoff:
                continue
            if any(item.stat().st_nlink != 1 for item in files):
                raise DomainError("temporary_identity", "临时对象含共享硬链接")
        temporary.append({"key": row["key"], "record": record, "path": str(path),
                          "bytes": sum(item.stat().st_size for item in files)})
    return refs, candidates, temporary


def cleanup_cache(project: Project, *, apply=False, minimum_age_seconds=86400):
    if (type(minimum_age_seconds) not in (int, float) or not math.isfinite(minimum_age_seconds)
            or minimum_age_seconds < 0):
        raise DomainError("cache_retention", "缓存保留期限必须为非负秒数")
    cutoff = time.time() - minimum_age_seconds
    with _maintenance_lock(project.root):
        before = shutil.disk_usage(project.root).free
        with project.transaction() as db:
            refs, candidates, temporary = _cache_candidates(project, db, cutoff)
            pending = _pending(db)
            result = {"mode": "apply" if apply else "dry-run", "releasable_job_refs": len(refs),
                      "objects": len(candidates), "temporary_objects": len(temporary), "pending_objects": len(pending),
                      "logical_bytes": sum(item["bytes"] for item in candidates + temporary + pending),
                      "deleted_bytes": 0, "deleted_objects": 0, "deleted_temporary": 0,
                      "protected": ["sources", "revisions", "all remaining refs including job_retry", "unregistered tmp", "exports", "backups"]}
            if not apply:
                return result
            for ref in refs:
                db.execute("DELETE FROM object_refs WHERE owner=? AND owner_id=? AND sha=? AND role=?", tuple(ref[key] for key in ("owner", "owner_id", "sha", "role")))
            for item in candidates:
                db.execute("DELETE FROM objects WHERE sha=?", (item["sha"],))
            # Commit removal of references first. A crash leaves only safe orphan files.
            _set_pending(db, list({item["sha"]: item for item in pending + candidates}.values()))
        # A new writer may have registered the same hash between transactions. Check
        # again while blocking reservations, and preserve any newly registered object.
        with project.transaction() as db:
            _idle(db)
            pending = _pending(db)
            remaining = []
            for index, item in enumerate(pending):
                if db.execute("SELECT 1 FROM objects WHERE sha=?", (item["sha"],)).fetchone():
                    continue
                path = _object_path(project.root, item["sha"])
                try:
                    if path.exists():
                        if (_identity(path) != item["identity"] or path.stat().st_nlink != 1
                                or _file_hash(path) != {"sha256": item["sha"], "bytes": item["bytes"]}):
                            raise DomainError("cache_identity", "缓存回收对象发生漂移")
                        path.unlink()
                        result["deleted_bytes"] += item["bytes"]
                    result["deleted_objects"] += 1
                except (OSError, DomainError) as exc:
                    remaining = pending[index:]
                    result["partial_error"] = str(exc)
                    break
            _set_pending(db, remaining)
            # Only registered terminal-job temporary objects are considered.
            for item in temporary:
                path = _temporary_path(project, item["record"])
                if path.exists():
                    if _identity(path) != item["record"]["identity"]:
                        raise DomainError("temporary_identity", "临时对象在清理前发生变化")
                    _tree_files(path) if path.is_dir() else _plain(path)
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    result["deleted_bytes"] += item["bytes"]
                db.execute("DELETE FROM meta WHERE key=?", (item["key"],))
                result["deleted_temporary"] += 1
            project.event(db, "cache.cleaned", uid(), {key: value for key, value in result.items() if key != "protected"})
        result["net_freed_bytes"] = shutil.disk_usage(project.root).free - before
        result["note"] = "逻辑删除量不等于磁盘实际释放；卷净差包含并发活动。"
        return result
