from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from yuecang.formats import extract
from yuecang.formats.common import ExtractionError, checked_archive, resolve_member


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, value.encode("utf-8") if isinstance(value, str) else value)
    return path


def _png():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(buffer, "PNG")
    return buffer.getvalue()


def _codes(result):
    return {row["code"] for row in result.issues}


@pytest.mark.parametrize("suffix", [".txt", ".md"])
def test_plain_source_exact_coverage_ids_and_unicode(tmp_path, suffix):
    text = "# 标题\r\n\r\n α😀 x+y=2  \r\n\t代码。\r\n\r\n结尾"
    source = tmp_path / ("book" + suffix)
    source.write_bytes(text.encode("utf-8"))
    result = extract(source, resource_dir=tmp_path / "resources")
    assert "".join(block["text"] for block in result.blocks) == text
    assert len({b["id"] for b in result.blocks}) == len(result.blocks)
    for block in result.blocks:
        assert text[block["locator"]["start"]:block["locator"]["end"]] == block["text"]
    again = extract(source, resource_dir=tmp_path / "resources")
    assert [b["id"] for b in result.blocks] == [b["id"] for b in again.blocks]


def test_markdown_table_formula_footnote_local_and_remote_resource(tmp_path):
    (tmp_path / "picture.png").write_bytes(_png())
    text = "# 数学\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n$E=mc^2$[^1]\n\n![local](picture.png) ![remote](https://invalid.example/p.png)\n\n[^1]: 公式的注释\n"
    source = tmp_path / "math.md"
    source.write_text(text, encoding="utf-8")
    result = extract(source, resource_dir=tmp_path / "resources")
    assert "".join(b["text"] for b in result.blocks) == text
    assert any(b["type"] == "table" for b in result.blocks)
    assert "external_resource_not_loaded" in _codes(result)
    assert "markdown_extension_preserved" in _codes(result)
    resources = [r for b in result.blocks for r in b["resources"]]
    assert len(resources) == 1 and Path(resources[0]["path"]).read_bytes() == _png()


def test_ambiguous_encoding_and_unknown_format_are_explicit(tmp_path):
    source = tmp_path / "gb18030.txt"
    source.write_bytes("中文说明".encode("gb18030"))
    result = extract(source)
    assert "中文说明" == "".join(b["text"] for b in result.blocks)
    assert result.metadata["encoding"] == "gb18030"
    source = tmp_path / "book.xyz"
    source.write_bytes(b"arbitrary")
    assert "unsupported_format" in _codes(extract(source))


def test_archive_traversal_and_xml_entity_are_rejected(tmp_path):
    source = _zip(tmp_path / "bad.docx", {"../escape": b"x", "word/document.xml": "<x/>"})
    with pytest.raises(ExtractionError):
        checked_archive(source)
    assert "extraction_failed" in _codes(extract(source))
    source = _zip(tmp_path / "entity.docx", {"word/document.xml": '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///secret">]><x>&e;</x>'})
    assert "extraction_failed" in _codes(extract(source))
    assert resolve_member("OPS/text/a.xhtml", "../images/p.png") == "OPS/images/p.png"
    assert resolve_member("OPS/text/a.xhtml", "https://invalid.example/a") is None
    with pytest.raises(ExtractionError):
        resolve_member("OPS/a.xhtml", "../../outside")


def make_epub(path):
    return _zip(path, {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
        "OPS/book.opf": '<package><metadata><title>合成教材</title><creator>测试作者</creator></metadata><manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/><item id="z" href="z.xhtml" media-type="application/xhtml+xml"/><item id="image" href="p.png" media-type="image/png"/></manifest><spine><itemref idref="z"/><itemref idref="a"/></spine></package>',
        "OPS/z.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>第一节</h1><p>精确 文本<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi><mo>+</mo><mn>1</mn></math><img src="p.png"/></p></body></html>',
        "OPS/a.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><h1>第二节</h1><aside epub:type="footnote" id="n1">注释</aside><script>never_run()</script><table><tr><td colspan="2">合并格</td></tr></table></body></html>',
        "OPS/p.png": _png(),
    })


def test_epub_spine_order_formula_footnote_and_resources(tmp_path):
    result = extract(make_epub(tmp_path / "book.epub"), resource_dir=tmp_path / "resources")
    assert result.title == "合成教材"
    assert [b["text"] for b in result.blocks if b["type"] == "heading"] == ["第一节", "第二节"]
    assert any(b["type"] == "footnote" and b["text"] == "注释" for b in result.blocks)
    resources = [r for b in result.blocks for r in b["resources"]]
    assert {r["mime"] for r in resources} >= {"image/png", "application/mathml+xml"}
    assert _codes(result) >= {"inline_formula_or_vector_preserved", "unsafe_or_unsupported_markup", "table_spans_preserved"}


def test_docx_preserves_order_table_notes_formula_and_image(tmp_path):
    document = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>标题</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>单元格</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t xml:space="preserve">正文 </w:t><w:tab/><w:t>后半</w:t><w:footnoteReference w:id="1"/></w:r><m:oMath><m:r><m:t>x+1</m:t></m:r></m:oMath><a:blip r:embed="rId1"/></w:p></w:body></w:document>'''
    source = _zip(tmp_path / "book.docx", {
        "word/document.xml": document,
        "word/_rels/document.xml.rels": '<Relationships><Relationship Id="rId1" Target="media/p.png"/></Relationships>',
        "word/media/p.png": _png(),
        "word/footnotes.xml": '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote w:id="1"><w:p><w:r><w:t>脚注原文</w:t></w:r></w:p></w:footnote></w:footnotes>',
    })
    result = extract(source, resource_dir=tmp_path / "resources")
    assert [b["type"] for b in result.blocks[:4]] == ["heading", "table", "paragraph", "footnote"]
    assert result.blocks[2]["text"] == "正文 \t后半x+1"
    assert result.blocks[2]["attributes"]["note_references"] == ["1"]
    assert result.blocks[3]["text"] == "脚注原文"
    assert "xml" in result.blocks[3]["attributes"]
    assert {r["mime"] for r in result.blocks[2]["resources"]} >= {"image/png", "application/xml"}
    assert "docx_formula_preserved" in _codes(result)


def test_text_pdf_and_scanned_pdf_are_distinguished(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    source = tmp_path / "text.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 10 100 Td (Synthetic textbook) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.add_blank_page(200, 200)
    writer.write(source)
    result = extract(source, resource_dir=tmp_path / "resources")
    assert any("Synthetic textbook" in b["text"] and b["locator"]["page"] == 1 for b in result.blocks)
    assert any(i["code"] == "ocr_required" and i["page"] == 2 for i in result.issues)
    assert "pdf_layout_requires_review" in _codes(result)
    assert any(r["mime"] == "application/pdf" for b in result.blocks for r in b["resources"])


def test_image_without_ocr_tool_retains_original_and_reports_unavailable(tmp_path):
    source = tmp_path / "scan.png"
    source.write_bytes(_png())
    with patch("yuecang.formats.pdf.shutil.which", return_value=None):
        result = extract(source, ocr=True, resource_dir=tmp_path / "resources")
    assert "ocr_unavailable" in _codes(result)
    assert result.blocks[0]["type"] == "figure"
    assert Path(result.blocks[0]["resources"][0]["path"]).read_bytes() == source.read_bytes()


def test_ocr_tsv_preserves_word_boxes_and_marks_derived_text(tmp_path):
    import subprocess
    source = tmp_path / "scan.png"
    source.write_bytes(_png())

    def run(args, **kwargs):
        assert "--list-langs" in args
        return subprocess.CompletedProcess(args, 0, b"languages (2):\nchi_sim\neng\n", b"")

    def ocr(executable, path, output, languages):
        assert path.is_relative_to(tmp_path / "resources")
        assert output.is_relative_to(tmp_path / "resources")
        output.write_text("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t2\t3\t4\t5\t95.2\tWord\n", encoding="utf-8")
        return 0

    with patch("yuecang.formats.pdf.shutil.which", return_value="tesseract"), patch("yuecang.formats.pdf.subprocess.run", side_effect=run), patch("yuecang.formats.pdf._ocr_to_file", side_effect=ocr):
        result = extract(source, ocr=True, resource_dir=tmp_path / "resources")
    paragraph = next(b for b in result.blocks if b["type"] == "paragraph")
    assert paragraph["text"] == "Word"
    assert paragraph["locator"]["word_boxes"] == [[2, 3, 4, 5]]
    assert paragraph["attributes"]["ocr"] is True
    assert "ocr_review_required" in _codes(result)
