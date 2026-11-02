from __future__ import annotations

import random

import pytest

from yuecang.core.fingerprints import build_fingerprint, compare_fingerprints, validate_fingerprint
from yuecang.core.hashing import canonical_json, iter_canonical_json, sha256_json, sha256_text
from yuecang.core.parsing import chapter_heading_ordinal, parse_chinese_number, parse_source_name
from yuecang.core.text_diff import compare_stories, compare_texts, iter_difference_pages


def test_stable_json_is_unicode_sorted_and_rejects_nan():
    payload = {"中文": [1, {"b": 2, "a": "值"}], "a": True}
    assert canonical_json(payload) == ''.join(iter_canonical_json(payload))
    assert sha256_json(payload) == sha256_text(canonical_json(payload))
    assert sha256_json(payload) == sha256_json(dict(reversed(list(payload.items()))))
    with pytest.raises(ValueError):
        canonical_json({"bad": float("nan")})


@pytest.mark.parametrize("left,right", [("x+y=2", "x-y=2"), ("A()", "a()"), ("a b", "ab"), ("if x:\n  run()", "if x:\nrun()")])
def test_matching_fingerprint_is_not_proof_of_equivalence(left, right):
    result = compare_fingerprints(build_fingerprint(left), build_fingerprint(right))
    assert result.relation == "normalization_candidate"
    assert result.exact_body is False


def test_fingerprint_hash_is_verified_and_tamper_detected():
    original = build_fingerprint("第十二章\n数学：x+y=2。\n")
    assert compare_fingerprints(original, original).relation == "exact_duplicate"
    with pytest.raises(ValueError, match="source_body_hash_mismatch"):
        build_fingerprint("different", source_body_sha256=original["source_body_sha256"])
    original["normalized_chars"] = 0
    with pytest.raises(ValueError):
        validate_fingerprint(original)


@pytest.mark.parametrize("value,expected", [("十二", 12), ("两百零三", 203), ("二〇二六", 2026), ("一万零二", 10002), ("１２", 12), ("bad", None)])
def test_numbers(value, expected):
    assert parse_chinese_number(value) == expected


def test_filename_is_only_a_candidate_and_keeps_ambiguity():
    result = parse_source_name("教材第十二章-第十五章(未知版).txt")
    assert result.canonical_title == "教材"
    assert (result.ordinal_start, result.ordinal_end) == (12, 15)
    assert "suffix_preserved_in_stem" in result.issue_codes
    assert "未知版" in result.stem
    assert "copy_suffix_ambiguity" in parse_source_name("原理(2).txt").issue_codes
    assert chapter_heading_ordinal("第二十三节 例题") == 23


def _assert_difference_coverage(left, right, comparison):
    assert comparison["complete_difference_coverage"] is True
    l_cursor = r_cursor = 0
    for row in comparison.get("opcodes", []):
        a, b = row["left_span"]
        c, d = row["right_span"]
        assert left[l_cursor:a] == right[r_cursor:c]
        assert row["left_sha256"] == sha256_text(left[a:b])
        assert row["right_sha256"] == sha256_text(right[c:d])
        l_cursor, r_cursor = b, d
    assert left[l_cursor:] == right[r_cursor:]


def test_diff_codepoints_and_full_difference_coverage():
    for left, right in [("α😀中abc结尾", "α😀文abcd结尾"), ("prefix", "prefix plus"), ("abcXYZdef", "abcQdef")]:
        comparison = compare_texts(left, right)
        assert comparison["automatic_equivalence"] is False
        _assert_difference_coverage(left, right, comparison)
    assert compare_texts("same", "same")["automatic_equivalence"] is True
    assert compare_texts("", "a")["automatic_equivalence"] is False


def test_layout_and_html_views_never_authorize_equivalence():
    result = compare_stories("if x:\n    run()", "if x:\nrun()")
    assert result["automatic_equivalence"] is False
    assert result["relation"] == "normalization_candidate"
    result = compare_stories("<b>a</b>", "a", converted_left="a", converted_right="a")
    assert result["automatic_equivalence"] is False
    assert result["offset_space"] == "normalized_matching_view_codepoints"


def test_many_differences_are_paged_without_truncation():
    rng = random.Random(72)
    pieces = ["".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=40)) for _ in range(540)]
    left = "".join(part + "甲" for part in pieces)
    right = "".join(part + "乙" for part in pieces)
    result = compare_texts(left, right)
    _assert_difference_coverage(left, right, result)
    pages = list(iter_difference_pages(result, page_size=20))
    assert len(result["opcodes"]) >= 540
    assert sum(len(page["opcodes"]) for page in pages) == len(result["opcodes"])
    assert len({page["comparison_sha256"] for page in pages}) == 1
    assert pages[-1]["page_index"] + 1 == pages[-1]["page_count"]
    with pytest.raises(ValueError):
        list(iter_difference_pages(result, page_size=0))
