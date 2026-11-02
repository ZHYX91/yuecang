import json

import httpx
import pytest

from test_workbench import library
from yuecang.ai import ReviewService
from yuecang.jobs import JobQueue
from yuecang.storage.project import DomainError


def test_lost_http_result_requires_acknowledgement_then_succeeds_once(library, monkeypatch):
    wb, _, _, draft = library
    wb.project.save_settings({"ai": {"endpoint": "http://127.0.0.1:1/chat/completions", "model": "synthetic-retry-provider"}})
    review = ReviewService(wb)
    request = review.create(draft["revision_id"], provider="http")
    calls = []

    def provider(incoming):
        calls.append({"request_id": incoming.headers["Idempotency-Key"], "body": json.loads(incoming.content)})
        if len(calls) == 1:
            raise httpx.ReadTimeout("synthetic response lost after send", request=incoming)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"corrections": [], "observations": ["合成重试成功"]})}}], "usage": {"total_tokens": 12}})

    client_type = httpx.Client
    transport = httpx.MockTransport(provider)
    monkeypatch.setattr(httpx, "Client", lambda **options: client_type(transport=transport, **options))
    queue = JobQueue(wb.project)
    original = queue.enqueue("review", {"request_id": request["id"]})
    failed = queue.run_one()
    assert failed["state"] == "needs_attention"
    assert review.get(request["id"])["status"] == "needs_attention"
    assert len(calls) == 1
    with pytest.raises(DomainError) as error:
        queue.retry(original["id"])
    assert error.value.code == "job_external_unknown"
    with pytest.raises(DomainError) as error:
        review.run_http(request["id"])
    assert error.value.code == "ai_unknown"
    assert len(calls) == 1
    queued = queue.retry(original["id"], acknowledge_external=True)
    assert queued["checkpoint"]["retry_acknowledged"] is True
    assert review.get(request["id"])["status"] == "ready"
    succeeded = queue.run_one()
    assert succeeded["state"] == "succeeded"
    completed = review.get(request["id"])
    assert completed["status"] == "completed" and completed["response_sha"]
    assert len(calls) == 2
    assert calls[0]["request_id"] == calls[1]["request_id"] == request["id"]
    assert review.run_http(request["id"])["duplicate"] is True
    assert queue.enqueue("review", {"request_id": request["id"]})["state"] == "succeeded"
    assert queue.run_one() is None
    assert len(calls) == 2
