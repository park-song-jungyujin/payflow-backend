"""enqueue_task의 audience 키워드 인자 회귀 테스트 — payouts 등 기존 호출부는
audience를 안 넘기므로 기본 동작(OIDC_AUDIENCE 기반 URL/토큰)이 그대로 유지돼야
한다. src/guards/tasks.py는 C 소유이므로 이 테스트는 안전망 역할이다."""

import json

import pytest

from src.guards import tasks as guards_tasks


class FakeClient:
    def __init__(self):
        self.captured = {}

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, parent, task):
        self.captured["parent"] = parent
        self.captured["task"] = task


@pytest.fixture
def queue_env(monkeypatch):
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.setenv("TASKS_SERVICE_ACCOUNT_EMAIL", "tasks@payflow-test.iam.gserviceaccount.com")


def test_default_audience_still_uses_oidc_audience_env(queue_env, monkeypatch):
    """audience를 안 넘기면 여전히 OIDC_AUDIENCE로 URL과 토큰 audience를 만든다."""
    fake = FakeClient()
    monkeypatch.setattr(guards_tasks.tasks_v2, "CloudTasksClient", lambda: fake)

    guards_tasks.enqueue_task("/tasks/execute-payout", {"settlement_run_id": "run_1"})

    request = fake.captured["task"]["http_request"]
    assert request["url"] == "https://api.test.invalid/tasks/execute-payout"
    assert json.loads(request["body"]) == {"settlement_run_id": "run_1"}
    assert request["oidc_token"]["audience"] == "https://api.test.invalid"


def test_explicit_audience_overrides_url_and_token(queue_env, monkeypatch):
    """audience를 넘기면 URL과 토큰 audience 둘 다 그 값으로 바뀐다."""
    fake = FakeClient()
    monkeypatch.setattr(guards_tasks.tasks_v2, "CloudTasksClient", lambda: fake)

    guards_tasks.enqueue_task(
        "/agents/claimant/review",
        {"receipt_id": "rct_1"},
        audience="https://payflow-agent.test.invalid",
    )

    request = fake.captured["task"]["http_request"]
    assert request["url"] == "https://payflow-agent.test.invalid/agents/claimant/review"
    assert request["oidc_token"]["audience"] == "https://payflow-agent.test.invalid"
