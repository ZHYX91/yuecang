from __future__ import annotations

import codecs
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .common import ExtractionError, ExtractionResult, MAX_MEMBER_BYTES, resource_bytes


def decode_text(raw: bytes) -> tuple[str, str, bool]:
    for bom, encoding in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                          (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
                          (codecs.BOM_UTF8, "utf-8-sig")):
        if raw.startswith(bom):
            return raw.decode(encoding), encoding, False
    try:
        return raw.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        candidates = []
        for encoding in ("gb18030", "big5"):
            try:
                candidates.append((raw.decode(encoding), encoding))
            except UnicodeDecodeError:
                pass
        if not candidates:
            raise ExtractionError("text_encoding_unrecognized")
        return candidates[0][0], candidates[0][1], len({x[0] for x in candidates}) > 1


def _local_images(path: Path, tokens, directory: Path | None, result: ExtractionResult) -> list[dict]:
    images = []
    for token in tokens:
        if token.type == "image":
            href = token.attrGet("src") or ""
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc:
                result.issue("external_resource_not_loaded", "外部资源保留引用，未联网下载", reference=href)
                continue
            relative = Path(unquote(parsed.path))
            candidate = (path.parent / relative).resolve()
            if relative.is_absolute() or not candidate.is_relative_to(path.parent.resolve()):
                result.issue("resource_outside_import_directory", "资源位于导入目录之外", reference=href)
                continue
            if not candidate.is_file() or candidate.stat().st_size > MAX_MEMBER_BYTES:
                result.issue("resource_missing", "资源缺失或超过大小上限", reference=href)
                continue
            resource = resource_bytes(candidate.read_bytes(), candidate.name, directory)
            resource["original_reference"] = href
            images.append(resource)
        if token.children:
            images.extend(_local_images(path, token.children, directory, result))
    return images


def extract_plain(path: Path, *, markdown: bool, resource_dir: Path | None) -> ExtractionResult:
    text, encoding, ambiguous = decode_text(path.read_bytes())
    result = ExtractionResult(path.stem, metadata={"encoding": encoding, "source_format": "markdown" if markdown else "txt"},
                              parser="markdown-it-source-v1" if markdown else "plaintext-v1")
    if ambiguous:
        result.issue("encoding_ambiguous", "多个编码可以解码；请核对预览并选择正确编码")
    if not text:
        result.issue("empty_document", "文件没有文本", severity="error")
        return result
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    if not markdown:
        start = 0
        for index, line in enumerate(lines):
            if not line.strip() or index == len(lines) - 1:
                end = offsets[index + 1]
                chunk = text[start:end]
                if chunk:
                    result.add("paragraph", chunk, {"start": start, "end": end, "offset_space": "decoded_codepoints"})
                start = end
        return result
    from markdown_it import MarkdownIt
    parser = MarkdownIt("commonmark").enable("table")
    tokens = parser.parse(text)
    spans = []
    for token in tokens:
        if token.level == 0 and token.map and token.nesting >= 0:
            a, b = token.map
            if spans and a < spans[-1][1]:
                continue
            kind = {"heading_open": "heading", "table_open": "table", "fence": "code", "code_block": "code"}.get(token.type, "paragraph")
            spans.append((a, b, kind, int(token.tag[1:]) if kind == "heading" else None))
    if not spans:
        spans = [(0, len(lines), "paragraph", None)]
    cursor = 0
    for a, b, kind, level in spans:
        start, end = offsets[a], offsets[b]
        if start > cursor:
            result.add("paragraph", text[cursor:start], {"start": cursor, "end": start, "offset_space": "decoded_codepoints"}, attributes={"raw_markdown": True})
        selected = [t for t in tokens if t.map and a <= t.map[0] < b]
        resources = _local_images(path, selected, resource_dir, result)
        result.add(kind, text[start:end], {"start": start, "end": end, "offset_space": "decoded_codepoints"},
                   level=level, attributes={"raw_markdown": True}, resources=resources)
        cursor = end
    if cursor < len(text):
        result.add("paragraph", text[cursor:], {"start": cursor, "end": len(text), "offset_space": "decoded_codepoints"}, attributes={"raw_markdown": True})
    if re.search(r"^\[\^[^\]]+\]:", text, re.M):
        result.issue("markdown_extension_preserved", "脚注扩展以原始 Markdown 保留，导出 EPUB 时按文本保留")
    if any(t.type in {"html_block", "html_inline"} for t in tokens):
        result.issue("markdown_html_preserved", "内嵌 HTML 按原文保留，预览和 EPUB 导出不执行它")
    return result
