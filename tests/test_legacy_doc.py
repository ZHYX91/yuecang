"""Synthetic DOC conversion contracts and real optional LibreOffice coverage."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import zipfile

import pytest

from yuecang.formats import extract, legacy_doc as doc
from yuecang.formats.common import ResourceBudgetExceeded, resource_budget
from yuecang.jobs import JobQueue, _attempt_stopped, _process_stamp, run_isolated_one
from yuecang.storage.project import DomainError

OLE = bytes.fromhex("d0cf11e0a1b11ae1")
LINUX = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux owned-process supervision")


def make_docx(path):
    """Complete minimal OOXML package accepted by real Word-format readers."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>''')
        archive.writestr("_rels/.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>''')
        archive.writestr("word/document.xml", '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>合成文档验收</w:t></w:r></w:p>
<w:p><w:r><w:t>正文保持原样：天地玄黄，Unicode Ω。</w:t></w:r></w:p>
<w:tbl><w:tblPr><w:tblW w:w="4800" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr>
<w:tblGrid><w:gridCol w:w="2400"/><w:gridCol w:w="2400"/></w:tblGrid>
<w:tr><w:tc><w:tcPr><w:tcW w:w="2400" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>合成表格单元</w:t></w:r></w:p></w:tc>
<w:tc><w:tcPr><w:tcW w:w="2400" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>第二列</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl><w:p/><w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
</w:body></w:document>''')
    return path


def codes(result):
    return {issue["code"] for issue in result.issues}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "original.doc"
    path.write_bytes(OLE + b"synthetic adapter fixture; not a real Word file")
    return path


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    executable = tmp_path / "converter"
    executable.write_text("unused", encoding="utf-8")
    monkeypatch.setattr(doc, "detect_converters", lambda _: [
        {"id": name, "available": True} for name in doc.CONVERTERS])
    monkeypatch.setattr(doc, "_libreoffice_path", lambda _: str(executable))
    return executable


def test_adapter_preserves_original_resources_and_private_profile(source, adapter, tmp_path, monkeypatch):
    original = source.read_bytes()
    snapshots = []
    def convert(work, spec, cancel_check):
        copy = Path(spec["input"])
        assert copy != source and copy.read_bytes() == original
        assert not copy.stat().st_mode & stat.S_IWUSR
        profile = (work / "profile/user/registrymodifications.xcu").read_text()
        assert 'oor:name="DisableMacrosExecution"' in profile and '<value>true</value>' in profile
        assert 'oor:name="DisableActiveContent"' in profile
        assert 'oor:name="Link"' in profile and '<value>2</value>' in profile
        assert source.as_posix() not in " ".join(spec["command"])
        assert any(arg.startswith("-env:UserInstallation=file:") for arg in spec["command"])
        snapshots.append(work)
        make_docx(Path(spec["output"]))
        return {"status": "ok"}
    monkeypatch.setattr(doc, "_run_supervisor", convert)
    resources = tmp_path / "resources"
    reservations = []
    with resource_budget(resources, reserve=reservations.append, initial_bytes=len(original)):
        result = doc.extract_doc(source, resources)
    assert source.read_bytes() == original and source.stat().st_mode & stat.S_IWUSR
    assert all(not path.exists() for path in snapshots)
    assert result.title == "original" and result.metadata["source_format"] == "doc"
    assert result.metadata["conversion"]["converter"] == "libreoffice"
    assert result.metadata["conversion"]["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert result.metadata["conversion"]["extractor"] == "docx-ooxml-v1"
    assert "合成文档验收" in "".join(block["text"] for block in result.blocks)
    assert "doc_conversion_review_required" in codes(result)
    artifact = result.blocks[-1]["resources"][0]
    assert Path(artifact["path"]).exists() and zipfile.is_zipfile(artifact["path"])
    assert max(reservations) >= doc.MAX_CONVERTED_BYTES + doc.PROFILE_ALLOWANCE
    assert reservations[-1] == len(original) + artifact["bytes"]


def test_unavailable_converter_and_invalid_input_are_explicit(source, tmp_path, monkeypatch):
    monkeypatch.setattr(doc, "detect_converters", lambda _: [
        {"id": name, "available": name != "libreoffice"} for name in doc.CONVERTERS])
    # Auto must not silently start installed Word or WPS.
    result = doc.extract_doc(source, tmp_path / "resources")
    assert codes(result) == {"doc_converter_unavailable"} and not result.blocks
    source.write_bytes(b"not DOC or RTF")
    assert codes(doc.extract_doc(source, tmp_path / "resources")) == {"doc_format_invalid"}


@pytest.mark.parametrize("outcome", ["failed", "timeout", "cancelled", "limit", "isolation"])
def test_selected_failure_never_falls_back(source, adapter, monkeypatch, tmp_path, outcome):
    calls = []
    def fail(work, spec, cancel_check):
        calls.append(spec["converter"])
        return {"status": outcome}
    monkeypatch.setattr(doc, "_run_supervisor", fail)
    result = doc.extract_doc(source, tmp_path / "resources", priority=("libreoffice", "word", "wps"))
    assert calls == ["libreoffice"]
    assert codes(result) == {"doc_conversion_" + outcome}
    assert not result.blocks and not list((tmp_path / "resources").iterdir())


@pytest.mark.parametrize("bad_output", [None, b"", b"not an OOXML archive"])
def test_bad_converter_output_is_explained(source, adapter, monkeypatch, tmp_path, bad_output):
    def convert(work, spec, cancel_check):
        if bad_output is not None:
            Path(spec["output"]).write_bytes(bad_output)
        return {"status": "ok"}
    monkeypatch.setattr(doc, "_run_supervisor", convert)
    result = doc.extract_doc(source, tmp_path / "resources")
    assert codes(result) == {"doc_conversion_output"} and not result.blocks
    assert not list((tmp_path / "resources").iterdir())


def test_budget_refusal_prevents_converter_start(source, adapter, monkeypatch, tmp_path):
    monkeypatch.setattr(doc, "_run_supervisor", lambda *_: pytest.fail("converter must not start"))
    resources = tmp_path / "resources"
    def reserve(size):
        if size > 1024:
            raise ResourceBudgetExceeded("synthetic quota")
    with resource_budget(resources, reserve=reserve):
        with pytest.raises(ResourceBudgetExceeded):
            doc.extract_doc(source, resources)
    assert not list(resources.iterdir())


def test_source_change_during_conversion_is_rejected(source, adapter, monkeypatch, tmp_path):
    def convert(work, spec, cancel_check):
        make_docx(Path(spec["output"]))
        source.write_bytes(OLE + b"synthetic external change")
        return {"status": "ok"}
    monkeypatch.setattr(doc, "_run_supervisor", convert)
    result = doc.extract_doc(source, tmp_path / "resources")
    assert codes(result) == {"doc_source_changed"} and not result.blocks


def test_early_cancellation_never_starts_converter(source, adapter, monkeypatch, tmp_path):
    monkeypatch.setattr(doc, "_run_supervisor", lambda *_: pytest.fail("converter must not start"))
    result = extract(source, resource_dir=tmp_path / "resources", cancel_check=lambda: True)
    assert codes(result) == {"doc_conversion_cancelled"} and not result.blocks


def test_import_callback_checks_lease_while_converting(source, adapter, monkeypatch, tmp_path):
    queue = JobQueue(tmp_path / "library")
    sha = queue.project.put_bytes(source.read_bytes(), "source")
    job = queue.enqueue("import", {"asset_sha": sha, "name": source.name})
    calls = []

    def convert(work, spec, cancel_check):
        assert cancel_check() is False
        queue.cancel(job["id"])
        with pytest.raises(DomainError) as stale:
            cancel_check()
        calls.append(stale.value.code)
        raise stale.value

    monkeypatch.setattr(doc, "_run_supervisor", convert)
    assert queue.run_one()["state"] == "cancelled"
    assert calls == ["stale_job"]
    with queue.project.connect() as db:
        assert db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


def test_invalid_body_is_not_cached_as_success(source, adapter, monkeypatch, tmp_path):
    def convert(work, spec, cancel_check):
        with zipfile.ZipFile(spec["output"], "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
        return {"status": "ok"}
    monkeypatch.setattr(doc, "_run_supervisor", convert)
    resources = tmp_path / "resources"
    result = doc.extract_doc(source, resources)
    assert codes(result) == {"doc_conversion_output"} and not result.blocks
    assert not list(resources.iterdir())


def test_environment_path_and_read_only_detection(tmp_path, monkeypatch):
    executable = tmp_path / ("soffice.exe" if os.name == "nt" else "soffice")
    executable.write_text("synthetic", encoding="utf-8")
    monkeypatch.setenv("YUECANG_LIBREOFFICE", str(executable))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("detection must not launch"))
    detected = doc.detect_converters()
    assert detected[0]["available"] and not detected[0]["portable_included"]
    assert doc._libreoffice_path() == str(executable.resolve())
    assert not doc._libreoffice_path(tmp_path / "missing-converter")


def test_com_refuses_unowned_instance_and_disables_macros_before_open():
    script = doc._COM_SCRIPT
    assert script.index("$before -contains") < script.index("$owned = $true")
    assert script.index("AssignProcessToJobObject($jobHandle") < script.index("$app.Documents.Open")
    assert script.index("$app.AutomationSecurity = 3") < script.index("$app.Documents.Open")
    assert "if ($owned -and $app)" in script and "Stop-Process" not in script
    assert "if (-not $document.ReadOnly)" in script


def make_sleeper(tmp_path):
    script = tmp_path / "converter.py"
    # The descendant deliberately detaches, to exercise subreaper ownership.
    script.write_text('''import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                         start_new_session=sys.platform.startswith("linux"))
Path(sys.argv[1]).write_text(str(os.getpid()) + " " + str(child.pid))
time.sleep(120)
''')
    return script


def supervisor_spec(work, command, timeout=1):
    return {"work": str(work), "command": command, "timeout": timeout,
            "maximum": 16 * 1024**2, "job_name": "Local\\YueCangSynthetic-" + work.parent.name, "converter": "libreoffice"}


def wait_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


def process_ended(pid):
    if os.name == "nt":
        stamp = _process_stamp(pid)
        return stamp is None or not stamp["running"]
    return doc._linux_stamp(pid) is None


@pytest.mark.parametrize("mode", ["timeout", "cancel", "lease_loss"])
def test_owned_tree_drained_without_stopping_unrelated_process(tmp_path, mode):
    record = tmp_path / "owned-pids"
    script = make_sleeper(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    spec = supervisor_spec(work, [sys.executable, str(script), str(record)], timeout=1 if mode == "timeout" else 10)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    def cancelled():
        if record.exists() and mode == "lease_loss":
            raise RuntimeError("synthetic stale lease")
        return record.exists() and mode == "cancel"
    try:
        if mode == "lease_loss":
            with pytest.raises(RuntimeError, match="stale lease"):
                doc._run_supervisor(work, spec, cancelled)
        else:
            outcome = doc._run_supervisor(work, spec, cancelled)
            assert outcome["status"] == ("timeout" if mode == "timeout" else "cancelled")
        assert record.exists()
        pids = [int(value) for value in record.read_text().split()]
        wait_until(lambda: all(process_ended(pid) for pid in pids))
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_supervisor_follows_parent_death(tmp_path):
    record = tmp_path / "owned-pids"
    script = make_sleeper(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    spec = supervisor_spec(work, [sys.executable, str(script), str(record)], timeout=60)
    source_root = Path(doc.__file__).resolve().parents[2]
    caller = subprocess.Popen([sys.executable, "-c",
        "import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "from yuecang.formats.legacy_doc import _run_supervisor; "
        "_run_supervisor(Path(sys.argv[2]),json.loads(sys.argv[3]),None)",
        str(source_root), str(work), json.dumps(spec)])
    try:
        wait_until(record.exists)
        pids = [int(value) for value in record.read_text().split()]
        caller.kill()
        caller.wait(timeout=5)
        wait_until(lambda: all(process_ended(pid) for pid in pids))
    finally:
        if caller.poll() is None:
            caller.kill()
            caller.wait(timeout=5)


def _run_supervised_converter(project, job):
    project.reserve("synthetic-converter", 4096, job["id"] + ":" + job["token"])
    work = project.root / "work"
    work.mkdir()
    record = project.root / "owned-pids"
    command = [sys.executable, str(project.root / "converter.py"), str(record)]
    return doc._run_supervisor(work, supervisor_spec(work, command, timeout=60), None)


def _run_supervised_worker(root):
    run_isolated_one(JobQueue(root), callback=_run_supervised_converter)


@LINUX
def test_killed_coordinator_drains_detached_doc_tree_before_recovery(tmp_path):
    queue = JobQueue(tmp_path)
    make_sleeper(tmp_path)
    job = queue.enqueue("analyze", {})
    coordinator = multiprocessing.get_context("spawn").Process(target=_run_supervised_worker, args=(str(tmp_path),))
    coordinator.start()
    try:
        record = tmp_path / "owned-pids"
        wait_until(lambda: record.is_file() and record.stat().st_size, seconds=10)
        pids = [int(pid) for pid in record.read_text().split()]
        stamp = queue.get(job["id"])["checkpoint"]["task_process"]
        coordinator.kill()
        coordinator.join(5)
        wait_until(lambda: all(process_ended(pid) for pid in pids))
        wait_until(lambda: _attempt_stopped(stamp))
        with queue.project.transaction() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job["id"],))
        assert queue.recover_expired() == [job["id"]]
        with queue.project.connect() as db:
            assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
    finally:
        if coordinator.is_alive():
            coordinator.kill()
            coordinator.join(5)


def test_supervisor_limits_converter_output_and_drains_descendants(tmp_path):
    record = tmp_path / "owned-pids"
    script = make_sleeper(tmp_path)
    # Keep the process alive after crossing the bound; the monitor must stop it.
    script.write_text(script.read_text().replace("time.sleep(120)\n", "Path('oversize').write_bytes(b'x' * 65536)\ntime.sleep(120)\n"))
    work = tmp_path / "work"
    work.mkdir()
    spec = supervisor_spec(work, [sys.executable, str(script), str(record)], timeout=10)
    spec["maximum"] = 32768
    assert doc._run_supervisor(work, spec, None)["status"] == "limit"
    pids = [int(value) for value in record.read_text().split()]
    wait_until(lambda: all(process_ended(pid) for pid in pids))


def test_real_libreoffice_doc_conversion_and_cleanup(tmp_path):
    executable = doc._libreoffice_path()
    if not executable:
        pytest.skip("optional LibreOffice is not installed")
    seed = tmp_path / "seed"
    seed.mkdir()
    source_docx = make_docx(seed / "synthetic.docx")
    output = seed / "output"
    output.mkdir()
    profile = seed / "profile/user"
    profile.mkdir(parents=True)
    (profile / "registrymodifications.xcu").write_text(doc._PROFILE, encoding="utf-8")
    spec = supervisor_spec(seed, [executable, "-env:UserInstallation=" + profile.parent.as_uri(),
        "--headless", "--nologo", "--nodefault", "--norestore", "--convert-to", "doc:MS Word 97",
        "--outdir", str(output), str(source_docx)], timeout=30)
    spec["maximum"] = 128 * 1024**2
    assert doc._run_supervisor(seed, spec, None)["status"] == "ok"
    source = output / "synthetic.doc"
    assert source.read_bytes().startswith(OLE)
    original_hash = doc._hash_file(source)
    resources = tmp_path / "resources"
    with resource_budget(resources):
        result = doc.extract_doc(source, resources, timeout_seconds=30)
    assert not any(issue["severity"] == "error" for issue in result.issues), result.issues
    content = "\n".join(block["text"] for block in result.blocks)
    for expected in ("合成文档验收", "正文保持原样：天地玄黄，Unicode Ω。", "合成表格单元"):
        assert expected in content
    assert any(block["type"] == "table" for block in result.blocks), [(b["type"], b["text"], b["attributes"]) for b in result.blocks]
    assert doc._hash_file(source) == original_hash
    assert not list(resources.glob("doc-convert-*"))
    assert result.metadata["conversion"]["source_sha256"] == original_hash
    artifact = result.blocks[-1]["resources"][0]
    assert Path(artifact["path"]).read_bytes().startswith(b"PK")
    assert doc._hash_file(Path(artifact["path"])) == result.metadata["conversion"]["converted_sha256"]
