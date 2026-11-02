import pytest

from test_workbench import library
from test_ai import response_for
from yuecang.ai import ReviewService
from yuecang.storage.project import DomainError


def test_pending_high_risk_on_same_branch_survives_other_accepted_edit(library):
    wb, source, _, draft = library
    review = ReviewService(wb)
    request = review.create(draft["revision_id"])
    response = response_for(request)
    block = next(b for b in request["payload"]["blocks"] if "250" in b["text"])
    evidence = next(b for b in source["blocks"] if "250" in b["text"])
    offset = block["text"].index("250") + block["start"]
    response["corrections"].append({"block_id": block["id"], "start": offset, "end": offset + 3, "old_text": "250", "new_text": "2500", "reason": "合成高风险提案，必须人工处理", "evidence": [{"source_id": source["id"], "block_id": evidence["id"], "start": 0, "end": len(evidence["text"])}]})
    outcome = review.receive(request["id"], response)
    changed = review.decide(outcome["proposal_ids"][0], "accept")
    with pytest.raises(DomainError, match="高风险"):
        wb.finalize(changed["revision_id"])
    review.decide(outcome["proposal_ids"][1], "reject")
    assert wb.finalize(changed["revision_id"])["status"] == "final"
