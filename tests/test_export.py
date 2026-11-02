from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from yuecang.core.hashing import sha256_bytes, sha256_json
from yuecang.export import ExportError, export_revision, safe_filename, verify_export
from yuecang.formats import extract


def _revision(tmp_path):
    resource = tmp_path / "equation.mathml"
    resource.write_text('<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>', encoding="utf-8")
    return {"id": "revision-1", "metadata": {"title": "教材 <一>", "author": "合成作者", "language": "zh"},
            "blocks": [
                {"id": "h", "type": "heading", "level": 1, "text": "第一章", "attributes": {}, "resources": []},
                {"id": "p", "type": "paragraph", "text": "逐字 <script>alert(1)</script> α😀 文本。", "attributes": {}, "resources": [],
                 "provenance": [{"source_block_id": "source-1", "source_start": 0, "source_end": 2, "output_start": 0, "output_end": 2}]},
                {"id": "t", "type": "table", "text": "AB12", "attributes": {"rows": [["A", "B"], ["1", "2"]]}, "resources": []},
                {"id": "m", "type": "formula", "text": "x", "attributes": {"original_xml": "<math/>"},
                 "resources": [{"path": str(resource), "name": "equation.mathml", "mime": "application/mathml+xml", "sha256": sha256_bytes(resource.read_bytes())}]},
            ]}


@pytest.mark.parametrize("format,filename", [("markdown", "book.md"), ("txt", "book.txt"), ("epub", "book.epub")])
def test_export_keeps_resources_revision_provenance_and_manifest(tmp_path, format, filename):
    revision = _revision(tmp_path)
    before = copy.deepcopy(revision)
    destination = tmp_path / format
    result = export_revision(revision, destination, format)
    assert revision == before
    assert Path(result["output_path"]).name == filename
    manifest = verify_export(destination)
    assert manifest["revision_sha256"] == sha256_json(revision)
    assert manifest["complete"] is True
    assert manifest["issues"]
    snapshot = json.loads((destination / "revision.json").read_text(encoding="utf-8"))
    assert snapshot["blocks"][1]["provenance"] == revision["blocks"][1]["provenance"]
    saved = snapshot["blocks"][-1]["resources"][0]
    assert not Path(saved["path"]).is_absolute()
    assert (destination / saved["path"]).read_bytes() == Path(revision["blocks"][-1]["resources"][0]["path"]).read_bytes()
    if format == "epub":
        with zipfile.ZipFile(destination / filename) as archive:
            assert archive.infolist()[0].filename == "mimetype"
            assert archive.infolist()[0].compress_type == zipfile.ZIP_STORED
            assert archive.read("mimetype") == b"application/epub+zip"
            for member in ("OEBPS/package.opf", "OEBPS/nav.xhtml", "OEBPS/content.xhtml"):
                ElementTree.fromstring(archive.read(member))
            xhtml = archive.read("OEBPS/content.xhtml").decode("utf-8")
            assert "<script>" not in xhtml and "&lt;script&gt;" in xhtml
            assert saved["path"] in xhtml
        parsed = extract(destination / filename, resource_dir=tmp_path / "parsed")
        assert parsed.title == revision["metadata"]["title"]
        assert any("逐字" in b["text"] for b in parsed.blocks)


def test_export_is_deterministic_and_never_overwrites(tmp_path):
    revision = _revision(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    a = export_revision(revision, first, "epub")
    b = export_revision(revision, second, "epub")
    assert (first / "book.epub").read_bytes() == (second / "book.epub").read_bytes()
    assert a["manifest_sha256"] == b["manifest_sha256"]
    original = (first / "manifest.json").read_bytes()
    with pytest.raises(ExportError, match="destination_exists"):
        export_revision(revision, first, "txt")
    assert (first / "manifest.json").read_bytes() == original


def test_export_detects_resource_changes_and_rolls_back_its_own_directory(tmp_path):
    revision = _revision(tmp_path)
    Path(revision["blocks"][-1]["resources"][0]["path"]).write_bytes(b"changed")
    destination = tmp_path / "new-export"
    with pytest.raises(ExportError, match="resource_hash_mismatch"):
        export_revision(revision, destination, "markdown")
    assert not destination.exists()
    assert (tmp_path / "equation.mathml").read_bytes() == b"changed"


def test_export_verification_detects_tamper_and_extra_files(tmp_path):
    revision = _revision(tmp_path)
    destination = tmp_path / "new-export"
    export_revision(revision, destination, "txt")
    original = (destination / "book.txt").read_bytes()
    (destination / "book.txt").write_bytes(b"modified")
    with pytest.raises(ExportError, match="export_file_hash_mismatch"):
        verify_export(destination)
    (destination / "book.txt").write_bytes(original)
    (destination / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(ExportError, match="export_file_set_mismatch"):
        verify_export(destination)


def test_safe_names_reserved_and_unicode_lengths():
    assert safe_filename("CON", "id").startswith("_CON")
    assert "/" not in safe_filename("a/b:c", "id")
    assert len(safe_filename("中" * 200, "id").encode("utf-8")) <= 240


def test_missing_content_and_duplicate_block_id_do_not_create_output(tmp_path):
    destination = tmp_path / "export"
    with pytest.raises(ExportError, match="revision_has_no_content"):
        export_revision({"id": "r", "blocks": []}, destination, "txt")
    revision = _revision(tmp_path)
    revision["blocks"][1]["id"] = "h"
    with pytest.raises(ExportError, match="revision_block_identity"):
        export_revision(revision, destination, "txt")
    assert not destination.exists()
