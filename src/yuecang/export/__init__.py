"""Deterministic revision export with explicit downgrade reports and manifests."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import zipfile
from pathlib import Path

from ..core.hashing import canonical_json, sha256_json

__all__ = ["export_revision", "ExportError", "verify_export"]


class ExportError(ValueError):
    pass


def safe_filename(title: str, identity: str, extension: str = ".md") -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title)).strip(" .") or "untitled"
    if value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    identity = re.sub(r"[^a-zA-Z0-9_-]", "_", str(identity))[:64]
    suffix = "__" + identity + extension
    while len((value + suffix).encode("utf-8")) > 240:
        value = value[:-1]
    return value + suffix


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _file_info(root: Path, path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
            size += len(part)
    return {"path": path.relative_to(root).as_posix(), "sha256": digest.hexdigest(), "bytes": size}


def _resources(blocks: list[dict], root: Path) -> tuple[dict[str, str], list[dict]]:
    mapped, rows = {}, []
    for block in blocks:
        for item in block.get("resources", []):
            source = Path(item.get("path", ""))
            if not str(item.get("path", "")) or not source.is_file() or source.is_symlink():
                raise ExportError("resource_missing_or_unsafe")
            digest_state = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest_state.update(chunk)
            digest = digest_state.hexdigest()
            if item.get("sha256") and item["sha256"] != digest:
                raise ExportError("resource_hash_mismatch")
            suffix = Path(item.get("name", source.name)).suffix.lower()[:12]
            if not suffix or not suffix[1:].isalnum():
                suffix = mimetypes.guess_extension(item.get("mime", "")) or ".bin"
            relative = f"resources/{digest}{suffix}"
            mapped[str(source)] = relative
            if item.get("original_reference"):
                mapped[item["original_reference"]] = relative
            if not (root / relative).exists():
                with source.open("rb") as incoming, (root / relative).open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                rows.append({"path": relative, "mime": item.get("mime", "application/octet-stream"), "name": item.get("name", source.name)})
    return mapped, rows


def _resource_links(block: dict, mapped: dict[str, str], *, markdown: bool) -> list[str]:
    links = []
    for resource in block.get("resources", []):
        target = mapped[str(Path(resource["path"]))]
        label = resource.get("alt") or resource.get("name") or "资源"
        if resource.get("role") == "pre_edit_source":
            label = "编辑前来源资源：" + str(label)
        label = str(label).replace("[", "\\[").replace("]", "\\]")
        if markdown:
            prefix = "!" if resource.get("mime", "").startswith("image/") and resource.get("mime") != "image/svg+xml" and resource.get("role") != "pre_edit_source" else ""
            links.append(f"{prefix}[{label}]({target})")
        else:
            links.append(f"[资源: {label} → {target}]")
    return links


def _markdown(revision: dict, mapped: dict[str, str], issues: list[dict]) -> bytes:
    metadata = {**revision.get("metadata", {}), "revision_id": str(revision["id"])}
    lines = ["---", *(f"{key}: {json.dumps(value, ensure_ascii=False, allow_nan=False)}" for key, value in sorted(metadata.items()) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)), "---", ""]
    for block in revision["blocks"]:
        text = block.get("text", "")
        attrs = block.get("attributes") or {}
        kind = block.get("type", "paragraph")
        if attrs.get("raw_markdown"):
            for original, target in mapped.items():
                text = text.replace("](" + original + ")", "](" + target + ")")
            lines.append(text)
        elif kind == "heading":
            lines.append("#" * min(6, max(1, int(block.get("level") or 1))) + " " + text)
        elif kind == "code":
            fence = "`" * max(3, max((len(x) for x in re.findall(r"`+", text)), default=0) + 1)
            lines.extend([fence, text, fence])
        elif kind == "table" and attrs.get("rows") and not attrs.get("structure_stale"):
            rows = attrs["rows"]
            width = max(len(row) for row in rows)
            formatted = ["| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", "<br>") for cell in list(row) + [""] * (width - len(row))) + " |" for row in rows]
            lines.extend([formatted[0], "| " + " | ".join(["---"] * width) + " |", *formatted[1:]])
            issues.append({"code": "table_markdown_downgrade", "block_id": block["id"], "message": "表格转为矩形 Markdown；复杂单元格结构保留在修订中"})
        else:
            lines.append(text)
            if kind in {"formula", "unknown", "footnote"}:
                issues.append({"code": "structured_block_downgrade", "block_id": block["id"], "message": "结构块以文本和原始资源导出"})
        if not attrs.get("raw_markdown"):
            lines.extend(_resource_links(block, mapped, markdown=True))
        if attrs.get("structure_stale"):
            issues.append({"code": "edited_structure_downgrade", "block_id": block["id"], "message": "结构块已编辑，导出采用当前文本；原结构和资源仅供来源核对"})
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def _txt(revision: dict, mapped: dict[str, str], issues: list[dict]) -> bytes:
    parts = []
    for block in revision["blocks"]:
        parts.append(str(block.get("text", "")))
        parts.extend(_resource_links(block, mapped, markdown=False))
        if block.get("type") not in {"paragraph", "heading", "code"} or block.get("resources") or (block.get("attributes") or {}).get("raw_markdown"):
            issues.append({"code": "txt_structure_downgrade", "block_id": block["id"], "message": "TXT 不保留结构或版式；资源随导出目录保留"})
    return "\n\n".join(parts).encode("utf-8")


def _xhtml(revision: dict, mapped: dict[str, str], issues: list[dict]) -> tuple[str, list[tuple[str, str]]]:
    output, headings = [], []
    for index, block in enumerate(revision["blocks"]):
        anchor = f"block-{index}"
        text = html.escape(str(block.get("text", "")))
        kind, attrs = block.get("type"), block.get("attributes") or {}
        if kind == "heading" and not attrs.get("raw_markdown"):
            level = min(6, max(1, int(block.get("level") or 1)))
            output.append(f'<h{level} id="{anchor}">{text}</h{level}>')
            headings.append((anchor, str(block.get("text", ""))))
        elif kind == "table" and attrs.get("rows") and not attrs.get("structure_stale"):
            output.append(f'<table id="{anchor}">' + "".join("<tr>" + "".join("<td>" + html.escape(str(cell)) + "</td>" for cell in row) + "</tr>" for row in attrs["rows"]) + "</table>")
            issues.append({"code": "table_epub_downgrade", "block_id": block["id"], "message": "复杂表格转为矩形表格"})
        elif kind == "code" or attrs.get("raw_markdown"):
            output.append(f'<pre id="{anchor}" style="white-space:pre-wrap">{text}</pre>')
            if attrs.get("raw_markdown"):
                issues.append({"code": "markdown_literal_epub", "block_id": block["id"], "message": "Markdown 扩展按字面保留，未执行内嵌 HTML"})
        else:
            output.append(f'<div id="{anchor}" style="white-space:pre-wrap">{text}</div>')
        for resource in block.get("resources", []):
            target = mapped[str(Path(resource["path"]))]
            label = html.escape(str(resource.get("name", "资源")))
            if resource.get("role") == "pre_edit_source":
                label = "编辑前来源资源：" + label
            mime = resource.get("mime", "")
            if mime.startswith("image/") and mime != "image/svg+xml" and resource.get("role") != "pre_edit_source":
                output.append(f'<img src="{html.escape(target, quote=True)}" alt="{label}" />')
            else:
                output.append(f'<a href="{html.escape(target, quote=True)}">{label}</a>')
                issues.append({"code": "epub_attachment", "block_id": block["id"], "message": "非标准阅读资源以附件保留"})
        if kind in {"formula", "unknown", "footnote"}:
            issues.append({"code": "epub_structured_block_downgrade", "block_id": block["id"], "message": "结构块文本与资源已保留，原有交互或排版未重建"})
        if attrs.get("structure_stale"):
            issues.append({"code": "edited_structure_downgrade", "block_id": block["id"], "message": "结构块已编辑，导出采用当前文本；原结构和资源仅供来源核对"})
    title = html.escape(str(revision.get("metadata", {}).get("title", "整理稿")))
    return f'<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head><body>{"".join(output)}</body></html>', headings


def _epub(revision: dict, root: Path, mapped: dict[str, str], resources: list[dict], issues: list[dict]) -> bytes:
    import io
    body, headings = _xhtml(revision, mapped, issues)
    metadata = revision.get("metadata", {})
    title = html.escape(str(metadata.get("title", "整理稿")))
    language = html.escape(str(metadata.get("language") or "und"))
    identifier = html.escape(str(revision["id"]))
    author = "<dc:creator>" + html.escape(str(metadata["author"])) + "</dc:creator>" if metadata.get("author") else ""
    navigation = "".join(f'<li><a href="content.xhtml#{anchor}">{html.escape(label)}</a></li>' for anchor, label in headings) or '<li><a href="content.xhtml">正文</a></li>'
    nav = f'<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>目录</title></head><body><nav epub:type="toc"><h1>目录</h1><ol>{navigation}</ol></nav></body></html>'
    resource_items = "".join(f'<item id="resource-{i}" href="{html.escape(row["path"], quote=True)}" media-type="{html.escape(row["mime"], quote=True)}"/>' for i, row in enumerate(resources))
    package = f'<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="revision"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="revision">{identifier}</dc:identifier><dc:title>{title}</dc:title><dc:language>{language}</dc:language>{author}<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta></metadata><manifest><item id="content" href="content.xhtml" media-type="application/xhtml+xml"/><item id="navigation" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{resource_items}</manifest><spine><itemref idref="content"/></spine></package>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        def put(name: str, data: bytes, stored=False):
            info = zipfile.ZipInfo(name, (2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
        put("mimetype", b"application/epub+zip", True)
        put("META-INF/container.xml", b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        put("OEBPS/package.opf", package.encode("utf-8"))
        put("OEBPS/content.xhtml", body.encode("utf-8"))
        put("OEBPS/nav.xhtml", nav.encode("utf-8"))
        for row in sorted(resources, key=lambda x: x["path"]):
            put("OEBPS/" + row["path"], (root / row["path"]).read_bytes())
    # Validate container membership and each generated XML document before publishing.
    from xml.etree import ElementTree
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        if archive.testzip() is not None:
            raise ExportError("epub_zip_integrity")
        for name in ("META-INF/container.xml", "OEBPS/package.opf", "OEBPS/content.xhtml", "OEBPS/nav.xhtml"):
            ElementTree.fromstring(archive.read(name))
    return buffer.getvalue()


def export_revision(revision: dict, destination: Path, format: str) -> dict:
    format = {"md": "markdown", "text": "txt"}.get(format.lower(), format.lower())
    if format not in {"markdown", "txt", "epub"}:
        raise ExportError("unsupported_export_format")
    if not revision.get("id") or not isinstance(revision.get("blocks"), list) or not revision["blocks"]:
        raise ExportError("revision_has_no_content")
    blocks = revision["blocks"]
    if len({str(b.get("id")) for b in blocks}) != len(blocks) or any(not b.get("id") for b in blocks):
        raise ExportError("revision_block_identity")
    destination = Path(destination)
    if any(p.is_symlink() for p in [destination, *destination.parents]):
        raise ExportError("unsafe_destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise ExportError("destination_exists") from exc
    # Exclusive directory reservation prevents overwrite races. A manifest is the
    # completion marker; interrupted directories are never reported as completed.
    try:
        (destination / "resources").mkdir()
        mapped, resources = _resources(blocks, destination)
        issues = list(revision.get("issues") or [])
        if format == "markdown":
            payload, filename = _markdown(revision, mapped, issues), "book.md"
        elif format == "txt":
            payload, filename = _txt(revision, mapped, issues), "book.txt"
        else:
            payload, filename = _epub(revision, destination, mapped, resources, issues), "book.epub"
        _write(destination / filename, payload)
        snapshot = copy.deepcopy(revision)
        for block in snapshot["blocks"]:
            for resource in block.get("resources", []):
                resource["path"] = mapped[str(Path(resource["path"]))]
        _write(destination / "revision.json", (canonical_json(snapshot) + "\n").encode("utf-8"))
        _write(destination / "issues.json", (canonical_json(issues) + "\n").encode("utf-8"))
        files = [_file_info(destination, p) for p in sorted(destination.rglob("*")) if p.is_file()]
        manifest = {"schema_version": 1, "revision_id": str(revision["id"]), "revision_sha256": sha256_json(revision),
                    "format": format, "renderer": "yuecang-export-v1", "files": files, "issues": issues, "complete": True}
        manifest["manifest_sha256"] = sha256_json(manifest)
        _write(destination / "manifest.json", (canonical_json(manifest) + "\n").encode("utf-8"))
        verify_export(destination)
        return {**manifest, "destination": str(destination.resolve()), "output_path": str((destination / filename).resolve()),
                "manifest_path": str((destination / "manifest.json").resolve())}
    except Exception:
        # Only remove the exact directory this invocation exclusively created.
        shutil.rmtree(destination)
        raise


def verify_export(destination: Path) -> dict:
    root = Path(destination)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    expected = manifest.get("manifest_sha256")
    if manifest.get("complete") is not True or expected != sha256_json({k: v for k, v in manifest.items() if k != "manifest_sha256"}):
        raise ExportError("manifest_hash_mismatch")
    declared = set()
    for row in manifest["files"]:
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"] or ":" in row["path"] or row["path"] in declared:
            raise ExportError("manifest_path_invalid")
        declared.add(row["path"])
        path = root / relative
        if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents):
            raise ExportError("export_file_missing_or_unsafe")
        if _file_info(root, path) != row:
            raise ExportError("export_file_hash_mismatch")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} - {"manifest.json"}
    if actual != declared:
        raise ExportError("export_file_set_mismatch")
    return manifest
