import concurrent.futures
import hashlib
import json
import sqlite3

import pytest

from yuecang.application.service import Workbench
from yuecang.storage.project import Project, DomainError


@pytest.fixture
def library(tmp_path):
    wb = Workbench(tmp_path / "project")
    text = tmp_path / "source.txt"
    text.write_text("《城市植物手册》\n\n第一章 浇水\n\n幼苗每次浇水约 250 mL。\n\n第二章 光照\n\n叶片通过光和作用制造养分。", encoding="utf-8")
    source = wb.source(wb.import_file(text)["source_id"])
    book = wb.create_book("城市植物手册", author="合成作者")
    result = wb.assemble(book["id"], [{"source_id": source["id"], "block_id": b["id"]} for b in source["blocks"]])
    return wb, source, book, result


def test_full_manual_roundtrip_and_duplicate(library, tmp_path):
    wb, source, book, draft = library
    duplicate = wb.import_file(tmp_path / "source.txt")
    assert duplicate["duplicate"] and duplicate["source_id"] == source["id"]
    revision = wb.revision(draft["revision_id"])
    block = next(b for b in revision["blocks"] if "光和作用" in b["text"])
    start = block["text"].index("光和作用")
    edited = wb.edit(draft["branch_id"], revision["id"], [{"block_id": block["id"], "start": start, "end": start + 4, "text": "光合作用"}])
    wb.validate_revision(edited["revision"])
    assert "光和作用" in next(b for b in wb.revision(revision["id"])["blocks"] if b["id"] == block["id"])["text"]
    wb.finalize(edited["revision_id"])
    for format in ("markdown", "txt", "epub"):
        exported = wb.export(edited["revision_id"], format)
        assert exported["report"]["output_path"]
        assert wb.project.row("exports", exported["export_id"])["path"] == "exports/" + exported["export_id"]
    wb.select_revision(draft["branch_id"], edited["revision_id"], revision["id"])
    assert wb.project.row("branches", draft["branch_id"])["head_revision_id"] == revision["id"]


def test_concurrent_edit_is_compare_and_swap(library):
    wb, _, _, draft = library
    block = draft["revision"]["blocks"][-1]
    def edit(text):
        try:
            return wb.edit(draft["branch_id"], draft["revision_id"], [{"block_id": block["id"], "text": text}])
        except DomainError as exc:
            return exc.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["甲", "乙"]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "revision_conflict" in results


def test_mixed_source_can_feed_two_books(library):
    wb, source, _, _ = library
    second = wb.create_book("另一图书")
    result = wb.assemble(second["id"], [{"source_id": source["id"], "block_id": source["blocks"][-1]["id"], "start": 0, "end": 2}])
    assert result["revision"]["blocks"][0]["text"] == source["blocks"][-1]["text"][:2]
    assert result["revision"]["issues"]  # Cross-book reuse must remain visible.


def test_cross_edition_compilation_requires_explicit_choice(library):
    wb, source, book, _ = library
    edition = wb.create_edition(book["id"], "综合整理版")
    selections = [{"source_id": source["id"], "block_id": block["id"]} for block in source["blocks"]]
    blocked = wb.assemble(book["id"], selections, edition_id=edition["id"])
    with pytest.raises(DomainError, match="版本冲突"):
        wb.finalize(blocked["revision_id"])
    compiled = wb.assemble(book["id"], selections, edition_id=edition["id"], allow_composite=True)
    assert compiled["revision"]["metadata"]["assembly_policy"] == "composite-confirmed"
    assert compiled["revision"]["issues"] and all(i["severity"] == "warning" for i in compiled["revision"]["issues"])
    assert wb.finalize(compiled["revision_id"])["status"] == "final"
    wb.validate_revision(compiled["revision"])
    assert [b["text"] for b in wb.source(source["id"])["blocks"]] == [b["text"] for b in source["blocks"]]


def test_delete_and_astral_unicode_provenance(library):
    wb, _, _, draft = library
    block = draft["revision"]["blocks"][0]
    changed = wb.edit(draft["branch_id"], draft["revision_id"], [{"block_id": block["id"], "text": "甲😀乙"}])
    changed = wb.edit(draft["branch_id"], changed["revision_id"], [{"block_id": changed["revision"]["blocks"][0]["id"], "start": 1, "end": 2, "text": ""}])
    assert changed["revision"]["blocks"][0]["text"] == "甲乙"
    wb.validate_revision(changed["revision"])


def test_unknown_database_not_modified(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    database = root / "library.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE old(value)")
    before = database.read_bytes()
    with pytest.raises(DomainError, match="未识别"):
        Project(root)
    assert database.read_bytes() == before


def test_same_bytes_different_format_preserves_distinct_extraction(tmp_path):
    wb = Workbench(tmp_path / "project")
    plain, markdown = tmp_path / "book.txt", tmp_path / "book.md"
    plain.write_text("# 第一章\n\n正文内容。", encoding="utf-8")
    markdown.write_bytes(plain.read_bytes())
    a = wb.source(wb.import_file(plain)["source_id"])
    b = wb.source(wb.import_file(markdown)["source_id"])
    assert a["asset_sha"] == b["asset_sha"] and a["id"] != b["id"]
    assert a["parser"] != b["parser"]
    assert a["metadata"]["extractor"] != b["metadata"]["extractor"]
    assert wb.import_file(markdown)["duplicate"] is True


def test_budget_and_hash_failure(tmp_path):
    project = Project(tmp_path / "p")
    project.save_settings({"storage": {"temporary_bytes": 8, "minimum_free_bytes": 0}})
    project.reserve("a", 6, "first")
    with pytest.raises(DomainError, match="上限"):
        project.reserve("b", 3, "second")
    with pytest.raises(DomainError):
        project.put_bytes(b"123456789")
    assert not list((project.root / "tmp").iterdir())
    project.save_settings({"storage": {"temporary_bytes": 1024}})
    sha = project.put_json({"x": 1})
    project.object_path(sha).write_bytes(b"corrupted")
    with pytest.raises(DomainError, match="哈希"):
        project.read_json(sha)


def test_assignment_uses_default_edition_when_omitted(library):
    wb, source, book, _ = library
    result = wb.assign(book["id"], spans=[{"source_id": source["id"], "block_id": source["blocks"][0]["id"]}])
    assert result["assigned"] == 1


def test_structural_slice_rejected_and_edited_table_exports_current_text(tmp_path):
    from unittest.mock import patch
    from yuecang.formats import ExtractionResult
    wb = Workbench(tmp_path / "project")
    result = ExtractionResult("合成表格")
    result.add("table", "旧表文本", {"table": 1}, attributes={"rows": [["旧表文本"]], "xml": "<table>旧表文本</table>"})
    path = tmp_path / "table.docx"
    path.write_bytes(b"synthetic fixture; extractor is mocked")
    with patch("yuecang.formats.extract", return_value=result):
        source = wb.source(wb.import_file(path)["source_id"])
    book = wb.create_book("合成表格")
    selection = {"source_id": source["id"], "block_id": source["blocks"][0]["id"]}
    with pytest.raises(DomainError) as exc:
        wb.assemble(book["id"], [{**selection, "start": 0, "end": 1}])
    assert exc.value.code == "structured_span"
    draft = wb.assemble(book["id"], [selection])
    edited = wb.edit(draft["branch_id"], draft["revision_id"], [{"block_id": draft["revision"]["blocks"][0]["id"], "text": "新表文本"}])
    assert edited["revision"]["blocks"][0]["attributes"]["structure_stale"] is True
    wb.validate_revision(edited["revision"])
    report = wb.export(edited["revision_id"], allow_draft=True)["report"]
    from pathlib import Path
    rendered = Path(report["output_path"]).read_text(encoding="utf-8")
    assert "新表文本" in rendered and "旧表文本" not in rendered
    assert any(i["code"] == "edited_structure_downgrade" for i in report["issues"])


def test_revision_validator_rejects_unknown_and_unrelated_edit_provenance(library):
    import copy
    wb, _, _, draft = library
    block = draft["revision"]["blocks"][0]
    edited = wb.edit(draft["branch_id"], draft["revision_id"], [{"block_id": block["id"], "text": "校订文字"}])
    valid = edited["revision"]
    wb.validate_revision(valid)
    invalid = copy.deepcopy(valid)
    invalid["blocks"][0]["provenance"][0]["kind"] = "invented"
    with pytest.raises(DomainError, match="未知来源"):
        wb.validate_revision(invalid)
    invalid = copy.deepcopy(valid)
    invalid["blocks"][0]["text"] = "伪造文字"
    with pytest.raises(DomainError):
        wb.validate_revision(invalid)
    invalid = copy.deepcopy(valid)
    invalid["blocks"][1]["provenance"] = [{"kind": "edit", "operation_id": valid["blocks"][0]["provenance"][0]["operation_id"],
                                          "actor": "user", "output_start": 0, "output_end": len(invalid["blocks"][1]["text"])}]
    with pytest.raises(DomainError, match="编辑来源"):
        wb.validate_revision(invalid)


def test_unchanged_edited_block_remains_valid_in_later_revision(library):
    wb, _, _, draft = library
    first, second = draft["revision"]["blocks"][:2]
    changed = wb.edit(draft["branch_id"], draft["revision_id"], [{"block_id": first["id"], "text": "校订首块"}])
    changed = wb.edit(draft["branch_id"], changed["revision_id"], [{"block_id": second["id"], "text": "校订次块"}])
    wb.validate_revision(changed["revision"])


def test_export_job_reuses_complete_output_after_commit_gap(library):
    from yuecang.jobs import JobQueue
    wb, _, _, draft = library
    queue = JobQueue(wb.project)
    queue.enqueue("export", {"revision_id": draft["revision_id"], "allow_draft": True})
    job = queue.claim()
    first = wb.export(draft["revision_id"], allow_draft=True, job=job)
    assert first["export_id"] == job["id"]
    with wb.project.transaction() as db:
        db.execute("DELETE FROM exports WHERE id=?", (job["id"],))
    recovered = wb.export(draft["revision_id"], allow_draft=True, job=job)
    assert recovered["report"]["manifest_sha256"] == first["report"]["manifest_sha256"]
    repeated = wb.export(draft["revision_id"], allow_draft=True, job=job)
    assert repeated["duplicate"] is True
    assert len(list((wb.project.root / "exports").iterdir())) == 1


def test_export_job_recovers_partial_directory_without_completion_manifest(library):
    from yuecang.jobs import JobQueue
    from yuecang.export import verify_export
    wb, _, _, draft = library
    queue = JobQueue(wb.project)
    queue.enqueue("export", {"revision_id": draft["revision_id"], "allow_draft": True})
    job = queue.claim()
    partial = wb.project.root / "exports" / job["id"]
    partial.mkdir()
    (partial / "partial.tmp").write_bytes(b"incomplete")
    wb.export(draft["revision_id"], allow_draft=True, job=job)
    assert not (partial / "partial.tmp").exists()
    assert verify_export(partial)["complete"] is True
