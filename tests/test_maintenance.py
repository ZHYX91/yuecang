from contextlib import closing
import gc
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from yuecang.storage.project import DomainError, Project, canonical, digest, uid
from yuecang.storage import maintenance as m


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yuecang-maintenance-")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temp.name)
        self.project = Project(self.root / "project")

    def source(self, text="synthetic source", resources=None):
        asset = self.project.put_bytes(text.encode(), "source")
        blocks = [{"id": "block-1", "type": "paragraph", "text": text, "resources": resources or []}]
        extracted = self.project.put_json(blocks, "extraction")
        source_id = uid()
        with self.project.transaction() as db:
            db.execute("INSERT INTO sources(id,asset_sha,name,status,blocks_sha,block_count,created) VALUES(?,?,?,?,?,?,?)",
                       (source_id, asset, "synthetic.txt", "ready", extracted, 1, time.time()))
            self.project.ref(db, "source", source_id, asset, "original")
            self.project.ref(db, "source", source_id, extracted, "extraction")
            for resource in resources or []:
                self.project.ref(db, "source", source_id, resource["sha256"], "resource")
        return source_id, asset, extracted

    def revision(self):
        book, edition, revision = uid(), uid(), uid()
        sha = self.project.put_json([{"id": "revision-block", "text": "synthetic revision", "resources": []}], "revision")
        with self.project.transaction() as db:
            db.execute("INSERT INTO books(id,title,author,created) VALUES(?,?,?,?)", (book, "Synthetic", "Tester", time.time()))
            db.execute("INSERT INTO editions(id,book_id,label,language) VALUES(?,?,?,?)", (edition, book, "first", "en"))
            db.execute("INSERT INTO revisions(id,book_id,edition_id,document_sha,metadata,created) VALUES(?,?,?,?,?,?)",
                       (revision, book, edition, sha, "{}", time.time()))
            self.project.ref(db, "revision", revision, sha, "document")
        return revision, sha

    def job(self, state="succeeded", *, age=2 * 86400, lease=None):
        job_id = uid()
        now = time.time() - age
        with self.project.transaction() as db:
            db.execute("INSERT INTO jobs(id,kind,payload,state,input_key,created,updated,lease_until,token) VALUES(?,?,?,?,?,?,?,?,?)",
                       (job_id, "test", "{}", state, uid(), now, now, lease, "current-token"))
        return job_id

    def cache(self, job_id, text="rebuildable cache", *, owner="job", role="cache", kind="cache"):
        sha = self.project.put_bytes(text.encode(), kind)
        with self.project.transaction() as db:
            self.project.ref(db, owner, job_id, sha, role)
        return sha

    def assert_code(self, code, call):
        with self.assertRaises(DomainError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)

    def test_backup_restore_copies_only_referenced_objects_and_never_key(self):
        resource = self.project.put_bytes(b"synthetic image resource", "resource")
        source_id, asset, extraction = self.source(resources=[{"sha256": resource}])
        revision, revision_sha = self.revision()
        orphan = self.project.put_bytes(b"abandoned candidate", "cache")
        (self.project.root / "provider-key").write_text("synthetic-secret", encoding="utf-8")
        report = m.backup_project(self.project)
        snapshot = Path(report["path"])
        manifest = m.verify_snapshot(snapshot)
        self.assertEqual(report["objects"], 4)
        self.assertFalse((snapshot / "provider-key").exists())
        self.assertFalse(m._object_path(snapshot, orphan).exists())
        self.assertEqual(set(name for name in manifest["files"] if name.startswith("objects/")),
                         {"objects/" + sha[:2] + "/" + sha for sha in (asset, extraction, resource, revision_sha)})
        destination = self.root / "restored"
        result = m.restore_project(snapshot, destination)
        self.assertTrue(result["ok"])
        self.assertFalse(result["provider_key_restored"])
        restored = Project(destination)
        self.assertEqual(restored.meta("project_id"), self.project.meta("project_id"))
        self.assertEqual(restored.read_json(revision_sha)[0]["text"], "synthetic revision")
        self.assertEqual(restored.settings(private=True)["ai"]["api_key"], "")
        with closing(restored.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM objects").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT asset_sha FROM sources WHERE id=?", (source_id,)).fetchone()[0], asset)

    def test_registered_exports_are_restored_but_unregistered_files_are_not(self):
        revision, _ = self.revision()
        export_id = uid()
        exported = self.project.root / "exports" / export_id
        exported.mkdir()
        (exported / "book.md").write_text("synthetic exported text", encoding="utf-8")
        (self.project.root / "exports" / "unregistered.txt").write_text("unowned", encoding="utf-8")
        report = {"destination": str(exported), "output_path": str(exported / "book.md"), "manifest_path": str(exported / "manifest.json")}
        with self.project.transaction() as db:
            db.execute("INSERT INTO exports VALUES(?,?,?,?,?,?)", (export_id, revision, "markdown", "exports/" + export_id, canonical(report), time.time()))
        snapshot = m.backup_project(self.project)
        restored = self.root / "restored"
        m.restore_project(snapshot["path"], restored)
        self.assertEqual((restored / "exports" / export_id / "book.md").read_text(), "synthetic exported text")
        self.assertFalse((restored / "exports" / "unregistered.txt").exists())
        with closing(m._readonly_db(restored / "library.sqlite3")) as db:
            actual = json.loads(db.execute("SELECT report FROM exports").fetchone()[0])
        self.assertEqual(actual, {"destination": str(restored / "exports" / export_id),
                                  "output_path": str(restored / "exports" / export_id / "book.md"),
                                  "manifest_path": str(restored / "exports" / export_id / "manifest.json")})

    def test_snapshot_uses_database_snapshot_not_later_live_references(self):
        self.source("before snapshot")
        copy = m._copy_verified
        injected = False

        def add_source_after_snapshot(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                self.source("arrived after database snapshot")
            return copy(*args, **kwargs)

        with patch.object(m, "_copy_verified", side_effect=add_source_after_snapshot):
            report = m.backup_project(self.project)
        with closing(m._readonly_db(Path(report["path"]) / "library.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sources").fetchone()[0], 1)
        with closing(self.project.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sources").fetchone()[0], 2)
        m.verify_snapshot(report["path"])

    def test_restore_pauses_active_jobs_and_drops_stale_reservations(self):
        job = self.job("running", age=0, lease=time.time() + 3600)
        with self.project.transaction() as db:
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,?)", (uid(), "temporary", 12, job, time.time()))
        report = m.backup_project(self.project)
        destination = self.root / "restored"
        result = m.restore_project(report["path"], destination)
        self.assertEqual(result["paused_jobs"], 1)
        with closing(m._readonly_db(destination / "library.sqlite3")) as db:
            self.assertEqual(tuple(db.execute("SELECT state,token,lease_until FROM jobs").fetchone()), ("paused", None, None))
            self.assertEqual(db.execute("SELECT count(*) FROM reservations").fetchone()[0], 0)

    def test_backup_rejects_stale_job_before_publishing(self):
        self.source()
        job_id = self.job("running", age=0, lease=time.time() + 3600)
        destination = self.root / "stale-backup"
        self.assert_code("stale_job", lambda: m.backup_project(self.project, destination, job={"id": job_id, "token": "old-token"}))
        self.assertFalse(destination.exists())

    def test_backup_and_restore_never_overwrite_existing_target(self):
        existing = self.root / "existing"
        existing.mkdir()
        (existing / "keep").write_text("preserve")
        self.assert_code("destination_exists", lambda: m.backup_project(self.project, existing))
        report = m.backup_project(self.project)
        self.assert_code("destination_exists", lambda: m.restore_project(report["path"], existing))
        self.assertEqual((existing / "keep").read_text(), "preserve")

    def test_backup_copy_failure_removes_only_fresh_output(self):
        self.source()
        destination = self.root / "partial-backup"
        with patch.object(m, "_copy_verified", side_effect=OSError("synthetic disk failure")), self.assertRaises(OSError):
            m.backup_project(self.project, destination)
        self.assertFalse(destination.exists())
        self.assertTrue(m.verify_project(self.project)["ok"])

    def test_verify_detects_missing_reference_and_corrupted_object(self):
        source_id, asset, _ = self.source()
        with self.project.transaction() as db:
            db.execute("DELETE FROM object_refs WHERE sha=?", (asset,))
        self.assert_code("object_reference_missing", lambda: m.verify_project(self.project))
        with self.project.transaction() as db:
            self.project.ref(db, "source", source_id, asset, "original")
        self.project.object_path(asset).write_bytes(b"corrupt")
        self.assert_code("object_corrupt", lambda: m.verify_project(self.project))

    def test_verify_detects_resource_missing_from_reference_closure(self):
        sha = self.project.put_bytes(b"image", "resource")
        source_id, _, _ = self.source(resources=[{"sha256": sha}])
        with self.project.transaction() as db:
            db.execute("DELETE FROM object_refs WHERE owner_id=? AND sha=?", (source_id, sha))
        self.assert_code("resource_reference_missing", lambda: m.verify_project(self.project))

    def test_snapshot_manifest_tamper_and_extra_key_are_rejected(self):
        self.source()
        report = m.backup_project(self.project)
        snapshot = Path(report["path"])
        (snapshot / "provider-key").write_text("must not restore")
        self.assert_code("snapshot_files", lambda: m.verify_snapshot(snapshot))
        (snapshot / "provider-key").unlink()
        marker = snapshot / "project.json"
        marker.write_text("{}")
        self.assert_code("snapshot_hash", lambda: m.verify_snapshot(snapshot))

    def test_manifest_path_traversal_is_rejected_even_with_recomputed_hash(self):
        report = m.backup_project(self.project)
        snapshot = Path(report["path"])
        manifest = json.loads((snapshot / "manifest.json").read_text())
        manifest["files"]["../outside"] = {"sha256": "0" * 64, "bytes": 0}
        manifest["manifest_sha256"] = digest({key: value for key, value in manifest.items() if key != "manifest_sha256"})
        (snapshot / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
        self.assert_code("snapshot_path", lambda: m.verify_snapshot(snapshot))

    def test_cache_protects_sources_revisions_retry_refs_and_unowned_objects(self):
        _, original, _ = self.source()
        _, revision = self.revision()
        job = self.job()
        cache = self.cache(job)
        retry = self.cache(job, "retry body", owner="job_retry")
        orphan = self.project.put_bytes(b"no proven cleanup owner", "cache")
        with self.project.transaction() as db:
            self.project.ref(db, "job", job, original, "cache")
            self.project.ref(db, "job", job, revision, "temporary")
        dry = m.cleanup_cache(self.project)
        self.assertEqual(dry["objects"], 1)
        self.assertTrue(self.project.object_path(cache).exists())
        result = m.cleanup_cache(self.project, apply=True)
        self.assertEqual(result["deleted_objects"], 1)
        self.assertFalse(self.project.object_path(cache).exists())
        for sha in (original, revision, retry, orphan):
            self.assertTrue(self.project.object_path(sha).exists())
        self.assertTrue(m.verify_project(self.project)["ok"])

    def test_cache_refuses_any_active_job_lease_or_reservation(self):
        job = self.job("waiting_review", age=0)
        self.assert_code("active_jobs", lambda: m.cleanup_cache(self.project, apply=True))
        with self.project.transaction() as db:
            db.execute("UPDATE jobs SET state='succeeded',lease_until=?", (time.time() + 60,))
        self.assert_code("active_jobs", lambda: m.cleanup_cache(self.project))
        with self.project.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=NULL")
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,?)", (uid(), "temporary", 10, job, time.time()))
        self.assert_code("active_reservations", lambda: m.cleanup_cache(self.project))

    def test_cache_releases_only_safe_terminal_roles(self):
        success = self.job()
        failed = self.job("failed", age=4 * 86400)
        durable = self.cache(success, "durable input", role="input")
        failed_body = self.cache(failed, "failure evidence")
        self.assertEqual(m.cleanup_cache(self.project, apply=True)["deleted_objects"], 0)
        self.assertTrue(self.project.object_path(durable).exists())
        self.assertTrue(self.project.object_path(failed_body).exists())

    def test_cache_failure_after_metadata_commit_is_resumable_without_dangling_refs(self):
        job = self.job()
        cache = self.cache(job)
        original = m._idle
        calls = 0

        def fail_second(db):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DomainError("active_jobs", "simulated newly queued job", 409)
            return original(db)

        with patch.object(m, "_idle", side_effect=fail_second):
            self.assert_code("active_jobs", lambda: m.cleanup_cache(self.project, apply=True))
        self.assertTrue(self.project.object_path(cache).exists())
        self.assertTrue(m.verify_project(self.project)["ok"])
        result = m.cleanup_cache(self.project, apply=True)
        self.assertEqual(result["deleted_objects"], 1)
        self.assertFalse(self.project.object_path(cache).exists())

    def test_gc_pending_preserves_a_hash_registered_again(self):
        job = self.job()
        sha = self.cache(job)
        original = m._idle
        calls = 0

        def fail_second(db):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DomainError("active_jobs", "simulated race", 409)
            return original(db)

        with patch.object(m, "_idle", side_effect=fail_second):
            self.assert_code("active_jobs", lambda: m.cleanup_cache(self.project, apply=True))
        self.assertEqual(self.project.put_bytes(b"rebuildable cache", "content"), sha)
        with self.project.transaction() as db:
            self.project.ref(db, "pin", "manual", sha, "keep")
        result = m.cleanup_cache(self.project, apply=True)
        self.assertEqual(result["deleted_objects"], 0)
        self.assertTrue(self.project.object_path(sha).exists())
        self.assertTrue(m.verify_project(self.project)["ok"])

    def test_registered_expired_tmp_deleted_unregistered_and_recent_preserved(self):
        job = self.job()
        expired = self.project.root / "tmp" / "owned"
        expired.mkdir()
        child = expired / "result.txt"
        child.write_text("synthetic temporary result")
        m.register_temporary(self.project, expired, job)
        old = time.time() - 2 * 86400
        os.utime(child, (old, old))
        os.utime(expired, (old, old))
        recent = self.project.root / "tmp" / "recent"
        recent.write_text("recent")
        m.register_temporary(self.project, recent, job)
        unregistered = self.project.root / "tmp" / (uid() + ".partial")
        unregistered.write_text("unknown owner")
        os.utime(unregistered, (old, old))
        result = m.cleanup_cache(self.project, apply=True)
        self.assertEqual(result["deleted_temporary"], 1)
        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unregistered.exists())

    def test_temporary_registration_cannot_target_source_and_cannot_forget_live(self):
        job = self.job()
        self.assert_code("temporary_path", lambda: m.register_temporary(self.project, self.project.root / "objects", job))
        temp = self.project.root / "tmp" / "owned"
        temp.write_text("temporary")
        token = m.register_temporary(self.project, temp, job)
        self.assert_code("temporary_exists", lambda: m.forget_temporary(self.project, token))
        temp.unlink()
        m.forget_temporary(self.project, token)
        with closing(self.project.connect()) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM meta WHERE key=?", (m.TMP_PREFIX + token,)).fetchone())

    def test_links_and_shared_hardlinks_are_not_cleaned(self):
        job = self.job()
        sha = self.cache(job)
        outside = self.root / "shared"
        try:
            os.link(self.project.object_path(sha), outside)
        except OSError as exc:
            self.skipTest(str(exc))
        self.assert_code("cache_identity", lambda: m.cleanup_cache(self.project, apply=True))
        self.assertEqual(outside.read_bytes(), b"rebuildable cache")

    def test_symlink_in_snapshot_or_tmp_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.project.root / "tmp" / "linked").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        self.assert_code("linked_maintenance_path", lambda: m.register_temporary(self.project, self.project.root / "tmp" / "linked", self.job()))
        self.assertEqual(list(outside.iterdir()), [])

    def test_maintenance_os_lock_serializes_and_releases_after_exception(self):
        with m._maintenance_lock(self.project.root):
            self.assert_code("maintenance_busy", lambda: m.verify_project(self.project))
        self.assertTrue(m.verify_project(self.project)["ok"])


if __name__ == "__main__":
    unittest.main()
