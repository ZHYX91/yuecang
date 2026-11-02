from __future__ import annotations

import re
from pathlib import Path

from .common import ExtractionError, ExtractionResult, checked_archive, local_name, resolve_member, resource_bytes, xml_root


def _xhtml(data: bytes):
    # HTML5's empty doctype contains no entities. Other DTDs remain rejected.
    return xml_root(re.sub(br"<!DOCTYPE\s+html\s*>", b"", data, flags=re.I))


def extract_epub(path: Path, resource_dir: Path | None) -> ExtractionResult:
    from lxml import etree
    result = ExtractionResult(path.stem, parser="epub-spine-v1", metadata={"source_format": "epub"})
    with checked_archive(path) as archive:
        names = set(archive.namelist())
        if "META-INF/encryption.xml" in names:
            result.issue("epub_encrypted_resources", "EPUB 声明加密或混淆资源，未解密；请核对缺失资源")
        container = xml_root(archive.read("META-INF/container.xml"))
        roots = container.xpath("//*[local-name()='rootfile']/@full-path")
        if not roots:
            raise ExtractionError("epub_package_missing")
        package_path = resolve_member("container.xml", roots[0])
        package = xml_root(archive.read(package_path))
        titles = package.xpath("//*[local-name()='metadata']/*[local-name()='title']/text()")
        creators = package.xpath("//*[local-name()='metadata']/*[local-name()='creator']/text()")
        languages = package.xpath("//*[local-name()='metadata']/*[local-name()='language']/text()")
        result.title = titles[0] if titles else path.stem
        result.metadata.update({"author": "; ".join(creators), "language": languages[0] if languages else ""})
        manifest = {}
        for item in package.xpath("//*[local-name()='manifest']/*[local-name()='item']"):
            manifest[item.get("id")] = {"path": resolve_member(package_path, item.get("href", "")),
                                          "mime": item.get("media-type", ""), "properties": item.get("properties", "")}
        spine = package.xpath("//*[local-name()='spine']/*[local-name()='itemref']")
        if not spine:
            raise ExtractionError("epub_spine_empty")
        used_members = set()
        for spine_index, itemref in enumerate(spine):
            item = manifest.get(itemref.get("idref"))
            if not item or not item["path"] or item["path"] not in names:
                result.issue("epub_spine_resource_missing", "阅读顺序中的资源缺失", severity="error", spine=spine_index)
                continue
            member = item["path"]
            used_members.add(member)
            if item["mime"] not in {"application/xhtml+xml", "text/html"}:
                resource = resource_bytes(archive.read(member), member, resource_dir, mime=item["mime"])
                result.add("unknown", "", {"member": member, "spine": spine_index}, resources=[resource])
                result.issue("epub_non_xhtml_spine", "非 XHTML 阅读资源保留原件", member=member)
                continue
            document = _xhtml(archive.read(member))
            bodies = document.xpath("//*[local-name()='body']")
            if not bodies:
                result.issue("epub_body_missing", "章节缺少 body", severity="error", member=member)
                continue
            counter = 0

            def emit(element):
                nonlocal counter
                tag = local_name(element).lower()
                locator = {"member": member, "spine": spine_index, "element": counter,
                           "element_id": element.get("id", "")}
                counter += 1
                text = "".join(element.itertext())
                attrs = {"source_tag": tag, "linear": itemref.get("linear", "yes") != "no"}
                resources = []
                for image in element.xpath(".//*[local-name()='img' or local-name()='image']") + ([element] if tag in {"img", "image"} else []):
                    href = image.get("src") or image.get("{http://www.w3.org/1999/xlink}href") or ""
                    image_member = resolve_member(member, href)
                    if not image_member:
                        result.issue("external_resource_not_loaded", "外部图片未下载", reference=href, member=member)
                    elif image_member not in names:
                        result.issue("resource_missing", "EPUB 图片资源缺失", reference=href, member=member)
                    else:
                        resource = resource_bytes(archive.read(image_member), image_member, resource_dir)
                        resource["original_reference"] = href
                        resource["alt"] = image.get("alt", "")
                        resources.append(resource)
                        used_members.add(image_member)
                references = element.xpath(".//*[local-name()='a']/@href")
                if references:
                    attrs["links"] = references
                if tag in {"script", "style", "iframe", "object", "embed"}:
                    result.add("unknown", text, locator, attributes={**attrs, "xml": etree.tostring(element, encoding="unicode")})
                    result.issue("unsafe_or_unsupported_markup", "脚本或嵌入对象未执行，保留原始标记", member=member, element=locator["element"])
                elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    result.add("heading", text, locator, level=int(tag[1]), attributes=attrs, resources=resources)
                elif tag == "table":
                    attrs["rows"] = [["".join(c.itertext()) for c in row if local_name(c) in {"th", "td"}]
                                     for row in element.xpath(".//*[local-name()='tr']")]
                    attrs["xml"] = etree.tostring(element, encoding="unicode")
                    result.add("table", text, locator, attributes=attrs, resources=resources)
                    if element.xpath(".//*[@rowspan or @colspan]"):
                        result.issue("table_spans_preserved", "合并单元格保留 XML，简化格式导出会降级", member=member)
                elif tag in {"math", "svg"}:
                    resource = resource_bytes(etree.tostring(element), "formula.mathml" if tag == "math" else "drawing.svg", resource_dir,
                                              mime="application/mathml+xml" if tag == "math" else "image/svg+xml")
                    result.add("formula" if tag == "math" else "figure", text, locator, attributes=attrs, resources=[resource])
                    result.issue("vector_or_formula_resource", "公式或矢量图保留原始资源，纯文本导出会降级", member=member)
                elif tag in {"p", "pre", "li", "blockquote", "figure", "img", "image", "aside"}:
                    # Keep nested math/graphics and semantics which itertext alone
                    # cannot represent, including note links and nested lists.
                    attrs["xml"] = etree.tostring(element, encoding="unicode")
                    for nested in element.xpath(".//*[local-name()='math' or local-name()='svg']"):
                        math = local_name(nested) == "math"
                        resources.append(resource_bytes(etree.tostring(nested), "formula.mathml" if math else "drawing.svg", resource_dir,
                                                        mime="application/mathml+xml" if math else "image/svg+xml"))
                        result.issue("inline_formula_or_vector_preserved", "行内公式或矢量图保留 XML 资源，文本视图需核对", member=member)
                    if element.xpath(".//*[local-name()='script' or local-name()='object' or local-name()='iframe']"):
                        result.issue("unsafe_or_unsupported_markup", "嵌入脚本或对象只保留 XML，不执行", member=member)
                    epub_type = element.get("{http://www.idpf.org/2007/ops}type", "")
                    kind = "footnote" if "footnote" in epub_type or "endnote" in epub_type else ("code" if tag == "pre" else ("figure" if tag in {"figure", "img", "image"} else "paragraph"))
                    result.add(kind, text, locator, attributes=attrs, resources=resources)
                else:
                    if element.text:
                        result.add("paragraph", element.text, locator, attributes=attrs)
                    for child in element:
                        if isinstance(child.tag, str):
                            emit(child)
                        if child.tail:
                            result.add("paragraph", child.tail, {**locator, "tail_of": child.get("id", local_name(child))}, attributes=attrs)

            emit(bodies[0])
        # Inventory resources not represented in the spine: no silent resource loss.
        extras = []
        for item in manifest.values():
            member = item["path"]
            if member and member in names and member not in used_members:
                resource = resource_bytes(archive.read(member), member, resource_dir, mime=item["mime"])
                resource["member"] = member
                extras.append(resource)
        if extras:
            result.add("unknown", "", {"role": "non_spine_resources"}, resources=extras,
                       attributes={"role": "attachment_inventory"})
            result.issue("epub_auxiliary_resources", "封面、导航、样式等非正文资源已保留，样式未自动应用")
    return result
