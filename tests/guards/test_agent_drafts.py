"""schema-contract.md §9 — POST /agents/drafts. Task 3 hook: CLAIMANT draft
쓰기가 /tasks/apply-claimant-draft enqueue를 촉발하는지, 다른 에이전트
(SAFETY·EXECUTOR)에서는 안 촉발하는지, 큐 미설정이어도 draft 쓰기 자체는
여전히 200을 돌려주는지 검증한다."""

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


def test_safety_draft_does_not_enqueue(monkeypatch, _patch):
    calls = []
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: calls.append((path, payload)))

    result = agent_drafts.write_agent_draft(
        _body(agent="SAFETY", target_type="SETTLEMENT_RUN", target_id="run_1", task_id="SAFETY:run_1")
    )

    assert result["status"] == "ok"
    assert calls == []


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


# --- Gemma 번역 배선 — payflow-agent는 한국어만 쓰고, 여기서 en 필드를 채운다 ---


def test_claimant_requery_message_gets_translated(monkeypatch, _patch):
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: None)
    calls = []
    monkeypatch.setattr(
        agent_drafts,
        "translate_lines",
        lambda texts: calls.append(texts) or ["Please resend the receipt."],
    )

    agent_drafts.write_agent_draft(
        _body(payload={"needs_requery": True, "requery_message": "영수증을 다시 보내주세요."})
    )

    assert calls == [["영수증을 다시 보내주세요."]]
    stored = _patch["client"].data["CLAIMANT:rct_1"]
    assert stored["payload"]["requery_message_en"] == "Please resend the receipt."


def test_claimant_no_requery_message_skips_translation(monkeypatch, _patch):
    """needs_requery=False처럼 requery_message가 없으면 번역 호출 자체를 안 한다."""
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: None)
    calls = []
    monkeypatch.setattr(agent_drafts, "translate_lines", lambda texts: calls.append(texts) or [])

    agent_drafts.write_agent_draft(_body(payload={"needs_requery": False}))

    assert calls == []


def test_claimant_translation_failure_keeps_korean_draft(monkeypatch, _patch):
    """번역이 실패해도(None) 원본 한국어 draft 쓰기는 막지 않는다."""
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: None)
    monkeypatch.setattr(agent_drafts, "translate_lines", lambda texts: None)

    result = agent_drafts.write_agent_draft(
        _body(payload={"needs_requery": True, "requery_message": "영수증을 다시 보내주세요."})
    )

    assert result["status"] == "ok"
    stored = _patch["client"].data["CLAIMANT:rct_1"]
    assert "requery_message_en" not in stored["payload"]
    assert stored["payload"]["requery_message"] == "영수증을 다시 보내주세요."


def test_executor_payload_passes_through_without_calling_gemma(monkeypatch, _patch):
    """executor/agent.py가 이제 summary_text_en·anomalies_en을 직접 써서 보낸다 —
    이 라우트는 Gemma를 부르지 않고 payload를 그대로 통과시킨다(지연 단축,
    guards/agent_drafts.py._with_translated_fields 참조)."""
    calls = []
    monkeypatch.setattr(agent_drafts, "translate_lines", lambda texts: calls.append(texts) or [])

    result = agent_drafts.write_agent_draft(
        _body(
            agent="EXECUTOR",
            target_type="SETTLEMENT_RUN",
            target_id="run_1",
            task_id="EXECUTOR:run_1",
            payload={
                "summary_text": "요약",
                "anomalies": ["이상징후 1", "이상징후 2"],
                "summary_text_en": "summary in english",
                "anomalies_en": ["anomaly 1 in english", "anomaly 2 in english"],
            },
        )
    )

    assert result["status"] == "ok"
    assert calls == []
    stored = _patch["client"].data["EXECUTOR:run_1"]
    assert stored["payload"]["summary_text_en"] == "summary in english"
    assert stored["payload"]["anomalies_en"] == ["anomaly 1 in english", "anomaly 2 in english"]


def test_safety_draft_is_never_translated(monkeypatch, _patch):
    """safety는 아직 아무 데도 안 보이는 출력이다 — 번역 대상이 아니다."""
    calls = []
    monkeypatch.setattr(agent_drafts, "translate_lines", lambda texts: calls.append(texts) or [])

    agent_drafts.write_agent_draft(
        _body(
            agent="SAFETY",
            target_type="SETTLEMENT_RUN",
            target_id="run_1",
            task_id="SAFETY:run_1",
            payload={"risk_report": "위험 없음"},
        )
    )

    assert calls == []
