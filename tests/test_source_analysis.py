import copy
import json

import pytest

from yuecang.application.service import Workbench
from yuecang.application.source_analysis import SourceAnalysis
from yuecang.core.source_analysis import AnalysisLimits, align_fragments, inspect_source, span
from yuecang.storage.project import DomainError


A1 = "城市植物手册记载幼苗每日需观察叶片颜色、土壤湿度和根部状态，避免连续大量浇水。"
A2 = "种植者应在清晨记录植株高度和新叶数量，根据当地季节安排移栽并保留观察日志。"
B3 = "远航记录第三节说明船员如何利用星图校验方向，夜间轮班需检查罗盘与甲板灯光。"


def block(identity, text, kind="paragraph"):
    return {"id": identity, "text": text, "type": kind}


def reference(identity, text, book="a", edition="a1"):
    return {"span": span("known-" + book, identity, 0, len(text)), "text": text,
            "book_id": book, "edition_id": edition}


def test_interleaved_a1_b3_a2_keeps_independent_range_evidence():
    blocks = [block("a1", A1), block("b3", B3), block("a2", A2)]
    refs = [reference("a1", A1), reference("a2", A2), reference("b3", B3, "b", "b1")]
    report = inspect_source("mixed", blocks, refs)
    assert [(m["span"]["block_id"], m["book_id"]) for m in report["matches"]] == [("a1", "a"), ("b3", "b"), ("a2", "a")]
    assert [b["position"]["block_id"] for b in report["boundaries"] if b["kind"] == "known_source_change"] == ["b3", "a2"]
    assert not report["automatic_assignment"] and not report["incomplete"]
    for item in report["matches"]:
        target = next(b for b in blocks if b["id"] == item["span"]["block_id"])
        evidence = item["evidence"][0]
        assert target["text"][item["span"]["start"]:item["span"]["end"]] == evidence["preview"]


def test_quotation_does_not_claim_book_boundary_or_whole_paragraph():
    text = "引用：“" + B3 + "”这段材料仅作为植物园参观报告的对照案例。"
    report = inspect_source("s", [block("a", A1), block("quote", text), block("a2", A2)],
                            [reference("a1", A1), reference("a2", A2), reference("b3", B3, "b", "b1")])
    quoted = next(m for m in report["matches"] if m["span"]["block_id"] == "quote")
    assert quoted["span"]["start"] == 4 and quoted["span"]["end"] < len(text)
    assert "引用" in quoted["uncertainty"]
    assert not any(b["kind"] == "known_source_change" for b in report["boundaries"])


def test_interleaved_books_inside_one_block_have_codepoint_locations():
    report = inspect_source("mixed", [block("all", A1 + B3 + A2)],
                            [reference("a1", A1), reference("a2", A2), reference("b3", B3, "b", "b1")])
    assert [b["position"]["start"] for b in report["boundaries"]] == [len(A1), len(A1 + B3)]
    assert report["boundaries"][0]["before"] == A1


def test_number_reset_is_uncertain_and_120_chapter_style_change_does_not_split():
    blocks = []
    for n in range(1, 121):
        blocks.extend([block(f"h{n}", f"第{n}回", "heading"), block(f"p{n}", A1 if n <= 80 else B3)])
    result = inspect_source("long", blocks, [])
    assert result["boundaries"] == [] and result["matches"] == []
    reset = inspect_source("reset", [block("3", "第三章", "heading"), block("1", "第一章", "heading")], [])
    assert reset["boundaries"][0]["kind"] == "numbering_reset"
    assert "同书" in reset["boundaries"][0]["uncertainty"]


def test_common_text_does_not_choose_one_book_or_edition():
    result = inspect_source("s", [block("x", A1)], [reference("x", A1), reference("y", A1, "b", "b1")])
    assert len(result["matches"]) == 2
    assert all("多个图书或版本" in m["uncertainty"] for m in result["matches"])
    assert result["boundaries"] == []


def test_match_cap_does_not_hide_competing_books_in_boundary_evidence():
    refs = [reference(f"a{n}", A1) for n in range(16)]
    refs += [reference("common", A1, "c", "c1"), reference("b3", B3, "b", "b1")]
    result = inspect_source("s", [block("a1", A1), block("b3", B3)], refs)
    assert len(result["matches"]) == 17
    assert "candidate_budget" in result["limit_reasons"] and result["boundaries"] == []
    limited = inspect_source("s", [block("a1", A1)], refs, limits=AnalysisLimits(max_vote_hits=1))
    assert limited["matches"] == [] and "anchor_budget" in limited["limit_reasons"]


def test_budgets_are_explicit_and_short_headings_are_not_identity():
    report = inspect_source("s", [block("one", A1), block("two", A2)], [], limits=AnalysisLimits(max_blocks=1))
    assert report["incomplete"] and report["analyzed_blocks"] == 1
    assert not inspect_source("s", [block("heading", "第一章")], [reference("x", "第一章")])["matches"]
    limited = inspect_source("s", [block("x", A1)], [reference("x", A1)], limits=AnalysisLimits(max_pair_cells=1))
    assert limited["incomplete"] and "comparison_budget" in limited["limit_reasons"]


def test_sampled_reference_anchors_match_shifted_long_text():
    text = "".join(chr(0x4E00 + n) for n in range(400))
    report = inspect_source("s", [block("x", "😀" + text)], [reference("x", text)])
    assert report["matches"][0]["span"]["start"] == 1
    assert report["matches"][0]["evidence"][0]["match_chars"] == 400


@pytest.fixture
def analysis_library(tmp_path):
    wb = Workbench(tmp_path / "library")

    def source(name, text):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return wb.source(wb.import_file(path)["source_id"])

    return wb, source


def selections(source):
    return [span(source["id"], b["id"], 0, len(b["text"])) for b in source["blocks"]]


def test_application_never_propagates_unconfirmed_candidates(analysis_library):
    wb, source = analysis_library
    known = source("known.txt", A1)
    mixed = source("mixed.txt", A1 + "\n\n" + B3)
    unrelated = source("other.txt", B3)
    book = wb.create_book("合成植物书")
    wb.assign(book["id"], book["edition_id"], selections(known))
    before = wb.state()["assignments"]
    service = SourceAnalysis(wb)
    assert service.inspect(mixed["id"])["matches"]
    assert service.inspect(unrelated["id"])["matches"] == []
    assert wb.state()["assignments"] == before


def test_retrieval_prioritizes_direct_match_over_earliest_assignment(analysis_library):
    wb, source = analysis_library
    older, newer = source("old.txt", A1), source("new.txt", B3)
    a, b = wb.create_book("旧来源"), wb.create_book("新来源")
    wb.assign(a["id"], a["edition_id"], selections(older))
    wb.assign(b["id"], b["edition_id"], selections(newer))
    target = source("target.txt", B3 + "附记")
    report = SourceAnalysis(wb, limits=AnalysisLimits(max_references=1)).inspect(target["id"])
    assert report["matches"][0]["book_id"] == b["id"]
    assert report["incomplete"] and "reference_loading_budget" in report["limit_reasons"]


def test_analysis_checks_job_lease_before_loading_data(analysis_library):
    wb, source = analysis_library
    target = source("target.txt", A1)
    with pytest.raises(DomainError) as exc:
        SourceAnalysis(wb).inspect(target["id"], job={"id": "missing", "token": "invalid"})
    assert exc.value.code == "stale_job"


def test_malformed_review_and_alignment_options_are_domain_errors(analysis_library):
    wb, _ = analysis_library
    service = SourceAnalysis(wb)
    with pytest.raises(DomainError) as exc:
        service.set_review_state("source", "boundary", [])
    assert exc.value.code == "boundary_state"
    with pytest.raises(DomainError) as exc:
        service.align({}, "edition", "left", "right")
    assert exc.value.code == "alignment_scope"


def test_same_edition_alignment_and_explicit_selection_provenance(analysis_library):
    wb, source = analysis_library
    left = source("left.txt", A1 + "\n\n" + A2 + "\n\n" + B3)
    right = source("right.txt", A1 + "\n\n" + A2.replace("清晨", "傍晚") + "\n\n" + B3)
    book = wb.create_book("合成手册")
    for item in [left, right]:
        wb.assign(book["id"], book["edition_id"], selections(item))
    result = SourceAnalysis(wb).align(book["id"], book["edition_id"], left["id"], right["id"])
    assert [r["kind"] for r in result["rows"]] == ["exact_duplicate", "different_text", "exact_duplicate"]
    chosen = [s for row in result["rows"] for s in (row["right"] if row["kind"] == "different_text" else row["left"])]
    draft = wb.assemble(book["id"], chosen, edition_id=book["edition_id"])
    wb.validate_revision(draft["revision"])
    assert "傍晚" in draft["revision"]["blocks"][1]["text"]
    assert not result["automatic_merge"]


def test_alignment_rejects_other_edition_and_unassigned_sources(analysis_library):
    wb, source = analysis_library
    left, right = source("left.txt", A1), source("right.txt", A2)
    book = wb.create_book("同作品")
    other = wb.create_edition(book["id"], "另一版本")
    wb.assign(book["id"], book["edition_id"], selections(left))
    wb.assign(book["id"], other["id"], selections(right))
    with pytest.raises(DomainError, match="双方来源"):
        SourceAnalysis(wb).align(book["id"], book["edition_id"], left["id"], right["id"])


def test_alignment_unions_overlapping_assignments_without_filling_gaps(analysis_library):
    wb, source = analysis_library
    text = A1 + A2
    left = source("left.txt", text)
    right = source("right.txt", text + "不同结尾")
    book = wb.create_book("合成手册")
    spans = []
    for side in (left, right):
        first = span(side["id"], side["blocks"][0]["id"], 0, 30)
        second = {**first, "start": 20, "end": 40}
        third = {**first, "start": 42, "end": len(text)}
        wb.assign(book["id"], book["edition_id"], [first, second, third])
        spans.append([{**first, "end": 40}, third])
    result = SourceAnalysis(wb).align(book["id"], book["edition_id"], left["id"], right["id"])
    assert [r["kind"] for r in result["rows"]] == ["exact_duplicate", "exact_duplicate"]
    assert [[row[side][0] for row in result["rows"]] for side in ("left", "right")] == spans


def test_alignment_duplicates_missing_ranges_and_resources_are_not_silently_chosen():
    def f(source, identity, text, **extra):
        return {"span": span(source, identity, 0, len(text)), "text": text, **extra}
    repeated = align_fragments([f("l", "a", A1), f("l", "b", A1)], [f("r", "a", A1)])
    assert repeated["rows"][0]["kind"] == "ambiguous"
    assert any(r["kind"] == "left_only" for r in repeated["rows"])
    resources = align_fragments([f("l", "a", "", type="figure", resources=[{"sha256": "a"}])],
                                [f("r", "a", "", type="figure", resources=[{"sha256": "b"}])])
    assert resources["rows"][0]["kind"] == "ambiguous"
    budget = align_fragments([f("l", "a", A1)], [f("r", "a", A1)], limits=AnalysisLimits(max_alignment_cells=0))
    assert budget["incomplete"] and not budget["rows"]


def test_alignment_uses_attachment_identity_and_returns_rows_up_to_the_limit():
    def fragment(source, identity, text, resources=None):
        return {"span": span(source, identity, 0, len(text)), "text": text, "resources": resources or []}

    left = [fragment("l", "image", "![](same.png)", [{"sha256": "left"}]), fragment("l", "a", A1)]
    right = [fragment("r", "image", "![](same.png)", [{"sha256": "right"}]), fragment("r", "a", A1)]
    assert [row["kind"] for row in align_fragments(left, right)["rows"]] == ["ambiguous", "exact_duplicate"]
    right[0]["resources"] = left[0]["resources"]
    limited = align_fragments(left, right, limits=AnalysisLimits(max_matches=1))
    assert [row["kind"] for row in limited["rows"]] == ["exact_duplicate"]
    assert limited["incomplete"] and limited["limit_reasons"] == ["alignment_row_budget"]


def test_frozen_offline_review_and_decisions_never_assign(analysis_library):
    wb, source = analysis_library
    mixed = source("mixed.txt", "《合成植物》\n\n" + A1 + "\n\n《合成航行》\n\n" + B3)
    service = SourceAnalysis(wb)
    report = service.inspect(mixed["id"])
    bid = report["boundaries"][0]["id"]
    assert service.set_review_state(mixed["id"], bid, "ignored")["state"] == "ignored"
    assert service.inspect(mixed["id"])["boundaries"][0]["state"] == "ignored"
    request = service.create_review(mixed["id"], [bid])
    assert service.project.read_json(request["request"]["report_sha"])["blocks_sha"] == mixed["blocks_sha"]
    response = {"request_id": request["id"], "opinions": [{"boundary_id": bid, "verdict": "same_work", "reason": "仅有书名标记，仍可能是引用，需人工核查。"}]}
    original = copy.deepcopy(wb.source(mixed["id"])["blocks"])
    for _ in range(2):
        result = service.receive_review(request["id"], response)
        assert result["status"] == "completed" and not result["applied"]
    assert not wb.state()["assignments"] and wb.source(mixed["id"])["blocks"] == original
    assert service.inspect(mixed["id"])["boundaries"][0]["state"] == "ignored"
    with wb.project.connect() as db:
        assert db.execute("SELECT count(*) FROM object_refs WHERE owner='source_review' AND owner_id=?", (request["id"],)).fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM events WHERE kind='source.review_received' AND object_id=?", (request["id"],)).fetchone()[0] == 1
    response["opinions"][0]["verdict"] = "possible_mixture"
    with pytest.raises(DomainError, match="已有结果"):
        service.receive_review(request["id"], response)


@pytest.mark.parametrize("change", ["request", "boundary", "verdict", "extra", "missing", "long_reason", "surrogate"])
def test_offline_review_rejects_scope_and_schema_errors(analysis_library, change):
    wb, source = analysis_library
    mixed = source("mixed.txt", "开头\n\n《另一题名》\n\n" + B3)
    service = SourceAnalysis(wb)
    request = service.create_review(mixed["id"])
    response = {"request_id": request["id"], "opinions": [{"boundary_id": request["request"]["boundaries"][0]["id"], "verdict": "uncertain", "reason": "证据不足"}]}
    if change == "request": response["request_id"] = "foreign"
    if change == "boundary": response["opinions"][0]["boundary_id"] = "foreign"
    if change == "verdict": response["opinions"][0]["verdict"] = ["same_work"]
    if change == "extra": response["opinions"][0]["assign_book"] = "foreign"
    if change == "missing": response["opinions"] = []
    if change == "long_reason": response["opinions"][0]["reason"] = "证据不足" + " " * 1000
    if change == "surrogate": response["opinions"][0]["reason"] = "\ud800"
    with pytest.raises(DomainError):
        service.receive_review(request["id"], response)
    assert service.get_review(request["id"])["status"] == "ready"
