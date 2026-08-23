"""schema-contract.md §9 — /agents/executor/analyze enqueue 경로와 본문 계약.
tests/ingest/test_enqueue.py와 같은 패턴."""

import json

import pytest

from src.guards import tasks as guards_tasks
from src.settlements.enqueue import (
    QueueNotConfigured,
    enqueue_executor_analyze,
    executor_draft_task_id,
)


def test_task_id_is_namespaced_by_agent():
    """agent_drafts 문서 ID는 task_id뿐이다(agent 필드는 안 섞는다) — 순수
    run_id를 쓰면 나중에 안전 확인 에이전트가 같은 run_id를 task_id로 쓸 때
    같은 문서를 덮어쓴다. 네임스페이스로 그 충돌을 막는다."""
    assert executor_draft_task_id("run_260821_ABC") == "EXECUTOR:run_260821_ABC"


def test_missing_agent_service_url_raises_explicitly(monkeypatch):
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.delenv("AGENT_SERVICE_URL", raising=False)
    with pytest.raises(QueueNotConfigured, match="AGENT_SERVICE_URL"):
        enqueue_executor_analyze("run_1", [], [], [])


def test_missing_queue_raises_explicitly(monkeypatch):
    monkeypatch.setenv("AGENT_SERVICE_URL", "https://payflow-agent.test.invalid")
    monkeypatch.delenv("CLOUD_TASKS_QUEUE", raising=False)
    with pytest.raises(QueueNotConfigured):
        enqueue_executor_analyze("run_1", [], [], [])


def test_builds_oidc_task_targeting_agent_service(monkeypatch):
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.setenv("AGENT_SERVICE_URL", "https://payflow-agent.test.invalid")
    monkeypatch.setenv("TASKS_SERVICE_ACCOUNT_EMAIL", "tasks@payflow-test.iam.gserviceaccount.com")
    captured = {}

    class FakeClient:
        def queue_path(self, project, location, queue):
            return f"projects/{project}/locations/{location}/queues/{queue}"

        def create_task(self, parent, task):
            captured["task"] = task

    monkeypatch.setattr(guards_tasks.tasks_v2, "CloudTasksClient", FakeClient)

    claims = [{"claim_id": "clm_1"}]
    groups = [{"claim_ids": ["clm_1", "clm_2"]}]
    exact_groups = [{"claim_ids": ["clm_1", "clm_2"], "receipt_serial_number": "A1234"}]
    enqueue_executor_analyze("run_1", claims, groups, exact_groups)

    request = captured["task"]["http_request"]
    assert request["url"] == "https://payflow-agent.test.invalid/agents/executor/analyze"
    assert request["oidc_token"]["audience"] == "https://payflow-agent.test.invalid"
    body = json.loads(request["body"])
    assert body == {
        "settlement_run_id": "run_1",
        "task_id": "EXECUTOR:run_1",
        "candidate_claims": claims,
        "duplicate_groups": groups,
        "exact_duplicate_groups": exact_groups,
    }
