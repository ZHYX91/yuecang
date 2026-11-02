"""Deterministic, plaintext-free fingerprints for immutable source text."""

from __future__ import annotations

import bisect
import hashlib
import re
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from .hashing import sha256_json, sha256_text
from .parsing import parse_chinese_number


SCHEMA_VERSION = 1
NORMALIZATION_VERSION = "nfkc-layout-v1"
CHUNKER_VERSION = "cdc-char-2048-4096-8192-v1"
FUZZY_VERSION = "rolling-7gram-bottom128-v1"

_INLINE_SPACE_RE = re.compile(r"[^\S\r\n]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:第\s*[0-9零〇一二两三四五六七八九十百千万]+\s*[章节回卷部篇集]|"
    r"[0-9零〇一二两三四五六七八九十百千万]+\s*[.、:：]|"
    r"序章|序言|前言|楔子|引子|后记|尾声|终章|番外(?:\s*[0-9零〇一二两三四五六七八九十百千万]+)?)"
    r"(?:[\s:：、._-].*)?$",
    re.IGNORECASE,
)
_ORDINAL_HEADING_RE = re.compile(
    r"^\s*(?:第\s*)?(?P<number>[0-9零〇一二两三四五六七八九十百千万]+)\s*"
    r"(?:[章节回卷部篇集]|[.、:：])(?:\s*.*)?$",
    re.IGNORECASE,
)
_BOOK_TITLE_RE = re.compile(r"^\s*《[^》\r\n]{1,100}》\s*$")
_TERMINAL_LINE_RE = re.compile(r"^\s*(?:全文)?(?:完结?|终|大结局)\s*$")


@dataclass(frozen=True)
class FingerprintComparison:
    relation: str
    exact_body: bool
    same_layout: bool
    same_lexical: bool
    chunk_overlap: float
    left_contained: float
    right_contained: float
    order_score: float
    fuzzy_jaccard: float

    def as_dict(self) -> dict[str, object]:
        return {
            "relation": self.relation,
            "exact_body": self.exact_body,
            "same_layout": self.same_layout,
            "same_lexical": self.same_lexical,
            "chunk_overlap": self.chunk_overlap,
            "left_contained": self.left_contained,
            "right_contained": self.right_contained,
            "order_score": self.order_score,
            "fuzzy_jaccard": self.fuzzy_jaccard,
        }


def normalize_layout(text: str) -> str:
    value = unicodedata.normalize("NFKC", text)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_INLINE_SPACE_RE.sub(" ", line).strip() for line in value.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def normalize_lexical(text: str) -> str:
    return "".join(char.casefold() for char in normalize_layout(text) if char.isalnum())


def _chunk_hashes(value: str) -> list[str]:
    if not value:
        return []
    minimum, maximum, mask = 2048, 8192, 4095
    window = 64
    modulus_mask = 0xFFFFFFFFFFFFFFFF
    base = 257
    outgoing_factor = pow(base, window, 1 << 64)
    start = 0
    rolling = 0
    recent: deque[int] = deque()
    rows: list[str] = []
    for position, char in enumerate(value, start=1):
        token = ord(char) + 1
        outgoing = recent.popleft() if len(recent) == window else None
        recent.append(token)
        rolling = (rolling * base + token) & modulus_mask
        if outgoing is not None:
            rolling = (rolling - outgoing * outgoing_factor) & modulus_mask
        size = position - start
        if size >= minimum and ((rolling & mask) == 0 or size >= maximum):
            rows.append(sha256_text(value[start:position]))
            start = position
    if start < len(value):
        rows.append(sha256_text(value[start:]))
    return rows


def _fuzzy_signature(value: str, *, width: int = 7, size: int = 128) -> list[str]:
    if not value:
        return []
    if len(value) < width:
        return [hashlib.blake2b(value.encode("utf-8"), digest_size=8).hexdigest()]

    modulus = (1 << 64) - 59
    base = 1_000_003
    power = pow(base, width - 1, modulus)
    codepoints = (ord(char) + 1 for char in value)
    recent: deque[int] = deque()
    rolling = 0
    for _ in range(width):
        item = next(codepoints)
        recent.append(item)
        rolling = (rolling * base + item) % modulus
    bottom = {rolling}
    for item in codepoints:
        outgoing = recent.popleft()
        recent.append(item)
        rolling = (
            (rolling - outgoing * power) * base + item
        ) % modulus
        bottom.add(rolling)
        if len(bottom) > size * 4:
            bottom = set(sorted(bottom)[:size])
    return [f"{item:016x}" for item in sorted(bottom)[:size]]


def _structure(text: str) -> dict[str, object]:
    headings: list[dict[str, object]] = []
    ordinal_resets: list[int] = []
    title_offsets: list[int] = []
    terminal_offsets: list[int] = []
    terminal_followups: list[int] = []
    ordinals: list[int] = []
    after_terminal = False
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = unicodedata.normalize("NFKC", raw_line).strip()
        if after_terminal and line:
            terminal_followups.append(offset)
            after_terminal = False
        if _CHAPTER_HEADING_RE.fullmatch(line):
            headings.append(
                {
                    "heading_sha256": sha256_text(normalize_lexical(line)),
                    "offset": offset,
                }
            )
        ordinal_match = _ORDINAL_HEADING_RE.fullmatch(line)
        if ordinal_match:
            ordinal = parse_chinese_number(ordinal_match.group("number"))
            if ordinal is not None:
                if ordinals and ordinal <= 1 and ordinals[-1] >= 3:
                    ordinal_resets.append(offset)
                ordinals.append(ordinal)
        if _BOOK_TITLE_RE.fullmatch(line):
            title_offsets.append(offset)
        if _TERMINAL_LINE_RE.fullmatch(line):
            terminal_offsets.append(offset)
            after_terminal = True
        offset += len(raw_line)
    if text and not text.endswith(("\n", "\r")):
        # splitlines(keepends=True) already includes the final unterminated line.
        offset = len(text)
    boundary_offsets = ordinal_resets + title_offsets[1:] + terminal_followups
    boundary_offsets = sorted(set(boundary_offsets))[:100]
    gaps = [
        headings[index + 1]["offset"] - headings[index]["offset"]
        for index in range(len(headings) - 1)
    ]
    return {
        "chapter_count": len(headings),
        "heading_sha256s": [row["heading_sha256"] for row in headings],
        "chapter_gap_buckets": [min(int(gap) // 1000, 999) for gap in gaps],
        "chapter_ordinal_sha256": sha256_json(ordinals),
        "chapter_ordinal_resets": len(ordinal_resets),
        "book_title_markers": len(title_offsets),
        "terminal_markers": len(terminal_offsets),
        "boundary_offsets": boundary_offsets,
        "possible_work_boundaries": len(boundary_offsets),
    }


def build_fingerprint(text: str, *, source_body_sha256: str = "") -> dict[str, object]:
    layout = normalize_layout(text)
    lexical = normalize_lexical(layout)
    body_hash = sha256_text(text)
    if source_body_sha256 and source_body_sha256 != body_hash:
        raise ValueError("source_body_hash_mismatch")
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "fuzzy_version": FUZZY_VERSION,
        "source_body_sha256": body_hash,
        "layout_sha256": sha256_text(layout),
        "lexical_sha256": sha256_text(lexical),
        "normalized_chars": len(lexical),
        "chunk_sha256s": _chunk_hashes(lexical),
        "fuzzy_signature": _fuzzy_signature(lexical),
        "structure": _structure(text),
    }
    payload["fingerprint_sha256"] = sha256_json(payload)
    return payload


def validate_fingerprint(payload: dict[str, object]) -> None:
    required = {
        "schema_version",
        "normalization_version",
        "chunker_version",
        "fuzzy_version",
        "source_body_sha256",
        "layout_sha256",
        "lexical_sha256",
        "normalized_chars",
        "chunk_sha256s",
        "fuzzy_signature",
        "structure",
        "fingerprint_sha256",
    }
    if set(payload) != required or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("文本指纹结构非法")
    expected = sha256_json(
        {key: value for key, value in payload.items() if key != "fingerprint_sha256"}
    )
    if payload["fingerprint_sha256"] != expected:
        raise ValueError("文本指纹哈希校验失败")


def _multiset_overlap(left: list[str], right: list[str]) -> tuple[int, float, float, float]:
    left_counts, right_counts = Counter(left), Counter(right)
    common = sum((left_counts & right_counts).values())
    union = sum((left_counts | right_counts).values())
    left_contained = common / len(left) if left else (1.0 if not right else 0.0)
    right_contained = common / len(right) if right else (1.0 if not left else 0.0)
    # CDC boundary chunks legitimately differ when a larger edition adds a
    # prefix/suffix.  Interior chunks are the stronger containment evidence.
    if len(left) > 2:
        interior = Counter(left[1:-1])
        left_contained = max(
            left_contained, sum((interior & right_counts).values()) / len(left[1:-1])
        )
    if len(right) > 2:
        interior = Counter(right[1:-1])
        right_contained = max(
            right_contained, sum((interior & left_counts).values()) / len(right[1:-1])
        )
    return (
        common,
        common / union if union else 1.0,
        left_contained,
        right_contained,
    )


def _order_score(left: list[str], right: list[str]) -> float:
    positions: dict[str, list[int]] = defaultdict(list)
    for index, value in enumerate(right):
        positions[value].append(index)
    matched: list[int] = []
    cursors: Counter[str] = Counter()
    for value in left:
        available = positions.get(value, [])
        cursor = cursors[value]
        if cursor < len(available):
            matched.append(available[cursor])
            cursors[value] += 1
    if not matched:
        return 0.0
    tails: list[int] = []
    for position in matched:
        insertion = bisect.bisect_left(tails, position)
        if insertion == len(tails):
            tails.append(position)
        else:
            tails[insertion] = position
    return len(tails) / len(matched)


def compare_fingerprints(
    left: dict[str, object], right: dict[str, object]
) -> FingerprintComparison:
    validate_fingerprint(left)
    validate_fingerprint(right)
    left_chunks = [str(value) for value in left["chunk_sha256s"]]
    right_chunks = [str(value) for value in right["chunk_sha256s"]]
    _, overlap, left_contained, right_contained = _multiset_overlap(
        left_chunks, right_chunks
    )
    left_fuzzy = set(str(value) for value in left["fuzzy_signature"])
    right_fuzzy = set(str(value) for value in right["fuzzy_signature"])
    fuzzy_union = left_fuzzy | right_fuzzy
    fuzzy = len(left_fuzzy & right_fuzzy) / len(fuzzy_union) if fuzzy_union else 1.0
    exact = left["source_body_sha256"] == right["source_body_sha256"]
    same_layout = left["layout_sha256"] == right["layout_sha256"]
    same_lexical = left["lexical_sha256"] == right["lexical_sha256"]
    if exact:
        relation = "exact_duplicate"
    elif same_layout or same_lexical:
        relation = "normalization_candidate"
    elif max(left_contained, right_contained) >= 0.80 and fuzzy >= 0.35:
        relation = "probable_containment"
    elif fuzzy >= 0.55:
        relation = "probable_version"
    else:
        relation = "unrelated_or_uncertain"
    return FingerprintComparison(
        relation=relation,
        exact_body=exact,
        same_layout=same_layout,
        same_lexical=same_lexical,
        chunk_overlap=round(overlap, 6),
        left_contained=round(left_contained, 6),
        right_contained=round(right_contained, 6),
        order_score=round(_order_score(left_chunks, right_chunks), 6),
        fuzzy_jaccard=round(fuzzy, 6),
    )
