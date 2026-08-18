"""schema-contract.md §10 — /tasks/parse-receipt enqueue 경로와 본문 계약."""

import json

import pytest

from src.guards import tasks as guards_tasks
from src.ingest.enqueue import QueueNotConfigured, enqueue_parse_receipt


def test_missing_queue_raises_explicitly(monkeypatch):
    """큐가 없으면 성공한 척하지 않는다 — 영수증이 조용히 사라지는 걸 막는다."""
    monkeypatch.delenv("CLOUD_TASKS_QUEUE", raising=False)
    with pytest.raises(QueueNotConfigured):
        enqueue_parse_receipt("rct_01K3M9XQ7B2F4G6H8J0K2M4N6P")


def test_builds_oidc_task_for_parse_route(monkeypatch):
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.setenv("TASKS_SERVICE_ACCOUNT_EMAIL", "tasks@payflow-test.iam.gserviceaccount.com")
    captured = {}

    class FakeClient:
        def queue_path(self, project, location, queue):
            return f"projects/{project}/locations/{location}/queues/{queue}"

        def create_task(self, parent, task):
            captured["parent"] = parent
            captured["task"] = task

    monkeypatch.setattr(guards_tasks.tasks_v2, "CloudTasksClient", FakeClient)
    enqueue_parse_receipt("rct_01K3M9XQ7B2F4G6H8J0K2M4N6P")

    request = captured["task"]["http_request"]
    assert request["url"] == "https://api.test.invalid/tasks/parse-receipt"
    assert json.loads(request["body"]) == {"receipt_id": "rct_01K3M9XQ7B2F4G6H8J0K2M4N6P"}
    assert request["oidc_token"]["audience"] == "https://api.test.invalid"
    assert captured["parent"].endswith("/queues/payflow-queue")
