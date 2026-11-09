from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .common import ExtractionError, ExtractionResult, MAX_MEMBER_BYTES, resource_bytes
from yuecang.core.encoding import decode, repair_text, text_issues


def decode_text(raw: bytes) -> tuple[str, str, bool]:
    try:
        return decode(raw)
    except ValueError as exc:
        raise ExtractionError(str(exc)) from exc


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


def extract_plain(path: Path, *, markdown: bool, resource_dir: Path | None, encoding="auto", repair="none") -> ExtractionResult:
    try:
        text, encoding, ambiguous = decode(path.read_bytes(), encoding)
        text, changes = repair_text(text, repair)
    except ValueError as exc:
        raise ExtractionError(str(exc)) from exc
    result = ExtractionResult(path.stem, metadata={"encoding": encoding, "source_format": "markdown" if markdown else "txt"},
                              parser="markdown-it-source-v1" if markdown else "plaintext-v1")
    result.metadata["text_recovery"] = {"repair": repair, "changes": changes, "explicit": repair != "none"}
    result.issues.extend(text_issues(text))
    if repair != "none":
        result.issue("derived_text", "此提取是用户确认的文字修复，位置按修复后的码点计算；原始文件保持不变")
    if ambiguous:
        result.issue("encoding_ambiguous", "多个编码可以解码；请核对预览并选择正确编码")
    if not text:
        result.issue("empty_document", "文件没有文本", severity="error")
        return result
    # markdown-it normalizes CRLF/CR to LF, but does not split on Python's
    # additional Unicode line separators. Keep its line maps in the original
    # codepoint space, including the original CRLF widths.
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", text) if markdown else text.splitlines(keepends=True)
    if lines and not lines[-1]:
        lines.pop()
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    if not markdown:
        from yuecang.core.parsing import chapter_heading_ordinal
        def emit(start, end, kind="paragraph"):
            # Bound matching units even when an entire book has no blank lines.
            while start < end:
                stop = min(end, start + 4096)
                result.add(kind, text[start:stop], {"start": start, "end": stop, "offset_space": "decoded_codepoints"})
                start = stop

        start = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            heading = bool(stripped and len(stripped) <= 120 and (
                re.fullmatch(r"《[^》\r\n]+》", stripped) or
                chapter_heading_ordinal(stripped) is not None or
                re.match(r"^(?:chapter|part|book)\s+(?:\d+|[IVXLCDM]+)\b", stripped, re.I)))
            if heading:
                emit(start, offsets[index])
                emit(offsets[index], offsets[index + 1], "heading")
                start = offsets[index + 1]
                continue
            if not line.strip() or index == len(lines) - 1:
                end = offsets[index + 1]
                emit(start, end)
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
