import copy

import pytest

from yuecang.application.merge import edit_metadata, fork_revision, merge_branch
from yuecang.application.service import Workbench
from yuecang.storage.project import DomainError


@pytest.fixture
def branches(tmp_path):
    wb = Workbench(tmp_path / "project")
    path = tmp_path / "book.txt"
    path.write_text("甲乙丙丁戊己庚辛壬癸", encoding="utf-8")
    source = wb.source(wb.import_file(path)["source_id"])
    book = wb.create_book("合成版本")
    base = wb.assemble(book["id"], [{"source_id": source["id"], "block_id": source["blocks"][0]["id"]}])
    other = fork_revision(wb, base["revision_id"], "另一分支")
    return wb, base, other


def _edit(wb, branch, revision, start, end, text):
    block = wb.revision(revision)["blocks"][0]
    return wb.edit(branch, revision, [{"block_id": block["id"], "start": start, "end": end, "text": text}])


def test_nonoverlapping_changes_merge_and_keep_both_inputs(branches):
    wb, base, other = branches
    left = _edit(wb, base["branch_id"], base["revision_id"], 1, 2, "左")
    right = _edit(wb, other["id"], base["revision_id"], 7, 8, "右")
    before = copy.deepcopy(wb.revision(base["revision_id"]))
    merged = merge_branch(wb, base["branch_id"], left["revision_id"], right["revision_id"])
    assert merged["merged"] is True
    assert merged["revision"]["blocks"][0]["text"] == "甲左丙丁戊己庚右壬癸"
    wb.validate_revision(merged["revision"])
    operation = merged["revision"]["changes"][0]
    assert operation["actor"] == "merge-approved"
    assert {e["revision_id"] for e in operation["payload"]["evidence"]} == {base["revision_id"], left["revision_id"], right["revision_id"]}
    assert wb.revision(base["revision_id"])["blocks"] == before["blocks"]


@pytest.mark.parametrize("decision,expected", [("left", "甲左丙丁戊己庚辛壬癸"), ("right", "甲右丙丁戊己庚辛壬癸"), ({"choice": "custom", "text": "校订"}, "甲校订丙丁戊己庚辛壬癸")])
def test_conflict_is_read_only_until_explicit_resolution(branches, decision, expected):
    wb, base, other = branches
    left = _edit(wb, base["branch_id"], base["revision_id"], 1, 2, "左")
    right = _edit(wb, other["id"], base["revision_id"], 1, 2, "右")
    with wb.project.connect() as db:
        before = db.execute("SELECT count(*) FROM revisions").fetchone()[0]
    conflict = merge_branch(wb, base["branch_id"], left["revision_id"], right["revision_id"])
    assert conflict["merged"] is False
    assert conflict["conflicts"][0]["base"] == "乙"
    with wb.project.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == before
    resolved = merge_branch(wb, base["branch_id"], left["revision_id"], right["revision_id"], {conflict["conflicts"][0]["id"]: decision})
    assert resolved["merged"] is True
    assert wb.revision(resolved["revision_id"])["blocks"][0]["text"] == expected


def test_stale_head_other_book_and_stale_resolution_are_rejected(branches, tmp_path):
    wb, base, other = branches
    left = _edit(wb, base["branch_id"], base["revision_id"], 1, 2, "左")
    with pytest.raises(DomainError) as exc:
        merge_branch(wb, base["branch_id"], base["revision_id"], other["head_revision_id"])
    assert exc.value.code == "revision_conflict"
    another = wb.create_book("另一本")
    source = wb.state()["sources"][0]
    source = wb.source(source["id"])
    unrelated = wb.assemble(another["id"], [{"source_id": source["id"], "block_id": source["blocks"][0]["id"]}])
    with pytest.raises(DomainError) as exc:
        merge_branch(wb, base["branch_id"], left["revision_id"], unrelated["revision_id"])
    assert exc.value.code == "merge_edition"
    with pytest.raises(DomainError) as exc:
        merge_branch(wb, base["branch_id"], left["revision_id"], other["head_revision_id"], {"stale-id": "left"})
    assert exc.value.code == "merge_resolution_stale"


def test_metadata_changes_freeze_snapshot_and_reject_unresolved_merge(branches):
    wb, base, other = branches
    changed = edit_metadata(wb, base["branch_id"], base["revision_id"], {"title": "校订后的题名", "translator": "合成译者"})
    assert wb.revision(base["revision_id"])["metadata"]["title"] == "合成版本"
    assert changed["revision"]["metadata"]["title"] == "校订后的题名"
    assert changed["revision"]["document_sha"] == base["revision"]["document_sha"]
    wb.validate_revision(changed["revision"])
    result = merge_branch(wb, base["branch_id"], changed["revision_id"], other["head_revision_id"])
    assert result["unresolved"][0]["code"] == "metadata_difference"
    with pytest.raises(DomainError):
        edit_metadata(wb, base["branch_id"], base["revision_id"], {"author": "过期修改"})


def test_identical_insertions_are_not_duplicated(branches):
    wb, base, other = branches
    left = _edit(wb, base["branch_id"], base["revision_id"], 3, 3, "同添")
    right = _edit(wb, other["id"], base["revision_id"], 3, 3, "同添")
    result = merge_branch(wb, base["branch_id"], left["revision_id"], right["revision_id"])
    assert result["merged"] and result["unchanged"]
    assert wb.revision(result["revision_id"])["blocks"][0]["text"].count("同添") == 1
