import pytest

from yuecang.application.organization import Organization
from yuecang.application.service import Workbench
from yuecang.core.source_analysis import span
from yuecang.jobs import JobQueue
from yuecang.storage.project import DomainError


PLANTS = "青溪的植物观测站在石桥下方设置了三组样方，每组由不同志愿者记录种子的外壳与颜色。"
STARS = "山顶天文站的记录员在入夜前锁定了蓝色观测塔的圆顶，并检查八个刻度盘的校准结果。"
UNKNOWN = "砂岩仓库的修复师沿着墙体边缘寻找旧木榫，连续记录每一个需要单独处理的裂缝。"
QUOTE = "此句特意写为两个独立作品都可使用的共同引文，字数足够成为直接文本匹配证据。"


@pytest.fixture
def library(tmp_path):
    wb = Workbench(tmp_path / "library")

    def source(name, content):
        path = tmp_path / name
        path.write_bytes(content.encode("utf-8"))
        return wb.source(wb.import_file(path)["source_id"])

    return wb, Organization(wb), source


def selections(source):
    return [span(source["id"], block["id"], 0, len(block["text"])) for block in source["blocks"]]


def target(book):
    return {"book_id": book["id"], "edition_id": book["edition_id"]}


def expected(row):
    return {key: row[key] for key in ("source_id", "block_id", "start", "end", "book_id", "edition_id", "blocks_sha")}


def test_empty_library_bootstrap_groups_mix_and_reuses_explicit_creation_key(library):
    wb, organization, load = library
    mixed = load("合刊.txt", f"《植物记录》\n{PLANTS}\n《星空记录》\n{STARS}")
    another = load("植物资料.txt", UNKNOWN)
    plan = organization.create([mixed["id"], another["id"]])
    a, b = plan["sources"][0]["candidates"]
    c, = plan["sources"][1]["candidates"]
    assert (a["title_hint"], b["title_hint"]) == ("植物记录", "星空记录")
    assert all(candidate["kind"] == "unassigned" and candidate["suggested_target"] is None for candidate in (a, b, c))
    assert organization.get(plan["id"]) == plan
    assert not wb.state()["books"] and not wb.state()["assignments"]

    new_book = {"new_book": {"key": "plant", "title": "植物记录", "author": "合成作者"}}
    decisions = [{"candidate_id": a["id"], "action": "confirm", "target": new_book},
                 {"candidate_id": c["id"], "action": "confirm", "target": new_book},
                 {"candidate_id": b["id"], "action": "reject"}]
    receipt = organization.apply(plan["id"], decisions)
    assert receipt["confirmed"] == 2 and receipt["rejected_ids"] == [b["id"]]
    assert len(receipt["created_books"]) == len(wb.state()["books"]) == 1
    assert len(receipt["assignment_ids"]) == len(a["spans"] + c["spans"])
    assert {row["book_id"] for row in wb.state()["assignments"]} == {receipt["created_books"][0]["book_id"]}
    assert organization.apply(plan["id"], decisions[::-1]) == receipt
    assert organization.get(plan["id"])["result"] == receipt
    with pytest.raises(DomainError) as stale:
        organization.apply(plan["id"], decisions[:1])
    assert stale.value.status == 409


def test_exact_direct_ranges_are_suggested_without_propagating_unknown_content(library):
    wb, organization, load = library
    known = load("基准.txt", PLANTS)
    work = wb.create_book("植物")
    wb.assign(**target(work), spans=selections(known))
    mixed = load("混合.txt", PLANTS + "\n\n" + UNKNOWN)
    unknown = load("另一个来源.txt", UNKNOWN)
    plan = organization.create([mixed["id"], unknown["id"]])
    first, second = plan["sources"]
    direct, = [item for item in first["candidates"] if item["kind"] == "matched"]
    assert direct["suggested_target"] == target(work)
    assert direct["spans"][0]["preview"] == PLANTS
    origin = direct["alternatives"][0]["evidence"][0]
    assert origin["span"] == selections(known)[0]
    assert origin["preview"] == PLANTS
    assert any(UNKNOWN in selected["preview"]
               for item in first["candidates"] if item["kind"] == "unassigned"
               for selected in item["spans"])
    assert all(item["kind"] == "unassigned" for item in second["candidates"])
    result = organization.apply(plan["id"], [{"candidate_id": direct["id"], "action": "confirm"}])
    assert result["deferred"] == sum(len(item["candidates"]) for item in plan["sources"]) - 1
    assert organization.assignments(unknown["id"])["total"] == 0
    assert organization.assignments(mixed["id"])["items"][0]["preview"] == PLANTS
    assert all(item["kind"] == "unassigned" for item in organization.create([unknown["id"]])["sources"][0]["candidates"])


def test_shared_partial_quote_and_same_book_editions_remain_ambiguous(library):
    wb, organization, load = library
    plant = wb.create_book("植物")
    stars = wb.create_book("星空")
    edition = wb.create_edition(plant["id"], "重订版")
    for name, content, destination in [
        ("植物.txt", PLANTS + QUOTE, target(plant)),
        ("星空.txt", QUOTE + STARS, target(stars)),
        ("植物重订.txt", PLANTS + "再版附言", {"book_id": plant["id"], "edition_id": edition["id"]}),
    ]:
        reference = load(name, content)
        wb.assign(**destination, spans=selections(reference))
    mixed = load("待核对.txt", PLANTS + QUOTE + STARS)
    items = organization.create([mixed["id"]])["sources"][0]["candidates"]
    quote, = [item for item in items if item["spans"][0]["preview"] == QUOTE]
    plant_text, = [item for item in items if item["spans"][0]["preview"] == PLANTS]
    assert {item["book_id"] for item in quote["alternatives"]} == {plant["id"], stars["id"]}
    assert {item["edition_id"] for item in plant_text["alternatives"]} == {plant["edition_id"], edition["id"]}
    assert all(item["kind"] == "ambiguous" and item["suggested_target"] is None for item in (quote, plant_text))
    assert all(len(proof["preview"]) == proof["match_chars"] == proof["span"]["end"] - proof["span"]["start"]
               for item in (quote, plant_text) for option in item["alternatives"] for proof in option["evidence"])


def test_long_inline_quotation_retains_evidence_without_suggesting_identity(library):
    wb, organization, load = library
    reference = load("原文.txt", PLANTS)
    plant = wb.create_book("合成植物书")
    wb.assign(**target(plant), spans=selections(reference))
    commentary = load("引述.txt", f"笔记摘录：“{PLANTS}”请另核对。")
    items = organization.create([commentary["id"]])["sources"][0]["candidates"]
    quote, = [item for item in items if item["spans"][0]["preview"] == PLANTS]
    assert quote["kind"] == "ambiguous" and quote["suggested_target"] is None
    assert quote["alternatives"][0]["book_id"] == plant["id"]


def test_changes_stale_frozen_plan_and_malformed_decisions_rollback(library):
    wb, organization, load = library
    first = load("一.txt", PLANTS)
    second = load("二.txt", STARS)
    plan = organization.create([first["id"], second["id"]])
    a, b = [entry["candidates"][0]["id"] for entry in plan["sources"]]
    destination = {"new_book": {"key": "new", "title": "样本"}}
    with pytest.raises(DomainError) as inconsistent:
        organization.apply(plan["id"], [{"candidate_id": a, "action": "confirm", "target": destination},
                                        {"candidate_id": b, "action": "confirm",
                                         "target": {"new_book": {"key": "new", "title": "另一个样本"}}}])
    assert inconsistent.value.code == "assignment_target"
    with pytest.raises(DomainError) as invalid:
        organization.apply(plan["id"], [{"candidate_id": a, "action": "confirm", "target": destination},
                                        {"candidate_id": b, "action": "confirm",
                                         "target": {"book_id": "missing", "edition_id": "missing"}}])
    assert invalid.value.status == 404
    assert not wb.state()["books"] and not wb.state()["assignments"]
    assert organization.get(plan["id"])["state"] == "ready"

    wb.dispose(**selections(second)[0], kind="ignored", reason="合成处置")
    with pytest.raises(DomainError) as stale:
        organization.apply(plan["id"], [{"candidate_id": a, "action": "confirm", "target": destination}])
    assert stale.value.code == "assignment_plan_stale" and stale.value.status == 409
    replacement = organization.create([first["id"], second["id"]])
    assert replacement["sources"][1]["candidates"] == []
    revision_book = wb.create_book("整理目标")
    wb.assemble(revision_book["id"], selections(first), revision_book["edition_id"])
    with pytest.raises(DomainError) as stale:
        organization.apply(replacement["id"], [{"candidate_id": a, "action": "confirm", "target": destination}])
    assert stale.value.code == "assignment_plan_stale"


def test_correction_and_removal_preserve_frozen_revision_and_surviving_dispositions(library):
    wb, organization, load = library
    source = load("修改测试.txt", "😀" + PLANTS)
    first, second, corrected = [wb.create_book(title) for title in ("原归属", "重叠归属", "正确归属")]
    whole = selections(source)[0]
    revision_id = wb.assemble(first["id"], [whole], first["edition_id"])["revision_id"]
    original = wb.revision(revision_id)
    wb.assign(**target(second), spans=[{**whole, "start": 5, "end": 15}])
    wb.dispose(**{**whole, "start": 0, "end": 2}, kind="attachment", reason="保留明确处置")
    wb.dispose(**{**whole, "start": 2, "end": 4}, kind="used", reason="人工标记")
    initial = organization.assignments(source["id"], offset=1, limit=1)["items"][0]
    assert initial["preview"].startswith("😀")
    rebound = organization.change_assignment(initial["id"], expected(initial), target(corrected))
    replacement = rebound["assignment"]
    assert rebound["removed_id"] == initial["id"] and replacement["id"] != initial["id"]
    assert replacement["book_id"] == corrected["id"]
    with pytest.raises(DomainError) as stale:
        organization.change_assignment(initial["id"], expected(initial))
    assert stale.value.status == 409
    assert organization.change_assignment(replacement["id"], expected(replacement)) == {
        "assignment": None, "removed_id": replacement["id"],
    }
    spans = [(row["start"], row["end"], row["kind"], row["reason"])
             for row in wb.source(source["id"])["dispositions"]]
    assert spans == [(0, 2, "attachment", "保留明确处置"), (2, 4, "used", "人工标记"),
                     (4, 5, "pending", ""), (5, 15, "used", "已关联图书"), (15, whole["end"], "pending", "")]
    assert wb.revision(revision_id) == original
    assert organization.assignments(source["id"])["items"][0]["book_id"] == second["id"]


def test_leases_and_bad_request_values_do_not_write_plans(library):
    wb, organization, load = library
    source = load("来源.txt", PLANTS)
    for value in (None, [], [{}], [source["id"]] * 2, [f"{i}" for i in range(21)]):
        with pytest.raises(DomainError) as invalid:
            organization.create(value)
        assert invalid.value.code == "assignment_sources"
    with pytest.raises(DomainError) as lease:
        organization.create([source["id"]], job={"id": "expired", "token": "invalid"})
    assert lease.value.code == "stale_job"
    with wb.project.connect() as db:
        assert db.execute("SELECT count(*) FROM candidates WHERE kind='assignment_plan'").fetchone()[0] == 0


def test_job_retry_reuses_plan_after_publication(library):
    wb, organization, load = library
    source_ids = [load("合成任务来源.txt", PLANTS)["id"]]
    queue = JobQueue(wb.project)
    queue.enqueue("assignment_plan", {"source_ids": source_ids})
    job = queue.claim()
    first = organization.create(source_ids, job=job)
    assert organization.create(source_ids, job=job) == first
    with wb.project.connect() as db:
        assert db.execute("SELECT count(*) FROM candidates WHERE kind='assignment_plan'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM object_refs WHERE owner='assignment_plan' AND owner_id=?",
                          (first["id"],)).fetchone()[0] == 1


def test_zero_length_image_range_stays_atomic_through_confirm_and_remove(library, tmp_path):
    from PIL import Image

    wb, organization, _ = library
    image = tmp_path / "synthetic.png"
    Image.new("RGB", (2, 2), (0, 128, 0)).save(image)
    source = wb.source(wb.import_file(image)["source_id"])
    plan = organization.create([source["id"]])
    item, = plan["sources"][0]["candidates"]
    assert item["spans"] == [{**selections(source)[0], "preview": ""}]
    assert item["spans"][0]["start"] == item["spans"][0]["end"] == 0
    book = wb.create_book("合成图册")
    organization.apply(plan["id"], [{"candidate_id": item["id"], "action": "confirm", "target": target(book)}])
    assignment, = organization.assignments(source["id"])["items"]
    organization.change_assignment(assignment["id"], expected(assignment))
    assert wb.source(source["id"])["dispositions"][0]["kind"] == "pending"
    assert wb.source(source["id"])["blocks"][0]["resources"]
