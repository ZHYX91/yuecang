"""Bounded candidate alignment for a fixed, ordered base extraction."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import chain
import json

from .source_analysis import identifier


_EXTRACTION_ATTRIBUTES = {"ocr", "confidence", "language", "page_text", "layout_unverified"}
_NEAR_SCORE = .86
_MARGIN = .04


@dataclass(frozen=True)
class CurationLimits:
    max_donors: int = 4
    max_assignments: int = 8192
    max_extraction_bytes: int = 32 * 1024**2
    max_blocks: int = 4096
    max_chars: int = 2_000_000
    max_pair_cells: int = 20_000_000
    max_total_cells: int = 60_000_000
    max_query_shingles: int = 6_000_000
    max_vote_hits: int = 2_000_000
    max_candidates: int = 16


@dataclass(frozen=True)
class Match:
    base: tuple[int, ...]
    donor: tuple[int, ...]
    relation: str
    score: float | None = None


def structured(block):
    # These attributes describe extraction quality rather than inline structure.
    return (block.get("type", "paragraph") != "paragraph" or bool(block.get("resources"))
            or bool(set(block.get("attributes", {})) - _EXTRACTION_ATTRIBUTES))


def _content(fragment):
    attributes = {key: value for key, value in fragment.get("attributes", {}).items()
                  if key not in _EXTRACTION_ATTRIBUTES}
    structure = [fragment.get("type", "paragraph"), fragment.get("level"),
                 attributes, fragment.get("resources", [])]
    return fragment["text"], json.dumps(structure, sort_keys=True, ensure_ascii=False)


def _contiguous(left, right):
    a, b = left["span"], right["span"]
    if left["ordinal"] == right["ordinal"]:
        return a["end"] == b["start"]
    return (left["ordinal"] + 1 == right["ordinal"] and a["end"] == left["block_length"]
            and b["start"] == 0)


def _groups(fragments, reserved, *, reserved_singles=False):
    groups = {}
    plain = [not structured(fragment) for fragment in fragments]
    texts = ["".join(fragment["text"].split()) if reserved_singles or i not in reserved else ""
             for i, fragment in enumerate(fragments)]
    for i, text in enumerate(texts):
        if len(text) >= 24:
            groups[(i,)] = text
        if not text or not plain[i] or i in reserved:
            continue
        combined = text
        for j in range(i + 1, min(i + 3, len(fragments))):
            if not texts[j] or not plain[j] or j in reserved or not _contiguous(fragments[j - 1], fragments[j]):
                break
            combined += texts[j]
            if len(combined) >= 24:
                groups[tuple(range(i, j + 1))] = combined
    return groups


def _shingles(text, step=1):
    return (text[i:i + 4] for i in range(0, len(text) - 3, step))


def _near_matches(base, donor, reserved_base, reserved_donor, limits, budget, reasons, check):
    right = _groups(donor, reserved_donor)
    if not right:
        return [], {}
    # A second, near-identical donor occurrence may refer to an already
    # matched base position. It can block placement but cannot replace it.
    left = _groups(base, reserved_base, reserved_singles=True)
    index = defaultdict(list)
    for group, text in left.items():
        check()
        for token in dict.fromkeys(_shingles(text, max(1, len(text) // 96))):
            postings = index[token]
            if len(postings) <= 16:
                postings.append(group)
    if not index:
        return [], {}

    candidates, ambiguous = [], defaultdict(set)
    for rgroup, rtext in right.items():
        check()
        budget["shingles"] += len(rtext) - 3
        if budget["shingles"] > limits.max_query_shingles:
            reasons.add("shingle_budget")
            return [], {}
        votes = Counter()
        seen, common = set(), {}
        common_threshold = max(2, min(8, (len(rtext) - 3) // 8))
        for token in _shingles(rtext):
            postings = index.get(token, ())
            if len(postings) > 16:
                if len(common) < common_threshold:
                    common[token] = postings
                continue
            if not postings or token in seen:
                continue
            seen.add(token)
            budget["votes"] += len(postings)
            if budget["votes"] > limits.max_vote_hits:
                reasons.add("anchor_budget")
                return [], {}
            for lgroup in postings:
                if min(len(lgroup), len(rgroup)) == 1:
                    votes[lgroup] += 1
        if len(common) == common_threshold:
            # Saturated postings cannot establish an alignment. Several such
            # shingles can still warn that a repeated passage may be related.
            positions = sorted({i for postings in common.values() for group in postings for i in group})[:12]
            for i in rgroup:
                ambiguous[i].update(positions)
        recalled = [lgroup for lgroup, hits in votes.most_common() if hits >= 2
                    and base[lgroup[0]]["type"] == donor[rgroup[0]]["type"]
                    and 2 * min(len(left[lgroup]), len(rtext)) >= (_NEAR_SCORE - _MARGIN) * (len(left[lgroup]) + len(rtext))]
        if len(recalled) > limits.max_candidates:
            reasons.add("candidate_budget")
        for lgroup in recalled[:limits.max_candidates]:
            ltext = left[lgroup]
            cells = len(ltext) * len(rtext)
            if cells > limits.max_pair_cells or budget["cells"] + cells > limits.max_total_cells:
                reasons.add("comparison_budget")
                continue
            budget["cells"] += cells
            score = SequenceMatcher(None, ltext, rtext, autojunk=False).ratio()
            if score >= _NEAR_SCORE - _MARGIN:
                relation = "split_or_merge" if len(lgroup) != len(rgroup) else "text_variation"
                candidates.append(Match(lgroup, rgroup, relation, score))

    # A group must win on every covered position in both sources. Close
    # alternatives are evidence of ambiguity even if only one fits a given gap.
    left_votes, right_votes = defaultdict(list), defaultdict(list)
    for match in sorted(candidates, key=lambda item: -item.score):
        for i in match.base:
            left_votes[i].append(match)
        for i in match.donor:
            right_votes[i].append(match)

    def wins(match, alternatives):
        return alternatives[0] == match and (len(alternatives) == 1 or match.score - alternatives[1].score >= _MARGIN)

    accepted = []
    for match in candidates:
        if (match.score >= _NEAR_SCORE
                and all(i not in reserved_base and wins(match, left_votes[i]) for i in match.base)
                and all(wins(match, right_votes[i]) for i in match.donor)):
            accepted.append(match)
        else:
            # Plausibly related material stays unplaced even when its score is
            # too low to offer as a confirmed replacement candidate.
            for i in match.donor:
                ambiguous[i].update(match.base)
    return accepted, ambiguous


def _pair(base, donor, limits, budget, reasons, check):
    base_content, donor_content = defaultdict(list), defaultdict(list)
    for i, item in enumerate(base):
        base_content[_content(item)].append(i)
    for i, item in enumerate(donor):
        donor_content[_content(item)].append(i)

    matches, ambiguous, reserved_base = [], {}, set()
    for key, right in donor_content.items():
        left = base_content.get(key, ())
        if not left:
            continue
        reserved_base.update(left)
        if len(left) == len(right) == 1:
            matches.append(Match(tuple(left), tuple(right), "identical_content"))
        else:
            ambiguous.update((i, left) for i in right)

    reserved_donor = set(ambiguous) | {i for match in matches for i in match.donor}
    base_text, donor_text = defaultdict(list), defaultdict(list)
    for i, item in enumerate(base):
        base_text[item["text"]].append(i)
    for i, item in enumerate(donor):
        if i not in reserved_donor:
            donor_text[item["text"]].append(i)
    for text, right in donor_text.items():
        left = base_text.get(text, ())
        if not left:
            continue
        reserved_donor.update(right)
        if len(left) == len(right) == 1 and left[0] not in reserved_base:
            matches.append(Match(tuple(left), tuple(right), "structure_variation"))
        else:
            ambiguous.update((i, left) for i in right)
        reserved_base.update(left)

    near, uncertain = _near_matches(base, donor, reserved_base, reserved_donor, limits, budget, reasons, check)
    matches.extend(near)
    ambiguous.update((i, sorted(positions)) for i, positions in uncertain.items())
    matches.sort(key=lambda item: item.donor[0])

    # Mark both sides of each inversion, including an exact anchor crossed by
    # a moved match. Such evidence cannot locate an insertion.
    moved = set()
    farthest = -1
    for match in matches:
        if match.base[0] <= farthest:
            moved.add(match)
        farthest = max(farthest, match.base[-1])
    earliest = len(base)
    for match in reversed(matches):
        if match.base[-1] >= earliest:
            moved.add(match)
        earliest = min(earliest, match.base[0])
    return matches, moved, ambiguous


def _spans(fragments):
    return [fragment["span"] for fragment in fragments]


def _preview(fragments):
    return "\n".join(part["text"][:240] for part in fragments[:3])[:720]


def _row(kind, reasons, options, evidence, *, default=False):
    spans = [_spans(option) for option in options]
    row_id = identifier([kind, spans, evidence.get("spans")])
    choices = [{"id": identifier([row_id, ranges]), "spans": ranges,
                "preview": _preview(option)}
               for option, ranges in zip(options, spans)]
    return {"id": row_id, "kind": kind, "reason_codes": sorted(set(reasons)), "options": choices,
            "default_option_id": choices[0]["id"] if default else None, "evidence": evidence}


def _match_evidence(match, base, donor, *, moved=False):
    evidence = {"relation": match.relation, "base_spans": _spans([base[i] for i in match.base]),
                "spans": _spans([donor[i] for i in match.donor]), "moved": moved}
    if match.score is not None:
        evidence["score"] = round(match.score, 4)
    return evidence


def _informative_anchor(text):
    letters, count = set(), 0
    for char in text:
        if char.isalpha():
            letters.add(char.casefold())
            count += 1
            if count >= 24 and len(letters) >= 8:
                return True
    return False


def _donor_gaps(base, donor, matches, moved, ambiguous):
    occupied = {i for match in matches for i in match.donor}
    anchors = {match.donor[0]: match.base[0] for match in matches if match.relation == "identical_content"
               and match not in moved and _informative_anchor(donor[match.donor[0]]["text"])}
    additions, unplaced = [], []
    for match in matches:
        if match in moved and match.relation != "identical_content":
            unplaced.append(_unplaced([donor[i] for i in match.donor], "moved_match",
                                      {"matches": [_match_evidence(match, base, donor, moved=True)]}))
    i = 0
    while i < len(donor):
        if i in occupied:
            i += 1
            continue
        if i in ambiguous:
            positions = ambiguous[i]
            unplaced.append(_unplaced(donor[i:i + 1], "ambiguous_match",
                                      {"base_spans": _spans([base[p] for p in positions[:12]]),
                                       "related_positions": len(positions)}))
            i += 1
            continue
        end = i + 1
        while end < len(donor) and end not in occupied and end not in ambiguous and _contiguous(donor[end - 1], donor[end]):
            end += 1
        left, right = anchors.get(i - 1), anchors.get(end)
        if (left is not None and right == left + 1 and _contiguous(base[left], base[right])
                and _contiguous(donor[i - 1], donor[i]) and _contiguous(donor[end - 1], donor[end])):
            additions.append((right, donor[i:end], _spans([donor[i - 1], donor[end]])))
        else:
            unplaced.append(_unplaced(donor[i:end], "no_reliable_placement"))
        i = end
    return additions, unplaced


def _unplaced(fragments, reason, evidence=None):
    evidence = {**(evidence or {}), "spans": _spans(fragments), "preview": _preview(fragments)}
    return _row("unplaced", [reason], [[]], evidence)


def _base_rows(base, exact, conflicts, additions, unplaced):
    groups = []
    for match, donor in sorted(conflicts, key=lambda entry: entry[0].base):
        start, end = match.base[0], match.base[-1] + 1
        if groups and start < groups[-1][1]:
            old_start, old_end, matches = groups[-1]
            matches.append((match, donor))
            groups[-1] = old_start, max(end, old_end), matches
        else:
            groups.append((start, end, [(match, donor)]))
    clusters = {start: (end, matches) for start, end, matches in groups}
    internal_gaps = {gap for start, end, _ in groups for gap in range(start + 1, end)}

    for gap in sorted(internal_gaps & additions.keys()):
        for option, anchors in additions.pop(gap):
            unplaced.append(_unplaced(option, "overlapping_base_conflict", {"anchors": anchors}))

    rows, i = [], 0
    while i < len(base):
        if i in additions:
            options = [option for option, _ in additions[i]]
            evidence = {"anchors": _spans(base[i - 1:i + 1]),
                        "matches": [{"spans": _spans(option), "anchors": anchors} for option, anchors in additions[i]]}
            reasons = ["anchored_insertion"] + (["competing_donors"] if len(options) > 1 else [])
            rows.append(_row("addition", reasons, [*options, []], evidence))
        if i not in clusters:
            proofs = exact.get(i, [])
            reasons = (["identical_content"] + (["moved_match"] if any(p["moved"] for p in proofs) else [])) if proofs else ["base_only"]
            rows.append(_row("exact_duplicate" if proofs else "base", reasons, [base[i:i + 1]], {"matches": proofs}, default=True))
            i += 1
            continue

        end, matches = clusters[i]
        by_source = defaultdict(list)
        for match, donor in matches:
            by_source[donor[0]["span"]["source_id"]].append((match, donor))
        options = [base[i:end]]
        for changes in by_source.values():
            option, cursor = [], i
            for match, donor in changes:
                option.extend(base[cursor:match.base[0]])
                option.extend(donor[j] for j in match.donor)
                cursor = match.base[-1] + 1
            option.extend(base[cursor:end])
            options.append(option)
        evidence = {"matches": [_match_evidence(match, base, donor) for match, donor in matches]}
        evidence["matches"].extend(chain.from_iterable(exact.get(j, []) for j in range(i, end)))
        reasons = [match.relation for match, _ in matches] + (["competing_donors"] if len(by_source) > 1 else [])
        rows.append(_row("conflict", reasons, options, evidence))
        i = end
    return rows + unplaced


def plan_rows(base, donors, *, limits=None, check=None):
    limits = limits or CurationLimits()
    check = check or (lambda: None)
    sides = [base, *donors]
    reasons = set()
    if len(donors) > limits.max_donors:
        reasons.add("source_count_budget")
    if sum(map(len, sides)) > limits.max_blocks:
        reasons.add("fragment_budget")
    if sum(len(f["text"]) for side in sides for f in side) > limits.max_chars:
        reasons.add("character_budget")
    if reasons:
        return [], sorted(reasons)

    exact, conflicts, additions, unplaced = defaultdict(list), [], defaultdict(list), []
    budget = {"shingles": 0, "votes": 0, "cells": 0}
    for donor in donors:
        check()
        matches, moved, ambiguous = _pair(base, donor, limits, budget, reasons, check)
        for match in matches:
            if match.relation == "identical_content":
                exact[match.base[0]].append(_match_evidence(match, base, donor, moved=match in moved))
            elif match not in moved:
                conflicts.append((match, donor))
        inserted, unresolved = _donor_gaps(base, donor, matches, moved, ambiguous)
        for gap, fragments, anchors in inserted:
            additions[gap].append((fragments, anchors))
        unplaced.extend(unresolved)
    return _base_rows(base, exact, conflicts, additions, unplaced), sorted(reasons)
