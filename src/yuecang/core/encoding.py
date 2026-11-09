"""Strict decoding and explicit, reproducible text recovery. Never guesses lost bytes."""
from __future__ import annotations

import codecs
import hashlib
import re
import unicodedata

ENCODINGS = {
    "auto": "自动识别（有歧义时请预览）", "utf-8": "UTF-8", "utf-8-sig": "UTF-8 BOM",
    "gb18030": "GB18030 / GBK", "big5": "Big5 繁体中文", "utf-16": "UTF-16 BOM",
    "utf-16-le": "UTF-16 LE", "utf-16-be": "UTF-16 BE", "utf-32": "UTF-32 BOM",
    "utf-32-le": "UTF-32 LE", "utf-32-be": "UTF-32 BE", "cp1252": "Windows-1252",
    "latin-1": "Latin-1",
}
REPAIRS = {
    "none": "不修复", "utf8-as-gb18030": "UTF-8 被误读为 GB18030",
    "utf8-as-latin1": "UTF-8 被误读为 Latin-1", "utf8-as-cp1252": "UTF-8 被误读为 Windows-1252",
    "nfc": "合并等价 Unicode 组合字符", "remove-invisible": "移除零宽空格和正文 BOM",
}
BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"), (codecs.BOM_UTF8, "utf-8-sig"))
MAX_RECOVERY_BYTES = 16 * 1024 * 1024


def validate_options(encoding="auto", repair="none"):
    if not isinstance(encoding, str) or encoding not in ENCODINGS:
        raise ValueError("不支持的编码，请从编码列表选择")
    if not isinstance(repair, str) or repair not in REPAIRS:
        raise ValueError("不支持的文字修复方式")


def _bomless(raw):
    # ASCII punctuation/newlines give a useful byte-order hint. Chinese-only short
    # files may remain ambiguous; explicit selection is deliberately available.
    sample = raw[:8192]
    if not sample:
        return None
    for width, encodings in ((4, ("utf-32-le", "utf-32-be")), (2, ("utf-16-le", "utf-16-be"))):
        if len(raw) % width:
            continue
        candidates = []
        for encoding in encodings:
            positions = range(1, width) if encoding.endswith("le") else range(width - 1)
            zero_ratio = sum(sample[i] == 0 for i in range(len(sample)) if i % width in positions) / max(1, len(sample) * (width - 1) / width)
            if zero_ratio < (0.6 if width == 4 else 0.25):
                continue
            try:
                text = raw.decode(encoding)
            except UnicodeError:
                continue
            if not any(ord(c) < 32 and c not in "\r\n\t" for c in text):
                candidates.append((text, encoding, zero_ratio))
        if candidates:
            text, encoding, _ = max(candidates, key=lambda candidate: candidate[2])
            return text, encoding
    return None


def decode(raw: bytes, encoding="auto"):
    validate_options(encoding)
    if encoding != "auto":
        return raw.decode(encoding, errors="strict"), encoding, False
    for bom, selected in BOMS:
        if raw.startswith(bom):
            return raw.decode(selected), selected, False
    candidate = _bomless(raw)
    if candidate:
        return *candidate, True
    try:
        return raw.decode("utf-8"), "utf-8", False
    except UnicodeError:
        pass
    candidates = []
    for selected in ("gb18030", "big5"):
        try:
            candidates.append((raw.decode(selected), selected))
        except UnicodeError:
            pass
    if not candidates:
        raise ValueError("无法可靠识别编码，请选择编码后重新预览")
    return *candidates[0], len({item[0] for item in candidates}) > 1


def repair_text(text: str, repair="none"):
    validate_options(repair=repair)
    if repair == "none":
        return text, 0
    if repair == "nfc":
        fixed = unicodedata.normalize("NFC", text)
        return fixed, int(fixed != text)
    if repair == "remove-invisible":
        return re.sub("[\u200b\ufeff]", "", text), text.count("\u200b") + text.count("\ufeff")
    encoding = {"utf8-as-gb18030": "gb18030", "utf8-as-latin1": "latin-1", "utf8-as-cp1252": "cp1252"}[repair]
    changes = 0

    def recover(match):
        nonlocal changes
        old = match.group()
        try:
            raw = old.encode(encoding, errors="strict")
            new = raw.decode("utf-8", errors="strict")
            if new.encode("utf-8").decode(encoding) != old or new == old:
                return old
            if any(ord(c) < 32 and c not in "\t\r\n" for c in new):
                return old
        except UnicodeError:
            return old
        changes += 1
        return new

    # Do not transcode whole mixed good/bad paragraphs. An exact round-trip is
    # required for every non-ASCII run; undecodable runs are preserved verbatim.
    if encoding == "gb18030":
        pattern = r"[^\x00-\x7f]+"
    elif encoding == "latin-1":
        pattern = r"[\x80-\xff]+"
    else:
        extended = bytes(range(0x80, 0xA0)).decode("cp1252", errors="ignore")
        pattern = rf"[\xa0-\xff{extended}]+"
    return re.sub(pattern, recover, text), changes


def text_issues(text):
    issues = []
    if "\ufffd" in text:
        issues.append({"code": "lost_encoding_characters", "message": "发现替换字符 �：原信息可能已丢失，不能靠转码恢复，请对照其他来源", "severity": "warning"})
    if "\x00" in text:
        issues.append({"code": "text_nul", "message": "发现空字符，请检查 UTF-16/UTF-32 编码或原文件", "severity": "warning"})
    if "\u200b" in text or "\ufeff" in text:
        issues.append({"code": "invisible_characters", "message": "正文含零宽空格或 BOM，可预览后选择移除", "severity": "warning"})
    return issues


def preview(raw, encoding="auto", repair="none"):
    validate_options(encoding, repair)
    if len(raw) > MAX_RECOVERY_BYTES:
        raise ValueError("编码修复单次上限为 16 MiB，请拆分文件后处理")
    original, selected, ambiguous = decode(raw, encoding)
    fixed, changes = repair_text(original, repair)
    issues = text_issues(fixed)
    if ambiguous:
        issues.append({"code": "encoding_ambiguous", "message": "自动识别仍需核对，请比较候选编码预览", "severity": "warning"})
    encoding_candidates = []
    for candidate in ENCODINGS:
        if candidate == "auto":
            continue
        try:
            text = raw.decode(candidate)
            encoding_candidates.append({"encoding": candidate, "preview": text[:2000]})
        except UnicodeError:
            pass
    repair_candidates = []
    for candidate, label in REPAIRS.items():
        if candidate == "none":
            continue
        text, count = repair_text(original, candidate)
        if count:
            repair_candidates.append({"id": candidate, "label": label, "preview": text[:2000], "changes": count})
    return {"encoding": selected, "repair": repair, "preview": fixed[:8000], "original_preview": original[:8000],
            "chars": len(fixed), "text_sha256": hashlib.sha256(fixed.encode()).hexdigest(), "changed": fixed != original, "changes": changes, "issues": issues,
            "encoding_candidates": encoding_candidates, "repair_candidates": repair_candidates, "truncated": len(fixed) > 8000}
