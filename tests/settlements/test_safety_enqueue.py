"""schema-contract.md §9 — /agents/safety/report enqueue 경로와 본문 계약.
tests/settlements/test_enqueue.py와 같은 패턴.

이 파일이 존재하는 이유: test_routes.py는 `enqueue_safety_report`를 통째로
monkeypatch해서 `guards/tasks.py`의 `json.dumps`가 한 번도 안 돌았고, 그래서
run 스냅샷에 datetime이 그대로 실려 매 정산 실행마다 TypeError로 enqueue가
죽던 것을 아무도 못 잡았다. 여기서는 진짜 enqueue_task를 태워서 직렬화까지
확인한다 — 스텁은 Cloud Tasks 클라이언트 하나뿐이다.
"""

import json
from datetime import UTC, datetime

import pytest

from src.guards import tasks as guards_tasks
from src.settlements.routes import _task_safe_run
from src.settlements.safety_enqueue import (
    QueueNotConfigured,
    enqueue_safety_report,
    safety_draft_task_id,
)


def _run_doc() -> dict:
    """create_settlement_run_route가 만드는 doc 그대로 — created_at/updated_at은
    datetime 객체다(Firestore에 Timestamp로 들어가야 하므로 문자열이 아니다)."""
    now = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
    return {
        "settlement_run_id": "run_1",
        "org_id": "org_1",
        "base_currency": "USD",
        "total_amount_minor": 0,
        "fx_rates": {},
        "fx_locked_at": None,
        "approval_amount_hash": None,
        "approval_token_hash": "must-not-leak",
        "retry_seq": 0,
        "status": "DRAFT",
        "created_at": now,
        "updated_at": now,
    }


def test_task_id_is_namespaced_by_agent():
    assert safety_draft_task_id("run_260821_ABC") == "SAFETY:run_260821_ABC"


def test_missing_agent_service_url_raises_explicitly(monkeypatch):
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.delenv("AGENT_SERVICE_URL", raising=False)
    with pytest.raises(QueueNotConfigured, match="AGENT_SERVICE_URL"):
        enqueue_safety_report("run_1", {})


def _wire_tasks(monkeypatch, captured: dict):
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.setenv("AGENT_SERVICE_URL", "https://payflow-agent.test.invalid")
    monkeypatch.setenv("GCP_PROJECT", "payflow-test")
    monkeypatch.setenv("CLOUD_TASKS_LOCATION", "asia-northeast3")
    monkeypatch.setenv("TASKS_SERVICE_ACCOUNT_EMAIL", "tasks@payflow-test.iam.gserviceaccount.com")

    class FakeClient:
        def queue_path(self, project, location, queue):
            return f"projects/{project}/locations/{location}/queues/{queue}"

        def create_task(self, parent, task):
            captured["task"] = task

    monkeypatch.setattr(guards_tasks.tasks_v2, "CloudTasksClient", FakeClient)


def test_run_snapshot_with_datetimes_is_json_serializable(monkeypatch):
    """회귀 방지 — `_public_run(doc)`을 그대로 실으면 datetime 때문에
    `json.dumps`가 TypeError를 낸다. HTTP 응답에서는 FastAPI가 알아서
    직렬화해주지만 태스크 본문은 맨 json.dumps라 같은 헬퍼를 쓸 수 없다."""
    captured = {}
    _wire_tasks(monkeypatch, captured)

    enqueue_safety_report("run_1", _task_safe_run(_run_doc()))

    request = captured["task"]["http_request"]
    assert request["url"] == "https://payflow-agent.test.invalid/agents/safety/report"
    body = json.loads(request["body"])
    snapshot = body["settlement_run_snapshot"]
    assert snapshot["created_at"] == "2026-08-26T09:00:00+00:00"
    assert snapshot["updated_at"] == "2026-08-26T09:00:00+00:00"


def test_snapshot_still_strips_approval_token_hash(monkeypatch):
    """_task_safe_run은 _public_run 위에 얹은 변환이다 — §6 최소화(토큰 해시
    비노출)를 그대로 유지해야 한다."""
    captured = {}
    _wire_tasks(monkeypatch, captured)

    enqueue_safety_report("run_1", _task_safe_run(_run_doc()))

    snapshot = json.loads(captured["task"]["http_request"]["body"])["settlement_run_snapshot"]
    assert "approval_token_hash" not in snapshot


def test_body_matches_contract(monkeypatch):
    captured = {}
    _wire_tasks(monkeypatch, captured)

    enqueue_safety_report("run_1", _task_safe_run(_run_doc()))

    body = json.loads(captured["task"]["http_request"]["body"])
    assert body["settlement_run_id"] == "run_1"
    assert body["task_id"] == "SAFETY:run_1"
