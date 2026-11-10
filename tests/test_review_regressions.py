"""Synthetic regressions found during the 0.2 delivery review."""
import pytest

from yuecang.formats import extract
from yuecang.application.service import Workbench
from yuecang.storage.project import DomainError


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029", "\v", "\f"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_markdown_line_maps_preserve_unicode_separators(tmp_path, separator, newline):
    text = f"前言{separator}内容{newline}# 合成题名{newline}{newline}结尾{newline}"
    path = tmp_path / "synthetic.md"
    path.write_bytes(text.encode("utf-8"))
    result = extract(path, resource_dir=tmp_path / "resources")
    assert "".join(block["text"] for block in result.blocks) == text
    headings = [block for block in result.blocks if block["type"] == "heading"]
    assert len(headings) == 1
    assert headings[0]["text"] == f"# 合成题名{newline}"
    for block in result.blocks:
        location = block["locator"]
        assert text[location["start"]:location["end"]] == block["text"]


def test_recovery_checks_the_bytes_actually_ingested(tmp_path, monkeypatch):
    wb = Workbench(tmp_path / "library")
    path = tmp_path / "synthetic.txt"
    path.write_bytes("合成原件".encode())
    source = wb.source(wb.import_file(path)["source_id"])
    original_hash = wb.project.hash_file

    def changed_after_check(target):
        sha = original_hash(target)
        path.write_bytes("已经变化的输入".encode())
        return sha

    monkeypatch.setattr(wb.project, "hash_file", changed_after_check)
    with pytest.raises(DomainError, match="预览"):
        wb.import_file(path, encoding="utf-8", derived_from=source["id"],
                       expected_blocks_sha=source["blocks_sha"])
    assert len(wb.state()["sources"]) == 1
    assert wb.source(source["id"])["asset_sha"] == source["asset_sha"]
