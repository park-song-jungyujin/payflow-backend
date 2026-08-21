"""schema-contract.md §9 — 청구자 에이전트 review enqueue 경로와 본문 계약."""

import json

import pytest

from src.guards import tasks as guards_tasks
from src.parsing.enqueue import QueueNotConfigured, enqueue_claimant_review


def test_missing_queue_raises_explicitly(monkeypatch):
    """큐가 없으면 성공한 척하지 않는다 — 청구자 에이전트 호출이 조용히 사라지는 걸 막는다."""
    monkeypatch.delenv("CLOUD_TASKS_QUEUE", raising=False)
    with pytest.raises(QueueNotConfigured):
        enqueue_claimant_review("rct_01K3M9XQ7B2F4G6H8J0K2M4N6P")


def test_builds_oidc_task_for_claimant_review_route(monkeypatch):
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.setenv("TASKS_SERVICE_ACCOUNT_EMAIL", "tasks@payflow-test.iam.gserviceaccount.com")
    monkeypatch.setenv("AGENT_SERVICE_URL", "https://payflow-agent.test.invalid")
    captured = {}

    class FakeClient:
        def queue_path(self, project, location, queue):
            return f"projects/{project}/locations/{location}/queues/{queue}"

        def create_task(self, parent, task):
            captured["parent"] = parent
            captured["task"] = task

    monkeypatch.setattr(guards_tasks.tasks_v2, "CloudTasksClient", FakeClient)
    enqueue_claimant_review("rct_01K3M9XQ7B2F4G6H8J0K2M4N6P")

    request = captured["task"]["http_request"]
    # payflow-agent는 별도 Cloud Run 서비스라 api 자신의 OIDC_AUDIENCE로는
    # 도달할 수 없다 — URL과 OIDC 토큰 audience 둘 다 AGENT_SERVICE_URL이어야 한다.
    assert request["url"] == "https://payflow-agent.test.invalid/agents/claimant/review"
    assert json.loads(request["body"]) == {"receipt_id": "rct_01K3M9XQ7B2F4G6H8J0K2M4N6P"}
    assert request["oidc_token"]["audience"] == "https://payflow-agent.test.invalid"
    assert captured["parent"].endswith("/queues/payflow-queue")


def test_missing_agent_service_url_raises_queue_not_configured(monkeypatch):
    """AGENT_SERVICE_URL이 없으면 os.environ[...] KeyError로 새 나가지 않고
    QueueNotConfigured로 명시돼야 파이프라인이 PARSED를 유지하는 경로를 탄다."""
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.delenv("AGENT_SERVICE_URL", raising=False)
    with pytest.raises(QueueNotConfigured):
        enqueue_claimant_review("rct_01K3M9XQ7B2F4G6H8J0K2M4N6P")
