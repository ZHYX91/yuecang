from __future__ import annotations

from pathlib import Path

from .common import ExtractionResult, checked_archive, local_name, resolve_member, resource_bytes, xml_root

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _text(element) -> str:
    parts = []
    for node in element.iter():
        tag = local_name(node)
        if tag == "t":
            parts.append(node.text or "")
        elif tag == "tab":
            parts.append("\t")
        elif tag in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts)


def extract_docx(path: Path, resource_dir: Path | None) -> ExtractionResult:
    from lxml import etree
    result = ExtractionResult(path.stem, parser="docx-ooxml-v1", metadata={"source_format": "docx"})
    with checked_archive(path) as archive:
        names = set(archive.namelist())
        document = xml_root(archive.read("word/document.xml"))
        if "docProps/core.xml" in names:
            core = xml_root(archive.read("docProps/core.xml"))
            for key, tag in (("title", "title"), ("author", "creator"), ("language", "language")):
                values = core.xpath(f"//*[local-name()='{tag}']/text()")
                if values:
                    result.metadata[key] = values[0]
            result.title = result.metadata.get("title") or result.title
        relationships = {}
        if "word/_rels/document.xml.rels" in names:
            for rel in xml_root(archive.read("word/_rels/document.xml.rels")):
                relationships[rel.get("Id")] = (rel.get("Target", ""), rel.get("TargetMode", ""))
        used = set()

        def resources_for(element):
            resources = []
            for image in element.xpath(".//*[local-name()='blip' or local-name()='imagedata']"):
                rel_id = image.get(f"{{{R}}}embed") or image.get(f"{{{R}}}id")
                target, mode = relationships.get(rel_id, ("", ""))
                member = resolve_member("word/document.xml", target) if target and mode != "External" else None
                if not member or member not in names:
                    result.issue("docx_resource_missing", "图片关系缺失或是外部资源", relationship=rel_id)
                    continue
                resource = resource_bytes(archive.read(member), member, resource_dir)
                resources.append(resource)
                used.add(member)
            return resources

        body = document.find(f"{{{W}}}body")
        if body is None:
            result.issue("docx_body_missing", "DOCX 没有正文", severity="error")
            return result
        for index, element in enumerate(body):
            tag = local_name(element)
            locator = {"part": "word/document.xml", "body_index": index}
            text = _text(element)
            resources = resources_for(element)
            attrs = {}
            if tag == "p":
                styles = element.xpath(".//*[local-name()='pStyle']/@*[local-name()='val']")
                style = styles[0] if styles else ""
                level = int(style[-1]) if style.lower().startswith("heading") and style[-1:].isdigit() else None
                notes = element.xpath(".//*[local-name()='footnoteReference' or local-name()='endnoteReference']/@*[local-name()='id']")
                if notes:
                    attrs["note_references"] = notes
                formulas = element.xpath(".//*[local-name()='oMath']")
                for formula in formulas:
                    resources.append(resource_bytes(etree.tostring(formula), "formula.omml", resource_dir, mime="application/xml"))
                if formulas:
                    result.issue("docx_formula_preserved", "公式保留 OMML 原件，文本视图并非保真渲染", **locator)
                if element.xpath(".//*[local-name()='txbxContent' or local-name()='fldChar' or local-name()='instrText' or local-name()='del' or local-name()='ins']"):
                    attrs["xml"] = etree.tostring(element, encoding="unicode")
                    result.issue("docx_complex_paragraph", "文本框、域或修订保留原始 XML，请核对来源", **locator)
                result.add("heading" if level else ("figure" if not text and resources else "paragraph"), text, locator,
                           level=level, attributes=attrs, resources=resources)
            elif tag == "tbl":
                rows = [[_text(cell) for cell in row if local_name(cell) == "tc"] for row in element if local_name(row) == "tr"]
                attrs = {"rows": rows, "xml": etree.tostring(element, encoding="unicode")}
                result.add("table", text, locator, attributes=attrs, resources=resources)
                if element.xpath(".//*[local-name()='gridSpan' or local-name()='vMerge']"):
                    result.issue("table_spans_preserved", "合并单元格保留 XML，简化格式导出会降级", **locator)
            elif tag != "sectPr":
                result.add("unknown", text, locator, attributes={"xml": etree.tostring(element, encoding="unicode")}, resources=resources)
                result.issue("docx_unsupported_block", "未识别的正文结构保留 XML", **locator)
        for part in ("word/footnotes.xml", "word/endnotes.xml"):
            if part in names:
                for note in xml_root(archive.read(part)):
                    note_id = note.get(f"{{{W}}}id", "")
                    if note_id.startswith("-") or note.get(f"{{{W}}}type") in {"separator", "continuationSeparator"}:
                        continue
                    result.add("footnote", _text(note), {"part": part, "note_id": note_id},
                               attributes={"note_id": note_id, "xml": etree.tostring(note, encoding="unicode")})
                    if note.xpath(".//*[local-name()='blip' or local-name()='imagedata' or local-name()='oMath']"):
                        result.issue("docx_note_complex_content", "注释中的图形与公式保留 XML，图片在辅助资源中，需核对关系", part=part, note_id=note_id)
        extras = []
        for member in sorted(names):
            if ((member.startswith("word/media/") and member not in used) or member.startswith("word/embeddings/")
                    or (member.startswith("word/") and Path(member).name.startswith(("header", "footer", "comments")) and member.endswith(".xml"))):
                extras.append(resource_bytes(archive.read(member), member, resource_dir))
        if extras:
            result.add("unknown", "", {"role": "auxiliary_parts"}, resources=extras, attributes={"role": "attachment_inventory"})
            result.issue("docx_auxiliary_parts", "页眉页脚、批注、附件或未定位图片作为资源保留，未并入正文")
    return result
