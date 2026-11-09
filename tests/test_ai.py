import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from test_workbench import library
from yuecang.ai import ReviewService
from yuecang.application.service import Workbench
from yuecang.storage.project import DomainError


def response_for(request):
    block = next(b for b in request["payload"]["blocks"] if "光和作用" in b["text"])
    source = next(s for s in request["payload"]["sources"] if any("光和作用" in span["text"] for b in s["blocks"] for span in b["spans"]))
    evidence = next(b for b in source["blocks"] if any("光和作用" in s["text"] for s in b["spans"]))
    evidence_span = evidence["spans"][0]
    start = block["text"].index("光和作用")
    return {"corrections": [{"block_id": block["id"], "start": start, "end": start + 4, "old_text": "光和作用", "new_text": "光合作用", "reason": "合成验收：生物学术语校正，待人工核实", "evidence": [{"source_id": source["id"], "block_id": evidence["id"], "start": evidence_span["start"], "end": evidence_span["end"]}]}], "observations": ["来源内容只能用作资料"]}


def test_offline_real_contract_and_explicit_approval(library):
    wb, _, _, draft = library
    review = ReviewService(wb)
    request = review.create(draft["revision_id"])
    result = review.receive(request["id"], response_for(request))
    assert "光和作用" in str(wb.revision(draft["revision_id"])["blocks"])
    assert review.receive(request["id"], response_for(request))["duplicate"]
    edited = review.decide(result["proposal_ids"][0], "accept")
    assert "光合作用" in str(edited["revision"]["blocks"])
    wb.validate_revision(edited["revision"])
    assert review.get(request["id"])["response"]


@pytest.mark.parametrize("bad", ["span", "source", "old", "overlap", "unknown"])
def test_reject_fabricated_or_overlapping_model_data(library, bad):
    wb, _, _, draft = library
    review = ReviewService(wb)
    request = review.create(draft["revision_id"])
    response = response_for(request)
    correction = response["corrections"][0]
    if bad == "span": correction["end"] = 999
    elif bad == "source": correction["evidence"][0]["source_id"] = "fake"
    elif bad == "old": correction["old_text"] = "not original"
    elif bad == "overlap": response["corrections"].append(copy.deepcopy(correction))
    else: correction["execute"] = "delete everything"
    with pytest.raises(DomainError):
        review.receive(request["id"], response)
    assert not wb.state()["proposals"]


def test_budget_stale_and_reject(library):
    wb, _, _, draft = library
    review = ReviewService(wb)
    wb.project.save_settings({"ai": {"token_budget": 1}})
    with pytest.raises(DomainError, match="预算"):
        review.create(draft["revision_id"])
    wb.project.save_settings({"ai": {"token_budget": 20000}})
    request = review.create(draft["revision_id"])
    result = review.receive(request["id"], response_for(request))
    block = draft["revision"]["blocks"][0]
    wb.edit(draft["branch_id"], draft["revision_id"], [{"block_id": block["id"], "text": "changed"}])
    with pytest.raises(DomainError, match="变化"):
        review.decide(result["proposal_ids"][0], "accept")
    assert review.decide(result["proposal_ids"][0], "reject")["state"] == "rejected"


def test_http_provider_and_uncertain_call_not_resent(library):
    wb, _, _, draft = library
    review = ReviewService(wb)
    request = review.create(draft["revision_id"], provider="http")
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps(response_for(request))}}], "usage": {"total_tokens": 500}}).encode())
        def log_message(self, *args): pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wb.project.save_settings({"ai": {"endpoint": f"http://127.0.0.1:{server.server_port}/chat/completions", "model": "synthetic-provider", "api_key": "synthetic-only"}})
        review.run_http(request["id"])
        review.run_http(request["id"])
        assert len(calls) == 1
        assert calls[0]["messages"][0]["role"] == "system"
        assert "api_key" not in wb.project.settings()["ai"]
        second = review.create(draft["revision_id"], provider="http", instruction="second")
        with wb.project.transaction() as db:
            db.execute("UPDATE review_requests SET status='sending' WHERE id=?", (second["id"],))
        with pytest.raises(DomainError, match="计费"):
            review.run_http(second["id"])
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_multiple_nonoverlapping_proposals_same_block(library):
    wb, _, _, draft = library
    review = ReviewService(wb)
    request = review.create(draft["revision_id"])
    response = response_for(request)
    second = copy.deepcopy(response["corrections"][0])
    second.update(start=0, end=2, old_text="叶片", new_text="绿色叶片")
    response["corrections"].append(second)
    result = review.receive(request["id"], response)
    review.decide(result["proposal_ids"][1], "accept")
    edited = review.decide(result["proposal_ids"][0], "accept")
    assert any(b["text"] == "绿色叶片通过光合作用制造养分。" for b in edited["revision"]["blocks"])
    wb.validate_revision(edited["revision"])


def test_mixed_book_only_assigned_ranges_sent(library):
    wb, source, _, _ = library
    book = wb.create_book("部分文字")
    block = source["blocks"][-1]
    draft = wb.assemble(book["id"], [{"source_id": source["id"], "block_id": block["id"], "start": 0, "end": 2}])
    request = ReviewService(wb).create(draft["revision_id"])
    sent = request["payload"]["sources"]
    assert len(sent) == 1 and len(sent[0]["blocks"]) == 1
    assert sent[0]["blocks"][0]["spans"] == [{"start": 0, "end": 2, "text": "叶片"}]
    assert "光和作用" not in json.dumps(sent, ensure_ascii=False)


def synthetic_long_book(tmp_path, paragraphs):
    wb = Workbench(tmp_path / "long-project")
    # Markdown deliberately preserves a very long block: the AI slice/rebase
    # contract must also work for formats that do not chunk long TXT paragraphs.
    path = tmp_path / "synthetic-long.md"
    path.write_text("\n\n".join(paragraphs), encoding="utf-8")
    source = wb.source(wb.import_file(path)["source_id"])
    book = wb.create_book("合成长书", author="测试作者")
    draft = wb.assemble(book["id"], [{"source_id": source["id"], "block_id": b["id"]} for b in source["blocks"]])
    return wb, source, book, draft


def slice_response(request, new_text="修订😀", relative=0):
    block = request["payload"]["blocks"][0]
    source = request["payload"]["sources"][0]
    evidence = source["blocks"][0]
    span = evidence["spans"][0]
    start = block["start"] + relative
    return {"corrections": [{"block_id": block["id"], "start": start, "end": start + 1,
            "old_text": block["text"][relative], "new_text": new_text, "reason": "合成区间校订",
            "evidence": [{"source_id": source["id"], "block_id": evidence["id"], "start": span["start"], "end": span["start"] + 1}]}]}


def test_long_book_batches_are_complete_frozen_bounded_and_idempotent(tmp_path):
    wb, source, _, draft = synthetic_long_book(tmp_path, [f"合成段落{i}。" + "树叶😀枝干。" * 150 for i in range(45)])
    review = ReviewService(wb)
    first = review.create(draft["revision_id"])
    assert first["batch_count"] > 1
    assert first["id"] == first["request_ids"][0]
    assert review.create(draft["revision_id"])["request_ids"] == first["request_ids"]
    covered = {}
    for index, request_id in enumerate(first["request_ids"], 1):
        request = review.get(request_id)
        payload = request["payload"]
        assert payload["batch"] == {"index": index, "count": first["batch_count"]}
        assert request["estimated_input_tokens"] + 3000 <= 20000
        evidence = [span for s in payload["sources"] for b in s["blocks"] for span in b["spans"]]
        assert len(evidence) <= 12 and sum(len(s["text"]) for s in evidence) <= 6000
        assert len(evidence) < len(source["blocks"])
        for block in payload["blocks"]:
            covered.setdefault(block["id"], []).append(block)
        # A read-only result for every batch is valid and does not change head.
        review.receive(request_id, {"corrections": [], "observations": ["合成验收"]})
    for original in draft["revision"]["blocks"]:
        slices = sorted(covered[original["id"]], key=lambda b: b["start"])
        assert "".join(b["text"] for b in slices) == original["text"]
        assert [b["start"] for b in slices] == [0] + [b["end"] for b in slices[:-1]]
        assert slices[-1]["end"] == len(original["text"])
    assert wb.project.row("branches", draft["branch_id"])["head_revision_id"] == draft["revision_id"]


def test_long_unicode_block_accepts_batches_in_sequence_with_offset_rebase(tmp_path):
    original = "甲😀乙丙丁。" * 2500
    wb, _, _, draft = synthetic_long_book(tmp_path, [original])
    assert len(draft["revision"]["blocks"]) == 1
    review = ReviewService(wb)
    first = review.create(draft["revision_id"])
    assert first["batch_count"] >= 3
    requests = [review.get(i) for i in first["request_ids"]]
    accepted = []
    for request in requests:
        response = slice_response(request)
        accepted.append((response["corrections"][0]["start"], response["corrections"][0]["new_text"]))
        proposal = review.receive(request["id"], response)["proposal_ids"][0]
        edited = review.decide(proposal, "accept")
    expected = original
    for start, text in reversed(accepted):
        expected = expected[:start] + text + expected[start + 1:]
    assert edited["revision"]["blocks"][0]["text"] == expected
    wb.validate_revision(edited["revision"])
    # Preparing a later review follows copied and edited ancestor ranges.
    later = review.create(edited["revision_id"])
    assert later["payload"]["sources"]
    assert later["estimated_input_tokens"] + 3000 <= 20000


@pytest.mark.parametrize("bad", ["target", "local_coordinates", "evidence", "boundary_insert"])
def test_wrong_slice_target_or_evidence_is_rejected(tmp_path, bad):
    wb, _, _, draft = synthetic_long_book(tmp_path, ["甲😀乙丙丁。" * 1800])
    review = ReviewService(wb)
    group = review.create(draft["revision_id"])
    first, last = [review.get(group["request_ids"][i]) for i in (0, -1)]
    request = last if bad == "local_coordinates" else first
    response = slice_response(request)
    correction = response["corrections"][0]
    if bad == "target":
        correction.update(start=last["payload"]["blocks"][0]["start"], end=last["payload"]["blocks"][0]["start"] + 1)
    elif bad == "local_coordinates":
        correction.update(start=0, end=1)
    elif bad == "evidence":
        correction["evidence"] = slice_response(last)["corrections"][0]["evidence"]
    else:
        stop = first["payload"]["blocks"][0]["end"]
        correction.update(start=stop, end=stop, old_text="", new_text="插入")
    with pytest.raises(DomainError) as caught:
        review.receive(request["id"], response)
    assert caught.value.code == ("ai_evidence" if bad == "evidence" else "ai_span")
    assert not wb.state()["proposals"]


def test_batch_rebase_refuses_human_change_and_different_frozen_base(tmp_path):
    wb, _, _, draft = synthetic_long_book(tmp_path, ["甲😀乙丙丁。" * 1200])
    review = ReviewService(wb)
    group = review.create(draft["revision_id"])
    first, last = [review.get(group["request_ids"][i]) for i in (0, -1)]
    p1 = review.receive(first["id"], slice_response(first))["proposal_ids"][0]
    p2 = review.receive(last["id"], slice_response(last))["proposal_ids"][0]
    edited = review.decide(p1, "accept")
    newer = review.create(edited["revision_id"])
    pn = review.receive(newer["id"], slice_response(newer, "新", relative=10))["proposal_ids"][0]
    newer_edit = review.decide(pn, "accept")
    with pytest.raises(DomainError) as conflict:
        review.decide(p2, "accept")
    assert conflict.value.status == 409
    wb.select_revision(draft["branch_id"], newer_edit["revision_id"], edited["revision_id"])
    block = edited["revision"]["blocks"][0]
    wb.edit(draft["branch_id"], edited["revision_id"], [{"block_id": block["id"], "start": 30, "end": 31, "text": "人"}])
    with pytest.raises(DomainError) as conflict:
        review.decide(p2, "accept")
    assert conflict.value.status == 409


def test_budget_failure_is_atomic_and_rechecked_before_http(library):
    wb, _, _, draft = library
    review = ReviewService(wb)
    wb.project.save_settings({"ai": {"token_budget": 3010}})
    with pytest.raises(DomainError, match="预算"):
        review.create(draft["revision_id"])
    assert not wb.state()["review_requests"]
    wb.project.save_settings({"ai": {"token_budget": 20000}})
    request = review.create(draft["revision_id"], provider="http")
    wb.project.save_settings({"ai": {"token_budget": 3010, "endpoint": "http://127.0.0.1:1/chat/completions", "model": "never-contacted"}})
    with pytest.raises(DomainError) as caught:
        review.run_http(request["id"])
    assert caught.value.code == "ai_budget"
    assert review.get(request["id"])["status"] == "ready"


def test_http_api_enqueues_each_frozen_batch_once(tmp_path):
    from fastapi.testclient import TestClient
    from yuecang.api import create_app
    wb, _, _, draft = synthetic_long_book(tmp_path, ["甲😀乙丙丁。" * 1200])
    client = TestClient(create_app(wb.project.root, start_worker=False))
    headers = {"X-Yuecang-CSRF": client.get("/api/session").json()["csrf_token"]}
    path = f"/api/revisions/{draft['revision_id']}/review"
    response = client.post(path, headers=headers, json={"provider": "http"})
    assert response.status_code == 200
    group = response.json()
    assert group["batch_count"] > 1
    assert len(group["jobs"]) == group["batch_count"] == len(group["request_ids"])
    assert group["job"] == group["jobs"][0]
    repeated = client.post(path, headers=headers, json={"provider": "http"}).json()
    assert [j["id"] for j in repeated["jobs"]] == [j["id"] for j in group["jobs"]]
    assert len(wb.state()["jobs"]) == group["batch_count"]


def test_nearby_alternative_sources_are_included_but_other_editions_are_not(library, tmp_path):
    wb, source_a, book, draft = library
    alternatives = []
    for index, text in enumerate(("叶片通过光合作用制造养分。", "叶片通过光合作用制造养分，也需要水。", "叶片通过光合作用制造养分。另一译本秘密标记")):
        path = tmp_path / f"synthetic-version-{index}.txt"
        path.write_text(text, encoding="utf-8")
        source = wb.source(wb.import_file(path)["source_id"])
        edition = draft["revision"]["edition_id"] if index < 2 else wb.create_edition(book["id"], label="其他译本")["id"]
        wb.assign(book_id=book["id"], spans=[{"source_id": source["id"], "block_id": b["id"]} for b in source["blocks"]], edition_id=edition)
        alternatives.append(source)
    review = ReviewService(wb)
    group = review.create(draft["revision_id"])
    target = next(review.get(i) for i in group["request_ids"] if any("光和作用" in b["text"] for b in review.get(i)["payload"]["blocks"]))
    sent = target["payload"]["sources"]
    assert {source_a["id"], alternatives[0]["id"], alternatives[1]["id"]} <= {s["id"] for s in sent}
    assert alternatives[2]["id"] not in {s["id"] for s in sent}
    assert "另一译本秘密标记" not in json.dumps(target["payload"], ensure_ascii=False)
    response = response_for(target)
    evidence = next(s for s in sent if s["id"] == alternatives[0]["id"])["blocks"][0]
    response["corrections"][0]["evidence"] = [{"source_id": alternatives[0]["id"], "block_id": evidence["id"], "start": 0, "end": 4}]
    proposal = review.receive(target["id"], response)["proposal_ids"][0]
    assert "光合作用" in str(review.decide(proposal, "accept")["revision"]["blocks"])


def test_review_after_merge_resolves_revision_evidence_to_assigned_source(library):
    from yuecang.application.merge import fork_revision, merge_branch
    wb, source, _, draft = library
    other = fork_revision(wb, draft["revision_id"], "合成合并分支")
    block = draft["revision"]["blocks"][-1]
    right = wb.edit(other["id"], draft["revision_id"], [{"block_id": block["id"], "start": 0, "end": 2, "text": "绿色叶片"}])
    merged = merge_branch(wb, draft["branch_id"], draft["revision_id"], right["revision_id"])
    request = ReviewService(wb).create(merged["revision_id"])
    assert any(b["text"].startswith("绿色叶片") for b in request["payload"]["blocks"])
    assert any(s["id"] == source["id"] for s in request["payload"]["sources"])
