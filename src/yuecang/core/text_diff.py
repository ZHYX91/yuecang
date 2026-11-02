"""Content-bound, reusable numeric triage. Similarity never implies a merge."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, deque
from difflib import SequenceMatcher
from bisect import bisect_left

RULE_VERSION = "strict-layout-hierarchical-paged-v4"
CHAR_DIFF_LIMIT = 20_000
MAX_DIFF_REGIONS = 512
MATCH_PAIR_LIMIT = 200_000
LINE_DIFF_LIMIT = 30_000
ANCHOR_SCAN_LIMIT = 500_000
ANCHOR_WIDTH = 64
ANCHOR_STRIDE = 128
REFINEMENT_SCAN_BUDGET = 24_000_000
REFINEMENT_DEPTH_LIMIT = 8
POSITION_INDEX_LIMIT = 200_000


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strict_layout(text: str) -> str:
    """Keep characters, punctuation, case and word boundaries unchanged."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def evidence_key(members: list[dict], *, rule_version: str = RULE_VERSION) -> str:
    import json
    rows = [{k: row[k] for k in ("document_id", "source_content_sha256", "source_start_char", "source_end_char")} for row in members]
    rows.sort(key=lambda row: (str(row["document_id"]), str(row["source_start_char"]), str(row["source_end_char"]), row["source_content_sha256"]))
    return digest(json.dumps({"rule_version": rule_version, "members": rows}, sort_keys=True, separators=(",", ":")))


def common_edges(left: str, right: str) -> tuple[int, int]:
    size = min(len(left), len(right))
    prefix = 0
    while prefix < size and left[prefix] == right[prefix]:
        prefix += 1
    suffix = 0
    while suffix < size - prefix and left[-suffix - 1] == right[-suffix - 1]:
        suffix += 1
    return prefix, suffix


def _anchor_opcodes(left: str, right: str, start: int, other_start: int) -> list[tuple] | None:
    """Align exact shared windows before diffing gaps; bound scanning and memory."""
    if min(len(left),len(right))<ANCHOR_WIDTH*2 or len(left)+len(right)>ANCHOR_SCAN_LIMIT:
        return None
    windows={};duplicates=set()
    for i in range(0,len(left)-ANCHOR_WIDTH+1,ANCHOR_STRIDE):
        key=left[i:i+ANCHOR_WIDTH]
        if key in windows:duplicates.add(key)
        else:windows[key]=i
    for key in duplicates:windows.pop(key,None)
    if not windows:return None
    matches={};repeated=set()
    for j in range(len(right)-ANCHOR_WIDTH+1):
        key=right[j:j+ANCHOR_WIDTH]
        if key in windows:
            if key in matches:repeated.add(key)
            else:matches[key]=j
    pairs=sorted((windows[key],j) for key,j in matches.items() if key not in repeated)
    # Longest increasing subsequence prevents reordered sections from becoming
    # a false equal span. Every emitted window is also compared directly.
    tails=[];tail_indices=[];previous=[-1]*len(pairs)
    for index,(_,j) in enumerate(pairs):
        k=bisect_left(tails,j)
        if k:previous[index]=tail_indices[k-1]
        if k==len(tails):tails.append(j);tail_indices.append(index)
        else:tails[k]=j;tail_indices[k]=index
    if not tail_indices:return None
    chain=[];index=tail_indices[-1]
    while index>=0:chain.append(pairs[index]);index=previous[index]
    chain.reverse();anchors=[];a_end=b_end=0
    for i,j in chain:
        if i>=a_end and j>=b_end:
            if left[i:i+ANCHOR_WIDTH]!=right[j:j+ANCHOR_WIDTH]:raise ValueError('false_anchor')
            anchors.append((i,j));a_end=i+ANCHOR_WIDTH;b_end=j+ANCHOR_WIDTH
    if len(anchors)<2:return None
    out=[];a_end=b_end=0
    for i,j in anchors:
        if i>a_end or j>b_end:out.extend(_bounded_opcodes(left[a_end:i],right[b_end:j],start+a_end,other_start+b_end))
        out.append(('equal',start+i,start+i+ANCHOR_WIDTH,other_start+j,other_start+j+ANCHOR_WIDTH))
        a_end=i+ANCHOR_WIDTH;b_end=j+ANCHOR_WIDTH
    if a_end<len(left) or b_end<len(right):out.extend(_bounded_opcodes(left[a_end:],right[b_end:],start+a_end,other_start+b_end))
    return out


def _coarse_opcodes(left: str, right: str, start: int = 0, other_start: int = 0) -> list[tuple]:
    if len(left) + len(right) <= CHAR_DIFF_LIMIT:
        frequencies = Counter(left)
        if sum(frequencies[ch] * n for ch, n in Counter(right).items()) > MATCH_PAIR_LIMIT:
            anchored=_anchor_opcodes(left,right,start,other_start)
            if anchored is not None:return anchored
            return [("unresolved", start, start + len(left), other_start, other_start + len(right))]
        return [(tag, a + start, b + start, c + other_start, d + other_start) for tag, a, b, c, d in SequenceMatcher(None, left, right, autojunk=False).get_opcodes()]
    anchored=_anchor_opcodes(left,right,start,other_start)
    if anchored is not None:return anchored
    # Line alignment bounds expensive character matching to the changed lines.
    # Large unmatched blocks remain explicit unresolved blocks, never omissions.
    left_lines, right_lines = left.splitlines(keepends=True), right.splitlines(keepends=True)
    if len(left_lines) < 2 or len(right_lines) < 2 or len(left_lines) + len(right_lines) > LINE_DIFF_LIMIT:
        return [("unresolved", start, start + len(left), other_start, other_start + len(right))]
    line_frequencies = Counter(left_lines)
    # SequenceMatcher removes popular right-side lines with autojunk=True.
    # Bound the surviving match pairs before entering its quadratic search.
    right_frequencies = Counter(right_lines)
    popular_threshold = len(right_lines) // 100 + 1 if len(right_lines) >= 200 else len(right_lines)
    if sum(line_frequencies[line] * n for line, n in right_frequencies.items() if n <= popular_threshold) > MATCH_PAIR_LIMIT:
        return [("unresolved", start, start + len(left), other_start, other_start + len(right))]
    a_offsets, b_offsets = [start], [other_start]
    for line in left_lines: a_offsets.append(a_offsets[-1] + len(line))
    for line in right_lines: b_offsets.append(b_offsets[-1] + len(line))
    out = []
    for tag, a, b, c, d in SequenceMatcher(None, left_lines, right_lines, autojunk=True).get_opcodes():
        x, y, z, w = a_offsets[a], a_offsets[b], b_offsets[c], b_offsets[d]
        if tag == "equal":
            out.append((tag, x, y, z, w))
        elif (y - x) + (w - z) <= CHAR_DIFF_LIMIT:
            out.extend(_coarse_opcodes(left[x-start:y-start], right[z-other_start:w-other_start], x, z))
        else:
            out.append(("unresolved", x, y, z, w))
    return out


def _unique_positions(rows):
    """Repeated fingerprints cannot establish a unique correspondence."""
    positions = {}
    for key, position, width in rows:
        positions[key] = None if key in positions else (position, width)
    return positions


def _cdc_positions(text: str):
    """Content-defined chunks in strict text coordinates (no lexical folding)."""
    mask = (1 << 64) - 1
    outgoing_factor = pow(257, 64, 1 << 64)
    rolling = start = 0
    for index, char in enumerate(text):
        rolling = (rolling * 257 + ord(char) + 1) & mask
        if index >= 64:
            rolling = (rolling - (ord(text[index - 64]) + 1) * outgoing_factor) & mask
        end = index + 1
        size = end - start
        if size >= 256 and (rolling & 1023 == 0 or size >= 2048):
            yield digest(text[start:end]), start, size
            start = end
    if start < len(text):
        yield digest(text[start:]), start, len(text) - start


def _local_positions(text: str, *, width: int = 16, window: int = 32):
    """Winnow rolling k-grams; retain rightmost minima and their positions."""
    mask = (1 << 64) - 1
    outgoing_factor = pow(257, width, 1 << 64)
    rolling = 0
    minima = deque()
    last = -1
    for end, char in enumerate(text):
        rolling = (rolling * 257 + ord(char) + 1) & mask
        if end >= width:
            rolling = (rolling - (ord(text[end - width]) + 1) * outgoing_factor) & mask
        if end + 1 < width:
            continue
        position = end + 1 - width
        # Mix correlated polynomial hashes before selecting local minima.
        value = rolling
        value = ((value ^ (value >> 30)) * 0xbf58476d1ce4e5b9) & mask
        value = ((value ^ (value >> 27)) * 0x94d049bb133111eb) & mask
        value ^= value >> 31
        while minima and minima[0][1] <= position - window:
            minima.popleft()
        while minima and minima[-1][0] >= value:
            minima.pop()
        minima.append((value, position))
        if position >= window - 1 and minima[0][1] != last:
            fingerprint, last = minima[0]
            yield fingerprint, last, width


def _position_anchors(left: str, right: str, mode: str, context: dict):
    size = len(left) + len(right)
    if size > context['scan_remaining']:
        context['events']['scan_budget_exhausted'] += 1
        return []
    context['scan_remaining'] -= size
    context['events'][mode + '_scanned_chars'] += size
    generate = _cdc_positions if mode == 'cdc' else _local_positions
    indexes = []
    for text in (left, right):
        rows = []
        for row in generate(text):
            if len(rows) >= POSITION_INDEX_LIMIT:
                context['events']['position_index_limit'] += 1
                return []
            rows.append(row)
        indexes.append(_unique_positions(rows))
    pairs = []
    for key, match in indexes[0].items():
        other = indexes[1].get(key)
        if match is None or other is None:
            continue
        a, width = match
        b, other_width = other
        # Hash collisions and chunk-length mismatches never become equality.
        if width == other_width and left[a:a + width] == right[b:b + width]:
            pairs.append((a, b, width))
    pairs.sort()
    tails, tail_indices = [], []
    previous = [-1] * len(pairs)
    for index, (_, b, _) in enumerate(pairs):
        k = bisect_left(tails, b)
        if k:
            previous[index] = tail_indices[k - 1]
        if k == len(tails):
            tails.append(b)
            tail_indices.append(index)
        else:
            tails[k], tail_indices[k] = b, index
    chain = []
    index = tail_indices[-1] if tail_indices else -1
    while index >= 0:
        chain.append(pairs[index])
        index = previous[index]
    anchors = []
    a_end = b_end = 0
    for a, b, width in reversed(chain):
        if a >= a_end and b >= b_end:
            anchors.append((a, b, width))
            a_end, b_end = a + width, b + width
    context['events'][mode + '_verified_anchors'] += len(anchors)
    return anchors


def _refine_opcodes(left: str, right: str, start: int, other_start: int,
                    context: dict, depth: int = 0) -> list[tuple]:
    """Refine only old unresolved spans; previously proven equal spans survive."""
    prefix, suffix = common_edges(left, right)
    a_end, b_end = len(left) - suffix, len(right) - suffix
    out = []
    if prefix:
        out.append(('equal', start, start + prefix, other_start, other_start + prefix))
    a, b = left[prefix:a_end], right[prefix:b_end]
    x, y = start + prefix, other_start + prefix
    frequencies = Counter(a) if len(a) + len(b) <= CHAR_DIFF_LIMIT else None
    if not a or not b:
        if a or b:
            out.append(('delete' if a else 'insert', x, x + len(a), y, y + len(b)))
    elif len(a) + len(b) <= CHAR_DIFF_LIMIT and sum(
        count * frequencies[char] for char, count in Counter(b).items()
    ) <= MATCH_PAIR_LIMIT:
        out.extend((tag, x + i, x + j, y + k, y + l)
                   for tag, i, j, k, l in SequenceMatcher(None, a, b, autojunk=False).get_opcodes())
    else:
        anchors = []
        prior_limits = context['events']['scan_budget_exhausted'] + context['events']['position_index_limit']
        if depth < REFINEMENT_DEPTH_LIMIT:
            if len(a) + len(b) > 50_000:
                anchors = _position_anchors(a, b, 'cdc', context)
            if not anchors:
                anchors = _position_anchors(a, b, 'local', context)
        if anchors:
            i = j = 0
            for ai, bi, width in anchors:
                if ai != i or bi != j:
                    out.extend(_refine_opcodes(a[i:ai], b[j:bi], x + i, y + j, context, depth + 1))
                out.append(('equal', x + ai, x + ai + width, y + bi, y + bi + width))
                i, j = ai + width, bi + width
            if i < len(a) or j < len(b):
                out.extend(_refine_opcodes(a[i:], b[j:], x + i, y + j, context, depth + 1))
        else:
            reason = ('depth_limit' if depth >= REFINEMENT_DEPTH_LIMIT else
                      'scan_budget_or_index_limit' if context['events']['scan_budget_exhausted'] + context['events']['position_index_limit'] > prior_limits else
                      'no_unique_local_anchor')
            context['unresolved'].append({'left_span': [x, x + len(a)],
                                          'right_span': [y, y + len(b)], 'reason': reason})
            out.append(('unresolved', x, x + len(a), y, y + len(b)))
    if suffix:
        out.append(('equal', start + a_end, start + len(left), other_start + b_end, other_start + len(right)))
    return out


def _bounded_opcodes(left: str, right: str, start: int = 0, other_start: int = 0) -> list[tuple]:
    # Compatibility for the fixed-window coarse pass: recursion stays coarse.
    return _coarse_opcodes(left, right, start, other_start)


def iter_difference_pages(comparison: dict, *, page_size: int = MAX_DIFF_REGIONS):
    """Page every numeric difference; never truncate or collapse verified gaps."""
    if type(page_size) is not int or not 1 <= page_size <= MAX_DIFF_REGIONS:
        raise ValueError('invalid_difference_page_size')
    rows = comparison.get('opcodes', [])
    count = (len(rows) + page_size - 1) // page_size
    binding = digest(json.dumps(comparison, ensure_ascii=False, sort_keys=True,
                                separators=(',', ':'), allow_nan=False))
    for index, offset in enumerate(range(0, len(rows), page_size)):
        yield {'rule_version': comparison['rule_version'], 'comparison_sha256': binding, 'page_index': index,
               'page_count': count, 'first_opcode_index': offset,
               'difference_region_count': len(rows),
               'opcodes': rows[offset:offset + page_size]}


def compare_texts(left: str, right: str) -> dict:
    """Return complete Unicode code-point coverage and hashes of changed ranges."""
    if not left or not right:
        return {"relation": "empty_or_uncertain", "automatic_equivalence": False, "complete_difference_coverage": False, "rule_version": RULE_VERSION}
    if left == right:
        return {"relation": "exact_text_equal", "automatic_equivalence": True, "complete_difference_coverage": True, "equal_chars": len(left), "left_chars": len(left), "right_chars": len(right), "opcodes": [], "rule_version": RULE_VERSION}
    prefix, suffix = common_edges(left, right)
    a_end, b_end = len(left) - suffix, len(right) - suffix
    operations = []
    if prefix: operations.append(("equal", 0, prefix, 0, prefix))
    operations.extend(_bounded_opcodes(left[prefix:a_end], right[prefix:b_end], prefix, prefix))
    if suffix: operations.append(("equal", a_end, len(left), b_end, len(right)))
    context = {'scan_remaining': REFINEMENT_SCAN_BUDGET, 'events': Counter(), 'unresolved': []}
    refined = []
    for tag, a, b, c, d in operations:
        if tag == 'unresolved':
            refined.extend(_refine_opcodes(left[a:b], right[c:d], a, c, context))
        else:
            refined.append((tag, a, b, c, d))
    operations = refined
    rows, equal_chars, unresolved = [], 0, False
    a_cursor = b_cursor = 0
    for tag, a, b, c, d in operations:
        if (a, c) != (a_cursor, b_cursor) or not (a <= b <= len(left) and c <= d <= len(right)):
            raise ValueError("noncontiguous_difference_coverage")
        a_cursor, b_cursor = b, d
        if tag == "equal":
            if left[a:b] != right[c:d]: raise ValueError("false_equal_region")
            equal_chars += b - a
        else:
            unresolved |= tag == "unresolved"
            rows.append({"tag": tag, "left_span": [a,b], "right_span": [c,d], "left_chars": b-a, "right_chars": d-c, "left_sha256": digest(left[a:b]), "right_sha256": digest(right[c:d])})
    if (a_cursor, b_cursor) != (len(left), len(right)):
        raise ValueError("incomplete_difference_coverage")
    containment = None
    if len(left) <= len(right):
        pos = right.find(left)
        if pos >= 0: containment = {"contained":"left", "container":"right", "span":[pos,pos+len(left)]}
    else:
        pos = left.find(right)
        if pos >= 0: containment = {"contained":"right", "container":"left", "span":[pos,pos+len(right)]}
    return {"relation":"verified_containment_needs_extent_review" if containment else "different_text_needs_review", "automatic_equivalence":False,"complete_difference_coverage":True,"left_chars":len(left),"right_chars":len(right),"equal_chars":equal_chars,"changed_chars":sum(row['left_chars']+row['right_chars'] for row in rows),"large_unmatched_regions":unresolved,"containment":containment,"opcodes":rows,"rule_version":RULE_VERSION,
            "difference_region_count": len(rows), "difference_page_size": MAX_DIFF_REGIONS,
            "difference_page_count": (len(rows) + MAX_DIFF_REGIONS - 1) // MAX_DIFF_REGIONS,
            "alignment_diagnostics": {"events": dict(context['events']),
                                      "scanned_chars": REFINEMENT_SCAN_BUDGET - context['scan_remaining'],
                                      "unresolved_regions": context['unresolved']}}


def compare_stories(left: str, right: str, *, converted_left: str | None = None, converted_right: str | None = None) -> dict:
    """Compare matching views; only identical original text is equivalent.

    Layout differences can change code, formulas, or tables. Matching views are
    candidate evidence, and their offsets never address the original strings.
    """
    if not left or not right:
        return compare_texts(left,right)
    a = left if converted_left is None else converted_left
    b = right if converted_right is None else converted_right
    layout_a, layout_b = strict_layout(a), strict_layout(b)
    result = compare_texts(layout_a,layout_b)
    if result['automatic_equivalence']:
        result['relation'] = 'exact_story_equal' if left == right else 'normalization_candidate'
        result['automatic_equivalence'] = left == right
    result.update({"left_story_sha256":digest(left),"right_story_sha256":digest(right),"left_layout_sha256":digest(layout_a),"right_layout_sha256":digest(layout_b),"offset_space":"normalized_matching_view_codepoints", "semantic_verdict_performed":False,"original_completion_proven":False})
    return result
