"""Independent synthetic evaluation of guided organization, curation, and catalog lookup.

Run with an installed YueCang and explicit fresh paths for --work-dir and --output.
Only aggregate measurements and case labels are written to the JSON report.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from io import BytesIO
import json
from pathlib import Path
import platform
from time import perf_counter, time
from xml.sax.saxutils import escape
from zipfile import ZipFile

from fastapi.testclient import TestClient
from PIL import Image

from yuecang import __version__
from yuecang.api import create_app
from yuecang.application.curation import Curation
from yuecang.application.organization import Organization
from yuecang.application.service import Workbench
from yuecang.core.source_analysis import span
from yuecang.formats.common import ExtractionResult
from yuecang.storage.project import DomainError, canonical


COMMON = "退水后的泥面不能作为测量基准，工作人员必须先找到连续露出三日的旧水线，再用水准尺记录两条线之间的高差。"
REVISED_COMMON = "样品编号写在封签的内侧，交接人员只核对封签是否完整，登记表另存于值班柜的第二层抽屉。"
WATER = "船闸停电时，巡检员应先合上备用配电柜的隔离开关，再到下游廊道查看排水泵的指示灯。"
WATER_FRAG = "复位闸位传感器之前，先把齿条上的碎冰扫入收集桶；交接记录须注明冻雨持续时长以及未复位的区段。"
WATER_NEW = "新装的水位记录器每隔十一分钟回传一组数据，值班员需在换班前将断点标注在同一张潮汐表上。"
MUSEUM = "翻修完成的展柜须空置两晚，再把试纸沿底板四角分别压紧；清晨拆取时应逐张装入带批号的纸袋。"
HERBARIUM = "将紫苏的叶片平放在无酸衬纸上，覆以透气棉布，装订之前用侧光检查是否仍能看见细小的叶脉。"
UNKNOWN = "车站寄存柜会在末班车离开后锁闭，尚未取件的乘客可以凭票面背后的编号到翌日的服务台办理登记。"
NUM = "挂在围栏内侧的限载牌标明每次只可吊运36千克，称重员记下读数后将空吊具送回检修棚。"
NUM_VARIANT = NUM.replace("36", "38")
OCR = "维修人员拆开电池舱后，必须先擦净锈屑并核对接线端子的色标，再重新拧紧接地线上的铜螺母。"
OCR_VARIANT = OCR.replace("锈屑", "绣屑")


def load(wb, root, name, parts):
    path = root / name
    path.write_bytes("\n\n".join(parts).encode("utf-8"))
    return wb.source(wb.import_file(path)["source_id"])


def whole(source):
    return [span(source["id"], block["id"], 0, len(block["text"])) for block in source["blocks"]]


def region(source, text):
    hits = [(block, block["text"].index(text)) for block in source["blocks"] if text in block["text"]]
    assert len(hits) == 1 and hits[0][0]["text"].count(text) == 1, text
    block, start = hits[0]
    return span(source["id"], block["id"], start, start + len(text))


def paragraph(source, text):
    block, = [block for block in source["blocks"] if block["text"].strip() == text]
    return span(source["id"], block["id"], 0, len(block["text"]))


def coordinates(value):
    return tuple(value[key] for key in ("source_id", "block_id", "start", "end"))


def destination(value):
    return value["book_id"], value["edition_id"]


def text_index(sources):
    return {(source["id"], block["id"]): block["text"] for source in sources for block in source["blocks"]}


def book_target(book, edition=None):
    return {"book_id": book["id"], "edition_id": edition["id"] if edition else book["edition_id"]}


def snapshot(wb):
    with wb.project.connect() as db:
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("books", "editions", "assignments", "dispositions", "revisions", "branches")}
        return {**counts, **dict(db.execute("SELECT key,value FROM meta WHERE key IN "
                                            "('assignment_generation','disposition_generation')"))}


def organization_scenario(root):
    root.mkdir()
    wb = Workbench(root / "library")
    water = wb.create_book("闸站值班规程", edition="初印")
    revised = wb.create_edition(water["id"], "增订")
    museum = wb.create_book("展柜保养纪要")
    herbarium = wb.create_book("标本室记录")
    water_first, water_revised, museum_target, herbarium_target = (
        book_target(water), book_target(water, revised), book_target(museum), book_target(herbarium))
    references = []
    for name, paragraphs, target in [
        ("水闸值班初印.txt", [WATER, WATER_FRAG, COMMON, REVISED_COMMON, NUM, OCR], water_first),
        ("水闸值班增订.txt", [WATER_NEW, REVISED_COMMON], water_revised),
        ("展柜保养.txt", [MUSEUM, COMMON], museum_target),
        ("标本室.txt", [HERBARIUM], herbarium_target),
    ]:
        source = load(wb, root, name, paragraphs)
        wb.assign(**target, spans=whole(source))
        references.append(source)

    mixed = load(wb, root, "散页合订.txt", [WATER, MUSEUM, WATER_NEW, UNKNOWN, HERBARIUM])
    fragment, _, short_fragment = WATER_FRAG.partition("；")
    fragment += "；"
    clipped = load(wb, root, "半页复印.txt", [fragment, short_fragment])
    concatenated = load(wb, root, "无段界抄件.txt", [WATER + " ※ " + MUSEUM])
    quotations = load(wb, root, "材料引录.txt", [COMMON, REVISED_COMMON, "“" + HERBARIUM + "”"])
    commentary = load(wb, root, "通报附注.txt", [f"通报附注写道：“{HERBARIUM}”其余见附件。"])
    variants = load(wb, root, "受损复本.txt", [NUM_VARIANT, OCR_VARIANT])
    truth = [
        ("first_edition", mixed, WATER, water_first),
        ("mixed_other_work", mixed, MUSEUM, museum_target),
        ("revised_edition", mixed, WATER_NEW, water_revised),
        ("mixed_third_work", mixed, HERBARIUM, herbarium_target),
        ("clipped_copy", clipped, fragment, water_first),
        ("short_clipped_copy", clipped, short_fragment, water_first),
        ("merged_boundary_left", concatenated, WATER, water_first),
        ("merged_boundary_right", concatenated, MUSEUM, museum_target),
        ("cross_book_common_text", quotations, COMMON, None),
        ("same_book_common_editions", quotations, REVISED_COMMON, None),
        ("marked_quotation", quotations, HERBARIUM, None),
        ("quotation_with_introduction", commentary, HERBARIUM, None),
        ("unseen_material", mixed, UNKNOWN, None),
        ("number_variant", variants, NUM_VARIANT, water_first),
        ("ocr_variant", variants, OCR_VARIANT, water_first),
    ]
    whole_paragraph = {"first_edition", "mixed_other_work", "revised_edition", "mixed_third_work",
                       "number_variant", "ocr_variant"}
    locations = [(label, coordinates((paragraph if label in whole_paragraph else region)(source, text)),
                  destination(target) if target else None)
                 for label, source, text, target in truth]
    queries = [mixed, clipped, concatenated, quotations, commentary, variants]
    texts = text_index([*queries, *references])
    before = snapshot(wb)
    start = perf_counter()
    plan = Organization(wb).create([value["id"] for value in queries])
    elapsed = perf_counter() - start
    assert snapshot(wb) == before
    actual, review, exact_proofs = [], [], 0
    for entry in plan["sources"]:
        for item in entry["candidates"]:
            if item["alternatives"]:
                selected, = item["spans"]
                coordinate = coordinates(selected)
                query_text = texts[coordinate[:2]][coordinate[2]:coordinate[3]]
                for alternative in item["alternatives"]:
                    for proof in alternative["evidence"]:
                        if proof["relation"] != "exact_text_span":
                            continue
                        origin = coordinates(proof["span"])
                        original_text = texts[origin[:2]][origin[2]:origin[3]]
                        assert query_text.encode("utf-8") == original_text.encode("utf-8")
                        exact_proofs += 1
            for selected in item["spans"]:
                coordinate = coordinates(selected)
                if item["suggested_target"]:
                    actual.append((coordinate, destination(item["suggested_target"])))
                else:
                    review.append((coordinate, item["kind"], {destination(option) for option in item["alternatives"]}))
    expected = {(key, target) for _, key, target in locations if target is not None}

    def overlaps(first, second):
        return first[:2] == second[:2] and max(first[2], second[2]) < min(first[3], second[3])

    def covered_characters(key, intervals):
        return len({i for coordinate in intervals for i in range(max(key[2], coordinate[2]), min(key[3], coordinate[3]))})

    wrong_targets, suggestions_on_abstentions, boundary_errors = 0, 0, 0
    for coordinate, target in actual:
        related = {truth_target for _, key, truth_target in locations if overlaps(key, coordinate)}
        wrong_targets += related != {target}
        suggestions_on_abstentions += None in related
        boundary_errors += related == {target} and (coordinate, target) not in expected
    cases = {}
    for label, key, target in locations:
        touching = [(coordinate, suggested) for coordinate, suggested in actual if overlaps(key, coordinate)]
        deferred = [(coordinate, kind, options) for coordinate, kind, options in review if overlaps(key, coordinate)]
        # Check coverage independently of abstention. A disappeared interval is not a safe deferral.
        pending_characters = covered_characters(key, [coordinate for coordinate, _, _ in deferred])
        correct_manual_characters = covered_characters(key, [coordinate for coordinate, _, options in deferred if target in options])
        visible_characters = covered_characters(key, [coordinate for coordinate, _ in touching] +
                                                [coordinate for coordinate, _, _ in deferred])
        if (key, target) in actual:
            outcome = "exact_suggestion"
        elif target is None:
            outcome = "abstained" if not touching else "unsafe_suggestion"
        elif touching:
            outcome = "non_exact_suggestion"
        elif correct_manual_characters == key[3] - key[2]:
            outcome = "fully_covered_by_manual_target"
        elif correct_manual_characters:
            outcome = "partially_covered_by_manual_target"
        else:
            outcome = "manual_without_target_evidence"
        cases[label] = {
            "expected": "suggestion" if target is not None else "abstain",
            "outcome": outcome,
            "exact_suggestion": (key, target) in actual,
            "suggested_spans_overlapping": len(touching),
            "characters_pending_review": pending_characters,
            "characters_with_correct_manual_target": correct_manual_characters,
            "visible_characters": visible_characters,
            "truth_characters": key[3] - key[2],
            "alternative_targets": len(set().union(*(options for _, _, options in deferred))),
            "review_kinds": sorted({kind for _, kind, _ in deferred}),
        }
    true_positives = len(set(actual) & expected)
    return {"exact_span_and_target": {"true_positives": true_positives, "suggested": len(actual),
                                      "truth": len(expected), "precision": true_positives / len(actual) if actual else None,
                                      "recall": true_positives / len(expected)},
            "wrong_target_suggestions": wrong_targets, "suggestions_on_abstentions": suggestions_on_abstentions,
            "same_target_boundary_errors": boundary_errors,
            "verified_exact_evidence_ranges": exact_proofs,
            "case_outcomes": dict(Counter(case["outcome"] for case in cases.values())),
            "fully_deferred_abstentions": sum(case["expected"] == "abstain" and case["suggested_spans_overlapping"] == 0
                                             and case["characters_pending_review"] == case["truth_characters"]
                                             for case in cases.values()),
            "automatic_assignment_or_revision_writes": False,
            "limit_reasons": sorted({reason for entry in plan["sources"] for reason in entry["limit_reasons"]}),
            "plan_seconds": round(elapsed, 6), "cases": cases}


def write_epub(path, caption, color):
    image = BytesIO()
    Image.new("RGB", (16, 16), color).save(image, "PNG")
    image_bytes = image.getvalue()
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        archive.writestr("book.opf", '<package><metadata><title>潮位尺检修图</title></metadata><manifest>'
                         '<item id="page" href="page.xhtml" media-type="application/xhtml+xml"/>'
                         '<item id="figure" href="diagram.png" media-type="image/png"/>'
                         '</manifest><spine><itemref idref="page"/></spine></package>')
        archive.writestr("page.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                         f'<figure><img src="diagram.png" alt="检修图"/><figcaption>{escape(caption)}</figcaption></figure>'
                         '</body></html>')
        archive.writestr("diagram.png", image_bytes)
    return image_bytes


def reject(operation, code):
    try:
        operation()
    except DomainError as exc:
        assert exc.code == code, (exc.code, str(exc))
    else:
        raise AssertionError(f"Expected {code}")


def verify_revision(result, sources, expected):
    blocks = {(source["id"], block["id"]): block for source in sources for block in source["blocks"]}
    revision = result["revision"]
    assert [value["source_span"] for value in revision["blocks"]] == expected
    for output, selected in zip(revision["blocks"], expected, strict=True):
        source = blocks[selected["source_id"], selected["block_id"]]
        raw = source["text"][selected["start"]:selected["end"]].encode("utf-8")
        assert output["text"].encode("utf-8") == raw
        assert output["resources"] == source["resources"]
        proof, = output["provenance"]
        assert proof == {"kind": "copy", "source": selected, "output_start": 0, "output_end": len(output["text"]),
                         "text_sha256": hashlib.sha256(raw).hexdigest()}
    assert revision["changes"][0]["payload"]["selections"] == expected
    return len(expected)


def verify_option_previews(plan, sources):
    texts = text_index(sources)
    count = 0
    for row in plan["rows"]:
        for option in row["options"]:
            parts = []
            for selected in option["spans"]:
                source, block, start, end = coordinates(selected)
                original = texts[source, block]
                assert 0 <= start <= end <= len(original)
                parts.append(original[start:end])
            assert option["preview"] == "\n".join(part[:240] for part in parts[:3])[:720]
            count += 1
    return count


def verify_composite(wb, curation, target, base, base_spans, other, other_edition):
    before = snapshot(wb)
    plan = curation.create(*destination(target), base["id"], [other["id"]], allow_composite=True)
    assert plan["allow_composite"] and not plan["incomplete"]
    edition_id = other_edition["id"]
    assert plan["source_editions"] == {base["id"]: [target["edition_id"]], other["id"]: [edition_id]}
    assert plan["editions"][edition_id]["label"] == "修订排印本"
    original, replacement = paragraph(base, NUM), paragraph(other, NUM_VARIANT)
    row, = [row for row in plan["rows"] if row["kind"] == "conflict" and
            list(map(coordinates, row["options"][0]["spans"])) == [coordinates(original)]]
    option, = [option for option in row["options"] if
               list(map(coordinates, option["spans"])) == [coordinates(replacement)]]
    selected, = option["spans"]
    assert selected["edition_ids"] == [edition_id]
    assert selected["edition_ranges"] == [{"edition_id": edition_id, "start": replacement["start"], "end": replacement["end"]}]
    assert row["default_option_id"] is None
    options = verify_option_previews(plan, [base, other])
    reject(lambda: curation.assemble(plan["id"], plan["snapshot_sha"], {}), "curation_choices")
    assert snapshot(wb) == before
    result = curation.assemble(plan["id"], plan["snapshot_sha"], {row["id"]: option["id"]})
    assert result["revision"]["metadata"]["assembly_policy"] == "composite-confirmed"
    assert result["revision"]["changes"][0]["payload"]["guided_plan"]["allow_composite"] is True
    output = verify_revision(result, [base, other], [replacement if part == original else part for part in base_spans])
    assert snapshot(wb)["assignment_generation"] == before["assignment_generation"]
    return options, output


def verify_resources(wb, curation, root, target):
    figures, pixels = [], []
    caption = "北侧闸室水位尺的固定位置"
    for name, color in [("检修附图甲.epub", "navy"), ("检修附图乙.epub", "teal")]:
        path = root / name
        pixels.append(write_epub(path, caption, color))
        source = wb.source(wb.import_file(path)["source_id"])
        wb.assign(**target, spans=whole(source))
        figures.append(source)
    plan = curation.create(*destination(target), figures[0]["id"], [figures[1]["id"]])
    options = verify_option_previews(plan, figures)
    row, = plan["rows"]
    assert row["kind"] == "conflict" and "structure_variation" in row["reason_codes"]
    assert row["default_option_id"] is None
    option, = [option for option in row["options"] if option["spans"] == whole(figures[1])]
    result = curation.assemble(plan["id"], plan["snapshot_sha"], {row["id"]: option["id"]})
    output = verify_revision(result, figures, whole(figures[1]))
    resource, = result["revision"]["blocks"][0]["resources"]
    assert pixels[0] != pixels[1] == wb.project.object_path(resource["sha256"]).read_bytes()
    return options, output


def curation_scenario(root):
    root.mkdir()
    wb = Workbench(root / "library")
    book = wb.create_book("堤岸检修手册", edition="馆藏印本")
    target = book_target(book)
    opening = "到岗后先核对工具架上悬挂的领用牌，未领走的备用器具仍应留在橙色方格内。"
    split = ("晴天应把南岸的铆接面刷净并擦干。", "风力超过四级时停止刷漆，将余料移回带盖铁桶。")
    merge = ("围堰关闭前要通知下游的巡查小组。", "若岗哨仍未收到通知，就在吊桥上挂两面绿色小旗，等回旗后再开始放水。")
    moved = ("用木柄小锤沿北侧斜坡逐格轻敲，声音空闷的位置另画三角记号。",
             "先把梯顶的旧绳取下晾干，重新编号后卷成相同直径的绳盘。")
    repeated = "观察员须复述闸门开度，确认后由记录员盖章。"
    between = "挡板铰链的润滑孔位于吊耳背后，须换用弯头油枪。"
    gap_left = "潮位尺零点校验结束后，将黄铜托座固定在示意图标明的石阶上。"
    gap_right = "拆下隔离围栏之前，应请航标站复核浅滩两侧的两根红色木桩。"
    inserts = ("校验员把偏差读数写在黄色签条上，并在托座外沿留出一指宽的空位。",
               "复核人员把偏差读数抄入蓝边表格，再将表格投入上锁的铁盒。")
    tail = "最后一班按图索引逐项对照当日的维修记录，然后把原始表格装入防潮套。"
    front = "附记称上游沿岸的石灰窑已经停用，村里仍保存着几块早年的火砖。"
    outside = "🔧校验时不得借用灌溉区的备用蓄电池。"
    base = load(wb, root, "底稿.txt", [outside + opening, OCR, NUM, "".join(split), *merge, *moved,
                                      repeated, between, repeated, gap_left, gap_right, tail])
    donor_a = load(wb, root, "补页甲.txt", [front, opening, OCR_VARIANT, NUM_VARIANT, *split, "".join(merge),
                                           *reversed(moved), repeated, between, gap_left, inserts[0], gap_right, tail])
    donor_b = load(wb, root, "补页乙.txt", [gap_left, inserts[1], gap_right, tail])
    other = load(wb, root, "另一版.txt", [opening, NUM_VARIANT])
    different = wb.create_edition(book["id"], "修订排印本")
    first, *rest = whole(base)
    base_spans = [{**first, "start": len(outside)}, *rest]
    wb.assign(**target, spans=base_spans)
    for donor in (donor_a, donor_b):
        wb.assign(**target, spans=whole(donor))
    wb.assign(**book_target(book, different), spans=whole(other))
    curation = Curation(wb)
    reject(lambda: curation.create(*destination(target), base["id"], [other["id"]]), "curation_scope")
    before = snapshot(wb)
    start = perf_counter()
    plan = curation.create(*destination(target), base["id"], [donor_a["id"], donor_b["id"]])
    elapsed = perf_counter() - start
    assert curation.get(plan["id"]) == plan and not plan["incomplete"], plan["limit_reasons"]
    options_verified = verify_option_previews(plan, [base, donor_a, donor_b])
    assert snapshot(wb) == before
    reject(lambda: curation.assemble(plan["id"], plan["snapshot_sha"], {}), "curation_choices")
    assert snapshot(wb) == before

    rows = plan["rows"]
    expected_conflicts = {
        "ocr": ([paragraph(base, OCR)], [paragraph(donor_a, OCR_VARIANT)], "text_variation"),
        "number": ([paragraph(base, NUM)], [paragraph(donor_a, NUM_VARIANT)], "text_variation"),
        "split": ([paragraph(base, "".join(split))], [paragraph(donor_a, part) for part in split], "split_or_merge"),
        "merge": ([paragraph(base, part) for part in merge], [paragraph(donor_a, "".join(merge))], "split_or_merge"),
    }
    base_choices, donor_choices = {}, {}
    replacements = {}
    for label, (original, alternative, relation) in expected_conflicts.items():
        matching = [row for row in rows if row["kind"] == "conflict" and row["options"][0]["spans"] == original]
        assert len(matching) == 1, (label, [(row["kind"], row["reason_codes"], row["options"]) for row in rows])
        row, = matching
        assert row["default_option_id"] is None and relation in row["reason_codes"]
        option, = [value for value in row["options"] if value["spans"] == alternative]
        base_choices[row["id"]] = row["options"][0]["id"]
        donor_choices[row["id"]] = option["id"]
        replacements[coordinates(original[0])] = alternative
        replacements.update((coordinates(value), []) for value in original[1:])

    addition, = [row for row in rows if row["kind"] == "addition"]
    assert addition["default_option_id"] is None and "competing_donors" in addition["reason_codes"]
    inserted_a, inserted_b = [paragraph(source, text) for source, text in zip((donor_a, donor_b), inserts, strict=True)]
    assert {tuple(map(coordinates, option["spans"])) for option in addition["options"]} == {
        (), (coordinates(inserted_a),), (coordinates(inserted_b),)}
    base_choices[addition["id"]] = next(option["id"] for option in addition["options"] if not option["spans"])
    donor_choices[addition["id"]] = next(option["id"] for option in addition["options"] if option["spans"] == [inserted_b])
    unplaced = [row for row in rows if row["kind"] == "unplaced"]
    for pending in (paragraph(donor_a, front), paragraph(donor_a, repeated)):
        row, = [row for row in unplaced if pending in row["evidence"]["spans"]]
        assert row["default_option_id"] is None
    for row in unplaced:
        skipped, = row["options"]
        assert not skipped["spans"]
        base_choices[row["id"]] = donor_choices[row["id"]] = skipped["id"]

    moves = [match for row in rows for match in row["evidence"].get("matches", []) if match.get("moved")]
    assert {coordinates(match["base_spans"][0]) for match in moves} == {coordinates(paragraph(base, text)) for text in moved}
    assert all(match["relation"] == "identical_content" for match in moves)
    base_result = curation.assemble(plan["id"], plan["snapshot_sha"], base_choices)
    base_count = verify_revision(base_result, [base, donor_a, donor_b], base_spans)
    assert [block["text"].strip() for block in base_result["revision"]["blocks"]].count(repeated) == 2

    second = curation.create(*destination(target), base["id"], [donor_a["id"], donor_b["id"]])
    donor_result = curation.assemble(second["id"], second["snapshot_sha"], donor_choices)
    right_anchor = paragraph(base, gap_right)
    expected = []
    for selected in base_spans:
        if selected == right_anchor:
            expected.append(inserted_b)
        expected.extend(replacements.get(coordinates(selected), [selected]))
    donor_count = verify_revision(donor_result, [base, donor_a, donor_b], expected)
    third = curation.create(*destination(target), base["id"], [donor_a["id"], donor_b["id"]])
    base_choices[addition["id"]] = next(option["id"] for option in addition["options"] if option["spans"] == [inserted_a])
    another_result = curation.assemble(third["id"], third["snapshot_sha"], base_choices)
    expected_with_a = base_spans.copy()
    expected_with_a.insert(expected_with_a.index(right_anchor), inserted_a)
    another_count = verify_revision(another_result, [base, donor_a, donor_b], expected_with_a)
    assert snapshot(wb)["assignments"] == before["assignments"]

    composite_options, composite_count = verify_composite(wb, curation, target, base, base_spans, other, different)
    resource_options, resource_count = verify_resources(wb, curation, root, target)
    return {"row_kinds": dict(Counter(row["kind"] for row in rows)),
            "confirmed_conflict_cases": sorted(expected_conflicts), "moved_exact_ranges": len(moves),
            "repeated_base_ranges_preserved": 2, "competing_insertions": 2,
            "unplaced_ranges": sum(len(row["evidence"]["spans"]) for row in unplaced),
            "source_backed_option_previews": options_verified + composite_options + resource_options,
            "byte_verified_output_ranges": base_count + donor_count + another_count + resource_count + composite_count,
            "resource_conflict_and_original_bytes_verified": True,
            "different_edition_refused_by_default": True, "explicit_composite_selection_verified": True,
            "automatic_revision_writes": False,
            "plan_seconds": round(elapsed, 6)}


def seed_catalog(wb, template, count):
    # Seed independent originals and their one-line TXT extractions in a
    # single transaction. This measures catalog lookup only.
    project = wb.project
    raw = project.object_path(template["asset_sha"]).read_bytes()
    now = time()
    start = perf_counter()
    with project.transaction() as db:
        def write_object(data, kind):
            sha = hashlib.sha256(data).hexdigest()
            path = project.object_path(sha)
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
            db.execute("INSERT INTO objects VALUES(?,?,?,?)", (sha, len(data), kind, now))
            return sha

        for number in range(count):
            book_id, edition_id, source_id = (f"{kind}-{number:05}" for kind in ("book", "edition", "source"))
            name = f"装订清册第{number:05}册.txt"
            data = raw + b" " * (number + 1)
            sha = write_object(data, "asset")
            decoded = data.decode("utf-8")
            extraction = ExtractionResult(Path(name).stem)
            block = extraction.add("paragraph", decoded, {"start": 0, "end": len(decoded), "offset_space": "decoded_codepoints"})
            blocks_sha = write_object(canonical(extraction.blocks).encode("utf-8"), "extraction")
            metadata = {**template["metadata"], "title": extraction.title,
                        "suggested_segments": wb._suggest_segments(extraction.blocks, extraction.title)}
            db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?)", (
                source_id, sha, name, "ready", blocks_sha, 1, canonical(metadata), "[]", template["parser"], now))
            db.execute("INSERT INTO imports VALUES(?,?,?,?)", (f"import-{number}", source_id, name, now))
            db.execute("INSERT INTO books(id,title,author,language,created) VALUES(?,?,?,?,?)",
                       (book_id, f"装订清册第{number:05}册", "合成编目员", "zh", now))
            db.execute("INSERT INTO editions(id,book_id,label,language) VALUES(?,?,?,?)",
                       (edition_id, book_id, "编目样本", "zh"))
            db.execute("INSERT INTO dispositions VALUES(?,?,?,?,?,?,?)", (
                f"disposition-{number}", source_id, block["id"], 0, len(block["text"]), "pending", ""))
            project.ref(db, "source", source_id, sha, "original")
            project.ref(db, "source", source_id, blocks_sha, "extraction")
    return perf_counter() - start


def catalog_scenario(root):
    root.mkdir()
    wb = Workbench(root / "library")
    old = wb.create_book("装订清册_史%藏", author="城南档案室", edition="线装初刊")
    other = wb.create_book("装订清册甲史乙藏", edition="影印初刊")
    second = wb.create_edition(old["id"], "胶订复刻")
    a = load(wb, root, "清册_史%藏初刊.txt", [WATER])
    b = load(wb, root, "清册复刻.txt", [WATER_NEW])
    c = load(wb, root, "清册甲史乙藏.txt", [UNKNOWN])
    for book, edition, source in ((old, None, a), (old, second, b), (other, None, c)):
        wb.assign(**book_target(book, edition), spans=whole(source))
    seeded = 1021
    seed_seconds = seed_catalog(wb, c, seeded)

    timings, queries = [], 0
    with TestClient(create_app(wb.project.root, start_worker=False)) as client:
        assert client.get("/api/session").status_code == 200

        def query(endpoint, **params):
            nonlocal queries
            start = perf_counter()
            response = client.get(f"/api/{endpoint}", params=params)
            assert response.status_code == 200, response.text
            payload = response.json()
            timings.append(perf_counter() - start)
            queries += 1
            return payload

        legacy = query("state")
        assert len(legacy["books"]) == len(legacy["sources"]) == 1000
        assert old["id"] not in {item["id"] for item in legacy["books"]}
        assert a["id"] not in {item["id"] for item in legacy["sources"]}
        for collection, originals in (("books", [old["id"], other["id"]]), ("sources", [a["id"], b["id"], c["id"]])):
            total = seeded + len(originals)
            seen, offset = [], 0
            while offset is not None:
                page = query(collection, offset=offset, limit=113)
                assert page["total"] == total and page["offset"] == offset and len(page["items"]) <= 113
                seen.extend(item["id"] for item in page["items"])
                expected_next = offset + 113 if offset + 113 < total else None
                assert page["next_offset"] == expected_next
                offset = page["next_offset"]
            assert len(seen) == len(set(seen)) == total
            assert seen[-len(originals):] == originals[::-1]
            assert legacy["totals"][collection] == total

        def ids(collection, **params):
            page = query(collection, **params)
            assert page["total"] == len(page["items"])
            return {item["id"] for item in page["items"]}

        assert ids("books", search="_史%藏") == {old["id"]}
        assert ids("books", search="城南档案室") == {old["id"]}
        assert ids("sources", search="_史%藏") == {a["id"]}
        assert ids("editions", book_id=old["id"]) == {old["edition_id"], second["id"]}
        assert ids("sources", book_id=old["id"]) == {a["id"], b["id"]}
        assert ids("sources", book_id=old["id"], edition_id=old["edition_id"]) == {a["id"]}
        assert ids("sources", book_id=old["id"], edition_id=second["id"]) == {b["id"]}
        assert ids("sources", book_id=other["id"], edition_id=second["id"]) == set()
        assert query(f"sources/{a['id']}")["blocks"][0]["text"] == WATER

    return {"fixture_bulk_seeded": seeded, "sources": seeded + 3, "books": seeded + 2,
            "page_size": 113, "catalog_http_reads": queries,
            "old_records_searchable_and_editions_scoped": True,
            "bulk_seed_seconds": round(seed_seconds, 6), "http_read_seconds": round(sum(timings), 6),
            "slowest_http_read_seconds": round(max(timings), 6), "import_throughput_measured": False}


def budget_scenario(root):
    root.mkdir()
    wb = Workbench(root / "library")
    book = wb.create_book("样方长期观测簿")
    target = book_target(book)
    body = "春分后的记录每隔三天由两名观察员各自填写，雨后应另记石灰样方的积水深度。"
    long_text = body * 13_000
    base = load(wb, root, "观测册底本.md", [long_text, WATER])
    donors = [load(wb, root, f"观测册副本{number}.md", [long_text, f"记录架号为甲{number:03}。"])
              for number in range(4)]
    for source in [base, *donors]:
        wb.assign(**target, spans=whole(source))
    curation = Curation(wb)
    before = snapshot(wb)
    started = perf_counter()
    plan = curation.create(*destination(target), base["id"], [source["id"] for source in donors])
    curation_seconds = perf_counter() - started
    assert plan["incomplete"] and "character_budget" in plan["limit_reasons"] and not plan["rows"]
    reject(lambda: curation.assemble(plan["id"], plan["snapshot_sha"], {}), "curation_incomplete")
    started = perf_counter()
    narrowed = curation.create(*destination(target), base["id"], [donors[0]["id"]])
    narrowed_seconds = perf_counter() - started
    counts = Counter(row["kind"] for row in narrowed["rows"])
    assert not narrowed["incomplete"], narrowed["limit_reasons"]
    assert counts["unplaced"] == 1 and len(narrowed["rows"]) <= len(base["blocks"]) + len(donors[0]["blocks"]), counts
    assert any(row["kind"] == "exact_duplicate" and row["options"][0]["spans"] == whole(base)[:1]
               for row in narrowed["rows"])

    # New, unassigned sources have small exact evidence after a huge block.
    # The plan must report both fragment-level omissions and its batch boundary.
    queries = [load(wb, root, f"待核观测{number}.md", [str(number) + long_text, WATER]) for number in range(5)]
    before_query = snapshot(wb)
    started = perf_counter()
    organization = Organization(wb).create([source["id"] for source in queries])
    organization_seconds = perf_counter() - started
    reasons = {reason for entry in organization["sources"] for reason in entry["limit_reasons"]}
    assert "source_fragment_budget" in reasons and "source_budget" in reasons
    planned = sum(len(entry["candidates"]) for entry in organization["sources"])
    assert 0 < planned <= sum(len(source["blocks"]) for source in queries)
    assert any(entry["incomplete"] and not entry["candidates"] for entry in organization["sources"])
    assert snapshot(wb) == before_query and before_query["revisions"] == before["revisions"]
    return {"characters_per_long_block": len(long_text), "curation_sources": 5, "organization_sources": len(queries),
            "curation_limit_reasons": plan["limit_reasons"], "curation_rows_at_limit": len(plan["rows"]),
            "curation_rows_after_narrowing": len(narrowed["rows"]), "curation_seconds": round(curation_seconds, 6),
            "curation_narrowed_seconds": round(narrowed_seconds, 6), "organization_limit_reasons": sorted(reasons),
            "organization_candidate_count": planned, "organization_seconds": round(organization_seconds, 6)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True, help="New directory for synthetic libraries")
    parser.add_argument("--output", type=Path, required=True, help="New JSON file to write aggregate results")
    args = parser.parse_args()
    if args.work_dir.exists() or args.output.exists() or not args.work_dir.parent.is_dir() or not args.output.parent.is_dir():
        parser.error("Both paths must be new, with existing parent directories")
    args.work_dir.mkdir()
    report = {"scope": "Synthetic held-out fixtures. These measurements do not estimate quality on real collections.",
              "environment": {"yuecang": __version__, "python": platform.python_version(),
                              "os": platform.system(), "architecture": platform.machine()}}
    report.update({name: scenario(args.work_dir / name) for name, scenario in (
        ("organization", organization_scenario), ("curation", curation_scenario),
        ("catalog", catalog_scenario), ("long_text_budget", budget_scenario))})
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
