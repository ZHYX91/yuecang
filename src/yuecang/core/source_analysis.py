"""Bounded, evidence-only source matching. No work identity is inferred transitively."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import hashlib
import json
import re

from .parsing import chapter_heading_ordinal


@dataclass(frozen=True)
class AnalysisLimits:
    max_blocks: int = 4096
    max_chars: int = 2_000_000
    max_references: int = 2048
    max_fragment_chars: int = 8192
    max_pairs: int = 1000
    max_pair_cells: int = 16_000_000
    max_total_cells: int = 40_000_000
    max_vote_hits: int = 2_000_000
    max_matches: int = 500
    max_boundaries: int = 200
    max_alignment_cells: int = 1_000_000


def identifier(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:32]


def span(source_id, block_id, start, end):
    return {"source_id": source_id, "block_id": block_id, "start": start, "end": end}


def _anchors(text, *, query=False):
    if len(text) < 16:
        return (text,) if text.strip() else ()
    stride = 1 if query else max(1, (len(text) - 15) // 96)
    return dict.fromkeys(text[i:i + 16] for i in range(0, len(text) - 15, stride))


def _quoted(text):
    return bool(re.match(r'\s*(?:[>“「『"]|引用|引文|摘录|例如|参考文献)', text))


def inspect_source(source_id, blocks, references, *, limits=None, check=None):
    """References contain only explicitly assigned spans, never inferred candidates.

    Each reference: {span, text, book_id, edition_id}. Returned coordinates are
    raw Unicode codepoints. Matching raw text is evidence of reuse, not identity.
    """
    limits = limits or AnalysisLimits()
    check = check or (lambda: None)
    reasons, selected, chars = set(), [], 0
    for block in blocks:
        if len(selected) >= limits.max_blocks or chars + len(block["text"]) > limits.max_chars:
            reasons.add("source_budget")
            break
        selected.append(block)
        chars += len(block["text"])
    refs, index, ref_chars = [], defaultdict(list), 0
    for ref in references:
        text = ref["text"]
        if ref["span"]["source_id"] == source_id or not text.strip():
            continue
        if len(refs) >= limits.max_references or ref_chars + len(text) > limits.max_chars:
            reasons.add("reference_budget")
            break
        if len(text) > limits.max_fragment_chars:
            reasons.add("reference_fragment_budget")
            continue
        n = len(refs)
        refs.append(ref)
        ref_chars += len(text)
        for token in _anchors(text):
            index[token].append(n)
    matches, unmatched, dominant, boundaries, proofs = [], [], {}, [], {}
    pairs = cells = vote_hits = 0
    previous_ordinal = None

    def boundary(i, kind, why, uncertainty, offset=0, evidence=None):
        block = selected[i]
        location = span(source_id, block["id"], offset, offset)
        evidence = evidence or []
        if len(boundaries) >= limits.max_boundaries:
            reasons.add("boundary_budget")
            return
        boundaries.append({"id": identifier([location, kind, evidence]), "kind": kind,
                           "position": location,
                           "before": block["text"][max(0, offset - 240):offset] if offset else selected[i - 1]["text"][-240:] if i else "",
                           "after": block["text"][offset:offset + 240], "reasons": [why],
                           "uncertainty": uncertainty, "evidence": evidence, "state": "pending"})

    for i, block in enumerate(selected):
        check()
        text = block["text"]
        if i and re.match(r"^\s*《[^》\n]+》", text) and (block.get("type") == "heading" or len(text) < 120):
            boundary(i, "title_marker", "出现显式书名标记", "也可能是引用、重印题名或同书分部，不能据此认定换书")
        if block.get("type") == "heading":
            ordinal = chapter_heading_ordinal(text)
            if ordinal is not None:
                if previous_ordinal is not None and previous_ordinal >= 3 and ordinal <= 1:
                    boundary(i, "numbering_reset", "章节编号从较大编号重新开始", "同书分卷、附录和续补也会重置编号；仅提示核查")
                previous_ordinal = ordinal
        covered, strong_books, local_ranges = [], set(), defaultdict(set)
        fully_compared = True
        block_match_start = len(matches)
        if len(text) > limits.max_fragment_chars or block.get("type") in {"figure", "formula", "table", "unknown"}:
            reasons.add("source_fragment_budget")
            unmatched.append(span(source_id, block["id"], 0, len(text)))
            continue
        votes = Counter()
        for token in _anchors(text, query=True):
            postings = index.get(token, ())
            if vote_hits + len(postings) > limits.max_vote_hits:
                reasons.add("anchor_budget")
                fully_compared = False
                break
            votes.update(postings)
            vote_hits += len(postings)
        if len(votes) > 16:
            reasons.add("candidate_budget")
            fully_compared = False
        for n in sorted(votes, key=lambda n: (-votes[n], n))[:16]:
            ref = refs[n]
            cost = len(text) * len(ref["text"])
            if pairs >= limits.max_pairs or cost > limits.max_pair_cells or cells + cost > limits.max_total_cells:
                reasons.add("comparison_budget")
                fully_compared = False
                continue
            if len(matches) >= limits.max_matches:
                reasons.add("match_budget")
                fully_compared = False
                break
            check()
            pairs += 1
            cells += cost
            match = SequenceMatcher(None, text, ref["text"], autojunk=False).find_longest_match()
            # Short headings and stock phrases must not establish book identity.
            if match.size < 24:
                continue
            target = span(source_id, block["id"], match.a, match.a + match.size)
            origin = {**ref["span"], "start": ref["span"]["start"] + match.b,
                      "end": ref["span"]["start"] + match.b + match.size}
            quote = _quoted(text) or match.size < len(text) * .60
            uncertainty = "可能是引用或局部复用；不能把整段或整文件归入该书" if quote else "相同片段也可能被其他作品引用；图书及版本归属仍需确认"
            matches.append({"id": identifier([target, origin, ref["book_id"], ref["edition_id"]]),
                            "kind": "assignment_suggestion", "span": target,
                            "book_id": ref["book_id"], "edition_id": ref["edition_id"],
                            "preview": text[match.a:match.a + min(match.size, 240)],
                            "evidence": [{"span": origin, "preview": ref["text"][match.b:match.b + min(match.size, 240)],
                                          "match_chars": match.size, "relation": "exact_text_span"}],
                            "uncertainty": uncertainty})
            covered.append((match.a, match.a + match.size))
            local_ranges[(match.a, match.a + match.size)].add(ref["book_id"])
            if not quote:
                strong_books.add(ref["book_id"])
        cursor = 0
        for start, end in sorted(covered):
            if start > cursor:
                unmatched.append(span(source_id, block["id"], cursor, start))
            cursor = max(cursor, end)
        if cursor < len(text):
            unmatched.append(span(source_id, block["id"], cursor, len(text)))
        proofs[i] = sorted(({"span": m["span"], "book_id": m["book_id"], "edition_id": m["edition_id"],
                             "matched_source": m["evidence"][0]} for m in matches[block_match_start:]), key=identifier)
        # An extractor can place several books in one paragraph. Only use
        # non-overlapping directly matched ranges; overlapping identities abstain.
        unambiguous = all(len(books) == 1 for books in local_ranges.values())
        ranges = [(start, end, next(iter(books))) for (start, end), books in sorted(local_ranges.items())]
        nonoverlap = all(a[1] <= b[0] for a, b in zip(ranges, ranges[1:]))
        if fully_compared and unambiguous and not _quoted(text) and nonoverlap and sum(end - start for start, end, _ in ranges) >= .8 * len(text):
            for previous, following in zip(ranges, ranges[1:]):
                if previous[2] != following[2]:
                    nearby = []
                    for start, end, book in (previous, following):
                        nearby.extend([p for p in proofs[i] if (p["span"]["start"], p["span"]["end"], p["book_id"]) == (start, end, book)][:2])
                    boundary(i, "known_source_change", "本块相邻范围分别精确匹配已归属不同图书的来源", "也可能是引文或合刊；只核查此位置，不自动拆书", following[0], nearby)
        if fully_compared and len(strong_books) == 1 and len(set().union(*local_ranges.values())) == 1:
            dominant[i] = next(iter(strong_books))
            if i - 1 in dominant and dominant[i - 1] != dominant[i]:
                nearby = [p for p in proofs.get(i - 1, []) if p["book_id"] == dominant[i - 1]][:2] + [p for p in proofs[i] if p["book_id"] == dominant[i]][:2]
                boundary(i, "known_source_change", "相邻范围分别精确匹配已归属不同图书的来源", "可能混书，也可能是长引文、合刊或续补；本提示不建立作品身份", evidence=nearby)
    # Make ambiguity visible for text shared by several works or editions.
    targets = defaultdict(set)
    for item in matches:
        targets[(item["span"]["block_id"], item["span"]["start"], item["span"]["end"])].add((item["book_id"], item["edition_id"]))
    for item in matches:
        if len(targets[(item["span"]["block_id"], item["span"]["start"], item["span"]["end"])]) > 1:
            item["uncertainty"] = "该范围匹配多个图书或版本，不能仅凭共有文字确定归属"
    return {"source_id": source_id, "boundaries": boundaries, "matches": matches,
            "unmatched": unmatched, "incomplete": bool(reasons), "limit_reasons": sorted(reasons),
            "limits": asdict(limits), "analyzed_blocks": len(selected), "total_blocks": len(blocks),
            "automatic_assignment": False}


def align_fragments(left, right, *, limits=None, check=None):
    """Align ordered assigned spans using exact anchors; all alternatives stay explicit."""
    limits = limits or AnalysisLimits()
    check = check or (lambda: None)
    reasons = set()
    if len(left) > limits.max_blocks or len(right) > limits.max_blocks or len(left) * len(right) > limits.max_alignment_cells:
        return {"rows": [], "incomplete": True, "limit_reasons": ["alignment_budget"], "automatic_merge": False}
    if sum(len(f["text"]) for f in [*left, *right]) > limits.max_chars:
        return {"rows": [], "incomplete": True, "limit_reasons": ["alignment_character_budget"], "automatic_merge": False}
    def key(fragment):
        return identifier([fragment["text"], fragment.get("type", "paragraph"), fragment.get("level"),
                           fragment.get("attributes", {}), fragment.get("resources", [])])
    a, b = [key(f) for f in left], [key(f) for f in right]
    ca, cb = Counter(a), Counter(b)
    check()
    operations = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    rows = []

    def row(kind, lhs, rhs, reason):
        lspans, rspans = [f["span"] for f in lhs], [f["span"] for f in rhs]
        rows.append({"id": identifier([kind, lspans, rspans]), "kind": kind,
                     "left": lspans, "right": rspans,
                     "left_preview": "\n".join(f["text"][:240] for f in lhs[:3])[:720],
                     "right_preview": "\n".join(f["text"][:240] for f in rhs[:3])[:720],
                     "reasons": [reason], "requires_confirmation": True})

    def unique_anchor(operation, *, last=False):
        tag, i, j, _, _ = operation
        if tag != "equal":
            return False
        token = a[j - 1 if last else i]
        return ca[token] == cb[token] == 1

    for index, (tag, i, j, k, l) in enumerate(operations):
        check()
        remaining = limits.max_matches - len(rows)
        if remaining <= 0:
            reasons.add("alignment_row_budget")
            break
        if tag == "equal":
            for x, y in zip(range(i, min(j, i + remaining)), range(k, l)):
                ambiguous = ca[a[x]] > 1 or cb[b[y]] > 1
                row("ambiguous" if ambiguous else "exact_duplicate", [left[x]], [right[y]],
                    "相同内容重复出现，出现位置对应关系需核查" if ambiguous else "同书同版本的已归属范围内容与结构一致")
            if j - i > remaining:
                reasons.add("alignment_row_budget")
                break
        elif tag == "delete":
            row("left_only", left[i:j], [], "仅在左侧所选范围找到；不代表右侧整书缺失")
        elif tag == "insert":
            row("right_only", [], right[k:l], "仅在右侧所选范围找到；不代表左侧整书缺失")
        elif (j - i == l - k == 1 and left[i]["text"] != right[k]["text"]
              and (index > 0 and unique_anchor(operations[index - 1], last=True)
                   or index + 1 < len(operations) and unique_anchor(operations[index + 1]))):
            row("different_text", left[i:j], right[k:l], "精确锚点旁各有一个不同文本块；对应关系和取舍需确认")
        else:
            row("ambiguous", left[i:j], right[k:l], "缺少足够的精确锚点，无法可靠配对这些范围；可人工选择，不按长度自动取舍")
    return {"rows": rows, "incomplete": bool(reasons), "limit_reasons": sorted(reasons), "automatic_merge": False}
