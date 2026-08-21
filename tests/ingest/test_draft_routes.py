"""schema-contract.md §9 — POST /tasks/apply-claimant-draft.

draft 읽기는 settlements/store.get_agent_draft를 그대로 쓰므로 여기서는
routes.get_agent_draft를 monkeypatch해 그 함수가 정확한 인자로 불리는지만
본다 — 실제 Firestore 읽기 로직은 settlements 쪽 테스트 몫이다. 상태 전이는
routes.apply_claimant_verdict를 monkeypatch해 라우트가 올바른 인자로
위임하는지만 검증한다 — 실제 전이 로직은 tests/ingest/test_draft_apply.py 몫이다.
"""

import pytest
from fastapi import HTTPException

from src.ingest import routes
from src.ingest.drafts import DraftVerdict, InvalidDraftPayload
from src.ingest.store import ReceiptStoreUnavailable


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)


def _draft(**overrides):
    draft = {
        "draft_id": "drf_CLAIMANT:rct_1",
        "agent": "CLAIMANT",
        "target_type": "RECEIPT",
        "target_id": "rct_1",
        "task_id": "CLAIMANT:rct_1",
        "payload": {"needs_requery": False},
    }
    draft.update(overrides)
    return draft


def test_missing_task_id_returns_400():
    with pytest.raises(HTTPException) as exc:
        routes.task_apply_claimant_draft({})
    assert exc.value.status_code == 400


def test_unknown_task_id_returns_404(monkeypatch):
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: None)
    with pytest.raises(HTTPException) as exc:
        routes.task_apply_claimant_draft({"task_id": "CLAIMANT:rct_1"})
    assert exc.value.status_code == 404


def test_wrong_agent_returns_400(monkeypatch):
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: _draft(agent="SAFETY"))
    with pytest.raises(HTTPException) as exc:
        routes.task_apply_claimant_draft({"task_id": "CLAIMANT:rct_1"})
    assert exc.value.status_code == 400


def test_wrong_target_type_returns_400(monkeypatch):
    monkeypatch.setattr(
        routes, "get_agent_draft", lambda task_id: _draft(target_type="SETTLEMENT_RUN")
    )
    with pytest.raises(HTTPException) as exc:
        routes.task_apply_claimant_draft({"task_id": "CLAIMANT:rct_1"})
    assert exc.value.status_code == 400


def test_invalid_payload_returns_200_with_audit_log_and_does_not_apply(monkeypatch):
    monkeypatch.setattr(
        routes, "get_agent_draft", lambda task_id: _draft(payload={"needs_requery": "not-a-bool"})
    )
    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))
    applied = []
    monkeypatch.setattr(
        routes, "apply_claimant_verdict", lambda *a, **kw: applied.append((a, kw)) or "APPLIED"
    )

    result = routes.task_apply_claimant_draft({"task_id": "CLAIMANT:rct_1"})

    assert result == {"status": "ignored", "reason": "invalid_payload"}
    assert applied == []
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "CLAIMANT_DRAFT_APPLY_INVALID_PAYLOAD"


def test_valid_payload_applies_verdict_and_returns_result(monkeypatch):
    monkeypatch.setattr(
        routes,
        "get_agent_draft",
        lambda task_id: _draft(payload={"needs_requery": True, "reason": "amount mismatch"}),
    )
    captured = {}

    def fake_apply(receipt_id, verdict, *, now):
        captured["receipt_id"] = receipt_id
        captured["verdict"] = verdict
        return "REQUERY"

    monkeypatch.setattr(routes, "apply_claimant_verdict", fake_apply)

    result = routes.task_apply_claimant_draft({"task_id": "CLAIMANT:rct_1"})

    assert result == {"status": "ok", "receipt_id": "rct_1", "result": "REQUERY"}
    assert captured["receipt_id"] == "rct_1"
    assert isinstance(captured["verdict"], DraftVerdict)
    assert captured["verdict"].needs_requery is True


def test_get_agent_draft_called_with_task_id(monkeypatch):
    calls = []

    def fake_get(task_id):
        calls.append(task_id)
        return _draft()

    monkeypatch.setattr(routes, "get_agent_draft", fake_get)
    monkeypatch.setattr(routes, "apply_claimant_verdict", lambda *a, **kw: "APPLIED")

    routes.task_apply_claimant_draft({"task_id": "CLAIMANT:rct_1"})

    assert calls == ["CLAIMANT:rct_1"]


@pytest.mark.parametrize("bad_payload", ["a string", ["a", "list"], 123, None])
def test_non_dict_payload_returns_200_with_audit_log_not_500(monkeypatch, bad_payload):
    """F1 리뷰 지적 — payload가 dict가 아니면 drafts.parse_claimant_payload가
    InvalidDraftPayload를 던지고, 라우트는 그 경로를 그대로 흡수해 200 + 감사
    로그로 끝나야 한다. AttributeError가 새 나가 500이 되면 안 된다."""
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: _draft(payload=bad_payload))
    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))
    applied = []
    monkeypatch.setattr(
        routes, "apply_claimant_verdict", lambda *a, **kw: applied.append((a, kw)) or "APPLIED"
    )

    result = routes.task_apply_claimant_draft({"task_id": "CLAIMANT:rct_1"})

    assert result == {"status": "ignored", "reason": "invalid_payload"}
    assert applied == []
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "CLAIMANT_DRAFT_APPLY_INVALID_PAYLOAD"


def test_receipt_store_unavailable_returns_503_with_audit_log(monkeypatch):
    """F2 리뷰 지적 — store.py 도큐스트링대로 ReceiptStoreUnavailable은 호출부가
    503으로 바꿔야 한다. slack_events와 같은 계약."""
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: _draft())
    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))

    def boom(receipt_id, verdict, *, now):
        raise ReceiptStoreUnavailable("Failed to commit transaction in 5 attempts.")

    monkeypatch.setattr(routes, "apply_claimant_verdict", boom)

    with pytest.raises(HTTPException) as exc:
        routes.task_apply_claimant_draft({"task_id": "CLAIMANT:rct_1"})

    assert exc.value.status_code == 503
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "CLAIMANT_DRAFT_APPLY_FAILED"


def test_success_audit_log_failure_falls_back_and_still_returns_ok(monkeypatch):
    """F3 리뷰 지적 — CLAIMANT_DRAFT_APPLIED 감사 로그가 던져도 커밋은 이미
    끝난 뒤라 500으로 뒤집으면 안 된다. store.py의 CLAIM_DEMOTION_BLOCKED와
    같은 이중 폴백 — 실패하면 *_AUDIT_FAILED로 재기록해야 한다."""
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: _draft())
    monkeypatch.setattr(routes, "apply_claimant_verdict", lambda *a, **kw: "APPLIED")

    audit_calls = []

    def failing_audit(**kw):
        audit_calls.append(kw)
        if kw["action"] == "CLAIMANT_DRAFT_APPLIED":
            raise RuntimeError("audit sink down")

    monkeypatch.setattr(routes, "record_audit_log", failing_audit)

    result = routes.task_apply_claimant_draft({"task_id": "CLAIMANT:rct_1"})

    assert result == {"status": "ok", "receipt_id": "rct_1", "result": "APPLIED"}
    assert [c["action"] for c in audit_calls] == [
        "CLAIMANT_DRAFT_APPLIED",
        "CLAIMANT_DRAFT_APPLIED_AUDIT_FAILED",
    ]


def test_success_audit_log_double_failure_logs_and_still_returns_ok(monkeypatch, caplog):
    """감사 로그 재기록마저 실패하면 logging으로 흔적만 남기고 여전히 500이
    되면 안 된다."""
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: _draft())
    monkeypatch.setattr(routes, "apply_claimant_verdict", lambda *a, **kw: "APPLIED")
    monkeypatch.setattr(
        routes,
        "record_audit_log",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("audit sink down")),
    )

    with caplog.at_level("ERROR"):
        result = routes.task_apply_claimant_draft({"task_id": "CLAIMANT:rct_1"})

    assert result == {"status": "ok", "receipt_id": "rct_1", "result": "APPLIED"}
    assert "CLAIMANT_DRAFT_APPLIED audit log failed twice" in caplog.text


def test_oidc_verified_first(monkeypatch):
    """OIDC 없으면 401 — 다른 검사(task_id 등)보다 먼저 걸려야 한다."""
    from src.guards.oidc import verify_oidc as real_verify_oidc

    monkeypatch.setattr(routes, "verify_oidc", real_verify_oidc)  # autouse 스텁을 되돌려 진짜 검증을 탄다
    with pytest.raises(HTTPException) as exc:
        routes.task_apply_claimant_draft({}, authorization="")
    assert exc.value.status_code == 401
