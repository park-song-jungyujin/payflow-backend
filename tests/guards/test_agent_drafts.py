"""schema-contract.md §9 — POST /agents/drafts. Task 3 hook: CLAIMANT draft
쓰기가 /tasks/apply-claimant-draft enqueue를 촉발하는지, EXECUTOR에서는 안
촉발하는지, 큐 미설정이어도 draft 쓰기 자체는 여전히 200을 돌려주는지 검증한다."""

import pytest
from fastapi import HTTPException

from src.guards import agent_drafts


class FakeDocRef:
    def __init__(self, store, doc_id):
        self._store, self._doc_id = store, doc_id

    def set(self, data):
        self._store[self._doc_id] = data


class FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return FakeDocRef(self._store, doc_id)


class FakeClient:
    def __init__(self):
        self.data = {}

    def collection(self, name):
        assert name == "agent_drafts"
        return FakeCollection(self.data)


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(agent_drafts, "verify_oidc", lambda auth: {})
    client = FakeClient()
    monkeypatch.setattr(agent_drafts, "get_client", lambda: client)
    audit_calls = []
    monkeypatch.setattr(agent_drafts, "record_audit_log", lambda **kw: audit_calls.append(kw))
    return {"client": client, "audit_calls": audit_calls}


def _body(**overrides):
    body = {
        "agent": "CLAIMANT",
        "target_type": "RECEIPT",
        "target_id": "rct_1",
        "task_id": "CLAIMANT:rct_1",
        "payload": {"needs_requery": False},
    }
    body.update(overrides)
    return body


def test_claimant_draft_enqueues_apply_task(monkeypatch, _patch):
    calls = []
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: calls.append((path, payload)))

    result = agent_drafts.write_agent_draft(_body())

    assert result["status"] == "ok"
    assert calls == [("/tasks/apply-claimant-draft", {"task_id": "CLAIMANT:rct_1"})]


def test_executor_draft_does_not_enqueue(monkeypatch, _patch):
    calls = []
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: calls.append((path, payload)))

    result = agent_drafts.write_agent_draft(
        _body(agent="EXECUTOR", target_type="SETTLEMENT_RUN", target_id="run_1", task_id="EXECUTOR:run_1")
    )

    assert result["status"] == "ok"
    assert calls == []


def test_queue_not_configured_still_returns_200_and_logs_audit(monkeypatch, _patch):
    def boom(path, payload):
        raise Exception("CLOUD_TASKS_QUEUE not configured")

    monkeypatch.setattr(agent_drafts, "enqueue_task", boom)

    result = agent_drafts.write_agent_draft(_body())

    assert result == {"draft_id": "drf_CLAIMANT:rct_1", "status": "ok"}
    audit_calls = _patch["audit_calls"]
    actions = [c["action"] for c in audit_calls]
    assert "AGENT_DRAFT_WRITTEN" in actions
    assert "CLAIMANT_DRAFT_APPLY_ENQUEUE_FAILED" in actions
    failed_call = next(c for c in audit_calls if c["action"] == "CLAIMANT_DRAFT_APPLY_ENQUEUE_FAILED")
    assert failed_call["after"] == {"draft_id": "drf_CLAIMANT:rct_1", "task_id": "CLAIMANT:rct_1"}


def test_draft_document_and_original_audit_log_unchanged(monkeypatch, _patch):
    """훅이 붙어도 draft 문서 내용과 기존 AGENT_DRAFT_WRITTEN 감사 로그는 그대로여야 한다."""
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: None)

    result = agent_drafts.write_agent_draft(_body())

    client = _patch["client"]
    stored = client.data["CLAIMANT:rct_1"]
    assert stored["draft_id"] == "drf_CLAIMANT:rct_1"
    assert stored["agent"] == "CLAIMANT"
    assert stored["target_type"] == "RECEIPT"
    assert stored["target_id"] == "rct_1"
    assert stored["task_id"] == "CLAIMANT:rct_1"
    assert stored["payload"] == {"needs_requery": False}
    assert result == {"draft_id": "drf_CLAIMANT:rct_1", "status": "ok"}

    audit_calls = _patch["audit_calls"]
    written = next(c for c in audit_calls if c["action"] == "AGENT_DRAFT_WRITTEN")
    assert written["actor"] == "agent/claimant"
    assert written["after"] == {"draft_id": "drf_CLAIMANT:rct_1"}


def test_missing_authorization_returns_401(monkeypatch, _patch):
    from src.guards.oidc import verify_oidc as real_verify_oidc

    monkeypatch.setattr(agent_drafts, "verify_oidc", real_verify_oidc)
    with pytest.raises(HTTPException) as exc:
        agent_drafts.write_agent_draft(_body(), authorization="")
    assert exc.value.status_code == 401
