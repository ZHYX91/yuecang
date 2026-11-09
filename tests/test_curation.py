from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from yuecang.application.curation import Curation
from yuecang.application.service import Workbench
from yuecang.core.curation import CurationLimits, plan_rows
from yuecang.core.source_analysis import span
from yuecang.jobs import JobQueue
from yuecang.storage.project import DomainError


A = "渡船将在晨雾散去之后启程，旅客先依照登船名单核对随身行李和领取临时通行卡。"
B = "馆员将经潮湿天气侵蚀的账册移入干燥房间，待纸张重新平整后逐页记录损坏程度。"
C = "山路尽头的石碑刻有不同年代的地名，调查者把每种写法分别抄入原始记录和附图。"
D = "农事观察日志记录每月播种、浇水和除草的日期，季末按作物类型比较生长情况。"


def side(source_id, *texts):
    result = []
    for i, value in enumerate(texts):
        block = {"type": "paragraph", "text": value} if isinstance(value, str) else value
        length = len(block["text"])
        result.append({**block, "span": span(source_id, str(i), 0, length), "ordinal": i, "block_length": length})
    return result


@pytest.fixture
def library(tmp_path):
    wb = Workbench(tmp_path / "library")
    book = wb.create_book("合成书")

    def source(name, *paragraphs, assign=True):
        path = tmp_path / name
        path.write_text("\n\n".join(paragraphs), encoding="utf-8", newline="\n")
        data = wb.source(wb.import_file(path)["source_id"])
        if assign:
            selections = [span(data["id"], b["id"], 0, len(b["text"])) for b in data["blocks"]]
            wb.assign(book["id"], book["edition_id"], selections)
        return data

    return wb, book, source


def create(service, book, base, *donors, **kwargs):
    return service.create(book["id"], book["edition_id"], base["id"], [d["id"] for d in donors], **kwargs)


def test_confirm_multisource_options_and_preserve_provenance(library):
    wb, book, source = library
    base = source("base.txt", A, B, C, D)
    donor = source("donor.txt", A, "只见记录员带来了备用笔记本。", B, C.replace("石碑", "木牌"), D)
    other = source("other.txt", A, "随行人员另备相机和电池。", B, C, D)
    service = Curation(wb)
    plan = create(service, book, base, donor, other)
    assert plan == service.get(plan["id"])
    assert not plan["incomplete"]
    assert [r["kind"] for r in plan["rows"]] == ["exact_duplicate", "addition", "exact_duplicate", "conflict", "exact_duplicate"]
    _, addition, _, conflict, _ = plan["rows"]
    assert len(addition["options"]) == 3 and len(conflict["options"]) == 2
    assert all(row["default_option_id"] is None for row in (addition, conflict))
    assert addition["evidence"]["anchors"] == [span(base["id"], b["id"], 0, len(b["text"])) for b in base["blocks"][:2]]
    original_assignments = wb.state()["assignments"]
    choices = {addition["id"]: addition["options"][1]["id"], conflict["id"]: conflict["options"][1]["id"]}
    for invalid in ({}, {**choices, "foreign-row": "foreign-option"}, {**choices, addition["id"]: conflict["options"][1]["id"]}):
        with pytest.raises(DomainError) as exc:
            service.assemble(plan["id"], plan["snapshot_sha"], invalid)
        assert exc.value.code == "curation_choices"

    draft = service.assemble(plan["id"], plan["snapshot_sha"], choices)
    wb.validate_revision(draft["revision"])
    blocks = draft["revision"]["blocks"]
    assert [b["text"] for b in blocks] == [base["blocks"][0]["text"], other["blocks"][1]["text"],
                                            base["blocks"][1]["text"], donor["blocks"][3]["text"], base["blocks"][3]["text"]]
    assert [b["source_span"]["source_id"] for b in blocks] == [base["id"], other["id"], base["id"], donor["id"], base["id"]]
    assert wb.state()["assignments"] == original_assignments
    assert service.assemble(plan["id"], plan["snapshot_sha"], choices)["revision_id"] == draft["revision_id"]
    assert service.get(plan["id"])["result"]["revision_id"] == draft["revision_id"]
    with pytest.raises(DomainError) as exc:
        service.assemble(plan["id"], plan["snapshot_sha"], {**choices, addition["id"]: addition["options"][-1]["id"]})
    assert exc.value.code == "curation_applied"
    with wb.project.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM object_refs WHERE owner='curation_plan' AND owner_id=? AND role='payload'",
                          (plan["id"],)).fetchone()[0] == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_split_and_merge_ocr_keep_raw_codepoint_spans(reverse):
    left = side("l", A + B)
    right = side("r", A[:18], A[18:] + B[:8], B[8:].replace("平整", "乎整"))
    if reverse:
        left, right = right, left
    rows, reasons = plan_rows(left, [right])
    assert not reasons
    assert len(rows) == 1 and rows[0]["kind"] == "conflict"
    assert rows[0]["reason_codes"] == ["split_or_merge"]
    assert rows[0]["options"][0]["spans"] == [f["span"] for f in left]
    assert rows[0]["options"][1]["spans"] == [f["span"] for f in right]


def test_repeated_and_moved_passages_cannot_anchor_insertions():
    base = side("base", A, B, A, C, D)
    donor = side("donor", C, "找不到相邻位置的补充。", B, A, D)
    rows, reasons = plan_rows(base, [donor])
    assert not reasons
    assert [r["options"][0]["spans"][0] for r in rows[:5]] == [f["span"] for f in base]
    assert {tuple(r["reason_codes"]) for r in rows[5:]} == {("no_reliable_placement",), ("ambiguous_match",)}
    assert all(r["options"][0]["spans"] == [] and r["default_option_id"] is None for r in rows[5:])
    assert all("moved_match" in rows[i]["reason_codes"] for i in (1, 3))


def test_exact_base_neighbors_across_unassigned_blocks_do_not_anchor_insertion():
    base = side("base", A, C)
    base[1]["ordinal"] = 2
    rows, reasons = plan_rows(base, [side("donor", A, B, C)])
    assert not reasons
    assert [r["kind"] for r in rows] == ["exact_duplicate", "exact_duplicate", "unplaced"]


@pytest.mark.parametrize("kind,left,right", [
    ("paragraph", "。", "！"),
    ("paragraph", "A", "B"),
    ("heading", "第一章", "第二章"),
    ("paragraph", "嗯" * 30, "啊" * 30),
])
def test_weak_exact_matches_do_not_anchor_donor_material(kind, left, right):
    anchors = [{"type": kind, "text": text} for text in (left, right)]
    rows, reasons = plan_rows(side("l", *anchors), [side("r", anchors[0], D, anchors[1])])
    assert not reasons
    assert [r["kind"] for r in rows] == ["exact_duplicate", "exact_duplicate", "unplaced"]
    assert rows[-1]["reason_codes"] == ["no_reliable_placement"]


def test_resource_changes_require_an_atomic_conflict():
    figure = {"type": "figure", "text": "", "resources": [{"sha256": "a"}]}
    other = {**figure, "resources": [{"sha256": "b"}]}
    rows, reasons = plan_rows(side("l", figure), [side("r", other)])
    assert not reasons
    assert rows[0]["kind"] == "conflict" and rows[0]["reason_codes"] == ["structure_variation"]
    assert [opt["spans"] for opt in rows[0]["options"]] == [[span("l", "0", 0, 0)], [span("r", "0", 0, 0)]]


def test_reciprocal_margin_abstains_for_two_equally_close_positions():
    base = side("l", A.replace("晨雾", "风雪"), A.replace("晨雾", "雨幕"))
    rows, reasons = plan_rows(base, [side("r", A)])
    assert not reasons
    assert [r["kind"] for r in rows] == ["base", "base", "unplaced"]
    assert rows[-1]["reason_codes"] == ["ambiguous_match"]


def test_near_duplicate_of_matched_base_is_not_an_anchored_addition():
    base = side("l", A, B, C)
    donor = side("r", A, B, A.replace("晨雾", "风雪"), C)
    rows, reasons = plan_rows(base, [donor])
    assert not reasons
    assert [r["kind"] for r in rows] == ["exact_duplicate", "exact_duplicate", "exact_duplicate", "unplaced"]
    assert rows[-1]["evidence"]["base_spans"] == [base[0]["span"]]


def test_below_threshold_text_relation_cannot_be_an_anchored_addition():
    original = "".join(chr(0x4E00 + i) for i in range(50))
    # Eight of 50 unique codepoints differ, leaving a .84 similarity with
    # enough unchanged shingles to recall the already matched base passage.
    variant = "".join("□" if i % 6 == 5 else char for i, char in enumerate(original))
    base = side("l", original, B, C)
    rows, reasons = plan_rows(base, [side("r", original, B, variant, C)])
    assert not reasons
    assert [r["kind"] for r in rows] == ["exact_duplicate", "exact_duplicate", "exact_duplicate", "unplaced"]
    assert rows[-1]["reason_codes"] == ["ambiguous_match"]
    assert rows[-1]["evidence"]["base_spans"] == [base[0]["span"]]


def test_shingle_examination_budget_is_visible_without_any_posting_hits():
    _, reasons = plan_rows(side("l", A), [side("r", C)], limits=CurationLimits(max_query_shingles=0))
    assert reasons == ["shingle_budget"]


def test_repeated_near_match_with_saturated_postings_stays_unplaced():
    repeated = C + D
    base = side("l", *([repeated] * 18), A, B)
    rows, reasons = plan_rows(base, [side("r", A, repeated.replace("石碑", "木牌"), B)])
    assert not reasons
    assert all(row["kind"] != "addition" for row in rows)
    assert rows[-1]["kind"] == "unplaced" and rows[-1]["reason_codes"] == ["ambiguous_match"]
    assert rows[-1]["evidence"]["base_spans"]


def test_incomplete_and_other_edition_inputs_are_rejected(library):
    wb, book, source = library
    base = source("base.txt", A)
    donor = source("donor.txt", A.replace("晨雾", "风雪"))
    service = Curation(wb, limits=CurationLimits(max_total_cells=0))
    plan = create(service, book, base, donor)
    assert plan["incomplete"] and "comparison_budget" in plan["limit_reasons"]
    with pytest.raises(DomainError) as exc:
        service.assemble(plan["id"], plan["snapshot_sha"], {})
    assert exc.value.code == "curation_incomplete"
    outsider = source("outside.txt", C, assign=False)
    edition = wb.create_edition(book["id"], "另一版本")
    wb.assign(book["id"], edition["id"], [span(outsider["id"], outsider["blocks"][0]["id"], 0, len(C))])
    with pytest.raises(DomainError) as exc:
        create(service, book, base, outsider)
    assert exc.value.code == "curation_scope"


def test_explicit_composite_copies_only_same_book_assignments_and_preserves_edition_provenance(library):
    wb, book, source = library
    tail = "整理完成后，记录员把纸本和照片交回档案室。"
    base = source("base.txt", A, B, C, tail)
    donor = source("donor.txt", A, B.replace("馆员", "书商"), C, D, tail, assign=False)
    donor_spans = [span(donor["id"], b["id"], 0, len(b["text"])) for b in donor["blocks"]]
    alternate = wb.create_edition(book["id"], "补充本")
    another = wb.create_edition(book["id"], "另一整理本")
    foreign = wb.create_book("另一作品")
    wb.assign(book["id"], alternate["id"], [donor_spans[i] for i in (0, 1, 2, 4)])
    wb.assign(book["id"], another["id"], [donor_spans[1]])
    wb.assign(foreign["id"], foreign["edition_id"], [donor_spans[3]])
    service = Curation(wb)

    for args, kwargs in (((base, donor), {}), ((donor, base), {"allow_composite": True}),
                         ((base, donor), {"allow_composite": 1})):
        with pytest.raises(DomainError) as exc:
            create(service, book, *args, **kwargs)
        assert exc.value.code == "curation_scope"

    assignments = wb.state()["assignments"]
    plan = create(service, book, base, donor, allow_composite=True)
    assert plan["allow_composite"] and not plan["incomplete"]
    assert plan["source_editions"][donor["id"]] == sorted([alternate["id"], another["id"]])
    assert plan["editions"][alternate["id"]]["label"] == "补充本"
    assert [r["kind"] for r in plan["rows"]] == ["exact_duplicate", "conflict", "exact_duplicate", "exact_duplicate"]
    conflict = plan["rows"][1]
    selected = conflict["options"][1]
    assert selected["spans"][0]["edition_ids"] == sorted([alternate["id"], another["id"]])
    assert selected["spans"][0]["edition_ranges"] == [
        {"edition_id": eid, "start": donor_spans[1]["start"], "end": donor_spans[1]["end"]}
        for eid in sorted([alternate["id"], another["id"]])]
    choices = {conflict["id"]: selected["id"]}
    draft = service.assemble(plan["id"], plan["snapshot_sha"], choices)
    assert draft["revision"]["edition_id"] == book["edition_id"]
    assert draft["revision"]["metadata"]["assembly_policy"] == "composite-confirmed"
    assert draft["revision"]["blocks"][1]["source_span"] == donor_spans[1]
    assert {issue["severity"] for issue in draft["revision"]["issues"] if issue["code"] == "edition_conflict"} == {"warning"}
    assert all(D not in b["text"] for b in draft["revision"]["blocks"])
    assert wb.state()["assignments"] == assignments
    assert service.assemble(plan["id"], plan["snapshot_sha"], choices)["revision_id"] == draft["revision_id"]


def test_composite_freezes_clipped_base_edition_ranges_in_revision(library):
    wb, book, source = library
    base = source("base.txt", A, B, assign=False)
    donor = source("donor.txt", A[4:], B, assign=False)
    alternate = wb.create_edition(book["id"], "补充本")
    base_spans = [span(base["id"], b["id"], 0, len(b["text"])) for b in base["blocks"]]
    wb.assign(book["id"], book["edition_id"], [{**base_spans[0], "start": 4}, base_spans[1]])
    wb.assign(book["id"], alternate["id"], [{**base_spans[0], "end": 12}])
    wb.assign(book["id"], alternate["id"], [span(donor["id"], b["id"], 0, len(b["text"])) for b in donor["blocks"]])
    service = Curation(wb)
    plan = create(service, book, base, donor, allow_composite=True)
    assert all(row["kind"] == "exact_duplicate" for row in plan["rows"])
    ranges = plan["rows"][0]["options"][0]["spans"][0]["edition_ranges"]
    assert {r["edition_id"]: (r["start"], r["end"]) for r in ranges} == {
        book["edition_id"]: (4, base_spans[0]["end"]), alternate["id"]: (4, 12)}

    revision = service.assemble(plan["id"], plan["snapshot_sha"], {})["revision"]
    assert revision["metadata"]["source_editions"][alternate["id"]]["label"] == "补充本"
    assert revision["blocks"][0]["source_edition_ranges"] == [
        {"source_id": base["id"], "block_id": base_spans[0]["block_id"], **r} for r in ranges]


@pytest.mark.parametrize("drift", ["edition_metadata", "foreign_overlap"])
def test_composite_rejects_relevant_scope_drift(library, drift):
    wb, book, source = library
    base = source("base.txt", A, B)
    donor = source("donor.txt", A.replace("晨雾", "风雪"), B, assign=False)
    alternate = wb.create_edition(book["id"], "补充本")
    donor_spans = [span(donor["id"], b["id"], 0, len(b["text"])) for b in donor["blocks"]]
    wb.assign(book["id"], alternate["id"], donor_spans)
    service = Curation(wb)
    plan = create(service, book, base, donor, allow_composite=True)
    choices = {r["id"]: r["options"][0]["id"] for r in plan["rows"] if not r["default_option_id"]}

    if drift == "foreign_overlap":
        foreign = wb.create_book("另一作品")
        wb.assign(foreign["id"], foreign["edition_id"], [donor_spans[0]])
        with pytest.raises(DomainError) as exc:
            create(service, book, base, donor, allow_composite=True)
        assert exc.value.code == "curation_scope"
    else:
        with wb.project.transaction() as db:
            db.execute("UPDATE editions SET metadata=? WHERE id=?", ('{"note":"更新记录"}', alternate["id"]))

    with pytest.raises(DomainError) as exc:
        service.assemble(plan["id"], plan["snapshot_sha"], choices)
    assert exc.value.code == "curation_stale"
    assert service.get(plan["id"])["state"] == "ready"
    with wb.project.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0


def test_assemble_checks_exact_assignment_rows_again_during_commit(library, monkeypatch):
    wb, book, source = library
    base = source("base.txt", A, B)
    donor = source("donor.txt", A, B, D, assign=False)
    selections = [span(donor["id"], b["id"], 0, len(b["text"])) for b in donor["blocks"][:2]]
    wb.assign(book["id"], book["edition_id"], selections)
    service = Curation(wb)
    plan = create(service, book, base, donor)
    original = wb.assemble

    def drift(*args, **kwargs):
        # This new assignment is contained in existing coverage. Hashing only
        # the union would fail to notice that the frozen evidence changed.
        wb.assign(book["id"], book["edition_id"], [{**selections[0], "end": 10}])
        return original(*args, **kwargs)

    monkeypatch.setattr(wb, "assemble", drift)
    choices = {r["id"]: r["options"][0]["id"] for r in plan["rows"] if not r["default_option_id"]}
    with pytest.raises(DomainError) as exc:
        service.assemble(plan["id"], plan["snapshot_sha"], choices)
    assert exc.value.code == "curation_stale"
    assert service.get(plan["id"])["state"] == "ready"
    with wb.project.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0


def test_simultaneous_identical_assembly_reuses_committed_result(library, monkeypatch):
    wb, book, source = library
    base = source("base.txt", A)
    donor = source("donor.txt", A, D)
    service = Curation(wb)
    plan = create(service, book, base, donor)
    choices = {r["id"]: r["options"][0]["id"] for r in plan["rows"] if not r["default_option_id"]}
    original = wb.assemble
    barrier = Barrier(2, timeout=5)

    def simultaneous(*args, **kwargs):
        barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(wb, "assemble", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        drafts = [pool.submit(service.assemble, plan["id"], plan["snapshot_sha"], choices) for _ in range(2)]
        assert len({draft.result()["revision_id"] for draft in drafts}) == 1
    with wb.project.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1


def test_job_retry_reuses_plan_and_old_token_cannot_write(library):
    wb, book, source = library
    base = source("base.txt", A)
    donor = source("donor.txt", A, B)
    queue = JobQueue(wb.project)
    queued = queue.enqueue("curation_plan", {"book_id": book["id"]})
    job = queue.claim(lease_seconds=60)
    service = Curation(wb)
    assert job["id"] == queued["id"]
    plan = create(service, book, base, donor, job=job)
    assert plan == create(service, book, base, donor, job=job)
    queue.pause(job["id"])
    with pytest.raises(DomainError) as exc:
        create(service, book, base, donor, job=job)
    assert exc.value.code == "stale_job"
