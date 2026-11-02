"""Conservative filename hints. These are never proof of book or edition identity."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import PurePath

_DIGITS = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_NUMBER = r"[0-9零〇一二两三四五六七八九十百千万]+"
_SEQUENCE = re.compile(rf"第\s*(?P<start>{_NUMBER})\s*(?P<unit>[章节回卷部篇集季])(?:\s*[-~～—至到]\s*(?:第)?\s*(?P<end>{_NUMBER})\s*[章节回卷部篇集季]?)?")


def parse_chinese_number(value: str) -> int | None:
    value = unicodedata.normalize("NFKC", value).strip()
    if not value:
        return None
    if value.isdecimal():
        return int(value)
    if any(c not in _DIGITS and c not in _UNITS for c in value):
        return None
    if not any(c in _UNITS for c in value):
        return int("".join(str(_DIGITS[c]) for c in value))
    total = section = digit = 0
    for char in value:
        if char in _DIGITS:
            digit = _DIGITS[char]
        elif _UNITS[char] == 10000:
            total += (section + digit) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * _UNITS[char]
            digit = 0
    return total + section + digit


def normalize_title_key(value: str) -> str:
    return "".join(c.casefold() for c in unicodedata.normalize("NFKC", value) if c.isalnum())


def chapter_heading_ordinal(value: str) -> int | None:
    match = re.match(rf"^\s*(?:第\s*)?({_NUMBER})\s*(?:[章节回卷部篇集]|[.、:：])", value)
    return parse_chinese_number(match[1]) if match else None


@dataclass(frozen=True)
class ParsedSourceName:
    stem: str
    canonical_title: str
    canonical_key: str
    version_key: str = ""
    ordinal_start: int | None = None
    ordinal_end: int | None = None
    marker: str = "none"
    terminal: bool = False
    confidence: float = 0.0
    issue_codes: tuple[str, ...] = ()

    @property
    def has_ordinal(self) -> bool:
        return self.ordinal_start is not None

    def as_dict(self) -> dict:
        return asdict(self)


def parse_source_name(basename: str) -> ParsedSourceName:
    stem = PurePath(str(basename).replace("\\", "/")).stem
    value = unicodedata.normalize("NFKC", stem).strip()
    title, start, end, marker = value, None, None, "none"
    issues: list[str] = []
    claims = list(_SEQUENCE.finditer(value))
    if claims:
        claim = claims[0]
        title = value[:claim.start()].strip(" _-—《》")
        start = parse_chinese_number(claim["start"])
        end = parse_chinese_number(claim["end"]) if claim["end"] else start
        marker = "chapter_range" if claim["end"] else "chapter_marker"
        if len(claims) > 1 or claim["unit"] in "卷部季":
            issues.append("chapter_hierarchy_requires_review")
        if value[claim.end():].strip(" _-—()"):
            issues.append("suffix_preserved_in_stem")
    else:
        match = re.fullmatch(rf"(.+?)[_\s-]+({_NUMBER})(?:[-~～至]({_NUMBER}))?", value)
        if match:
            title = match[1].strip("《》 ")
            start = parse_chinese_number(match[2])
            end = parse_chinese_number(match[3]) if match[3] else start
            marker = "numeric_suffix"
            issues.append("number_requires_review")
        elif re.fullmatch(rf"(?:第)?{_NUMBER}[章节回卷部篇集]?", value):
            title = ""
            issues.append("orphan_chapter_number")
        elif re.search(r"\(\d+\)$", value):
            issues.append("copy_suffix_ambiguity")
    if start is not None and end is not None and (start > end or end > 1_000_000):
        issues.append("invalid_or_implausible_range")
    if not title:
        issues.append("missing_family_title")
    return ParsedSourceName(stem, title, normalize_title_key(title), ordinal_start=start,
                            ordinal_end=end, marker=marker, confidence=0.8 if claims else 0.0,
                            issue_codes=tuple(issues))
