"""schema-contract.md §10 POST /settlements/runs — verify_candidates 이후 흐름
(claim 요약 · 중복 판정 · 집행자 에이전트 enqueue)을 검증한다.

tests/guards/test_routes.py와 같은 패턴 — TestClient/ASGI를 거치지 않고 라우트
핸들러 함수를 직접 부른다. select_claims_for_run/verify_candidates/Firestore
쓰기/enqueue를 전부 모듈 레벨에서 monkeypatch한다.
"""

from datetime import date

import pytest
from fastapi import HTTPException

from src.settlements import routes


def _claim(claim_id, receipt_id="rct_1", **overrides):
    claim = {
        "claim_id": claim_id,
        "recipient_id": "rcp_1",
        "receipt_id": receipt_id,
        "amount_minor": 10000,
        "currency": "KRW",
        "account_category_code": "TRAVEL",
    }
    claim.update(overrides)
    return claim


class _FakeStub:
    """create_settlement_run/link_claims_to_run 호출만 기록한다."""

    def __init__(self):
        self.created = []
        self.linked = []

    def create(self, run_id, doc):
        self.created.append((run_id, doc))

    def link(self, run_id, claim_ids):
        self.linked.append((run_id, claim_ids))


def _wire(monkeypatch, *, claims, receipts, enqueue_error=None):
    monkeypatch.setattr(
        routes,
        "verify_session",
        lambda token: {"executor_id": "exe_1", "org_id": "org_1", "email": "alice@example.com"},
    )
    monkeypatch.setattr(routes, "select_claims_for_run", lambda org_id, filter: claims)
    monkeypatch.setattr(
        routes,
        "verify_candidates",
        lambda candidates: {"passed_claims": claims, "failed_claims": [], "receipts": receipts},
    )

    stub = _FakeStub()
    monkeypatch.setattr(routes, "create_settlement_run", stub.create)
    monkeypatch.setattr(routes, "link_claims_to_run", stub.link)

    enqueue_calls = []

    def fake_enqueue(run_id, claim_summaries, duplicate_groups):
        enqueue_calls.append((run_id, claim_summaries, duplicate_groups))
        if enqueue_error:
            raise enqueue_error

    monkeypatch.setattr(routes, "enqueue_executor_analyze", fake_enqueue)

    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))

    return stub, enqueue_calls, audit_calls


def test_create_run_enqueues_executor_analyze_with_claim_summaries(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    stub, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts=receipts)

    result = routes.create_settlement_run_route(body={}, authorization="Bearer t")

    assert len(enqueue_calls) == 1
    run_id, claim_summaries, duplicate_groups = enqueue_calls[0]
    assert run_id == result["settlement_run_id"]
    assert claim_summaries == [
        {
            "claim_id": "clm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 10000,
            "currency": "KRW",
            "account_category_code": "TRAVEL",
            "merchant_name": "스타벅스",
            "transaction_date": "2026-08-10",
        }
    ]
    assert duplicate_groups == []  # claim 1건뿐이라 중복 그룹이 안 생긴다


def test_create_run_finds_duplicate_group_among_passed_claims(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)},
        "rct_2": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)},
    }
    _, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts=receipts)

    routes.create_settlement_run_route(body={}, authorization="Bearer t")

    _, _, duplicate_groups = enqueue_calls[0]
    assert len(duplicate_groups) == 1
    assert set(duplicate_groups[0]["claim_ids"]) == {"clm_1", "clm_2"}


def test_missing_receipt_produces_null_merchant_and_date(monkeypatch):
    """receipt가 없는 claim도(이론상 발생하지 않아야 하지만) 요약 생성이 죽지 않는다."""
    claims = [_claim("clm_1", receipt_id="rct_missing")]
    _, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts={})

    routes.create_settlement_run_route(body={}, authorization="Bearer t")

    _, claim_summaries, _ = enqueue_calls[0]
    assert claim_summaries[0]["merchant_name"] is None
    assert claim_summaries[0]["transaction_date"] is None


def test_enqueue_failure_does_not_break_run_creation(monkeypatch):
    """집행자 분석은 조언일 뿐이다 — enqueue가 실패해도 정산 실행은 정상 생성된다."""
    claims = [_claim("clm_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    stub, enqueue_calls, audit_calls = _wire(
        monkeypatch, claims=claims, receipts=receipts, enqueue_error=RuntimeError("boom")
    )

    result = routes.create_settlement_run_route(body={}, authorization="Bearer t")

    assert result["status"] == "DRAFT"
    assert len(stub.created) == 1  # 배치는 정상적으로 만들어졌다
    assert audit_calls == [
        {
            "actor": "api/src/settlements",
            "action": "EXECUTOR_ENQUEUE_FAILED",
            "run_id": result["settlement_run_id"],
            "reason": "boom",
            "after": {"settlement_run_id": result["settlement_run_id"]},
        }
    ]


def test_audit_log_failure_does_not_mask_response(monkeypatch):
    """감사 로그 자체가 죽어도(Firestore 장애 등) 응답은 그대로 나간다."""
    claims = [_claim("clm_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    _wire(monkeypatch, claims=claims, receipts=receipts, enqueue_error=RuntimeError("boom"))
    monkeypatch.setattr(
        routes,
        "record_audit_log",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("firestore down")),
    )

    result = routes.create_settlement_run_route(body={}, authorization="Bearer t")

    assert result["status"] == "DRAFT"


def test_empty_candidate_batch_enqueues_with_empty_lists(monkeypatch):
    _, enqueue_calls, _ = _wire(monkeypatch, claims=[], receipts={})

    routes.create_settlement_run_route(body={}, authorization="Bearer t")

    _, claim_summaries, duplicate_groups = enqueue_calls[0]
    assert claim_summaries == []
    assert duplicate_groups == []


# --- GET /settlements/runs/{run_id} — agent_drafts.EXECUTOR 읽기 (Part 5) ---


def _run_doc(**overrides):
    run = {
        "settlement_run_id": "run_1",
        "org_id": "org_1",
        "status": "DRAFT",
        "approval_token_hash": "secret",
    }
    run.update(overrides)
    return run


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    monkeypatch.setattr(
        routes,
        "verify_session",
        lambda token: {"executor_id": "exe_1", "org_id": "org_1", "email": "alice@example.com"},
    )


def test_get_run_returns_none_analysis_when_no_draft_written_yet(monkeypatch):
    """None은 "아직 분석 안 됨"이다 — anomalies가 빈 리스트인 "이상 없음"과
    구분해야 한다. web이 "분석 대기 중" vs "이상 없음"을 다르게 렌더링할 수 있게."""
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: _run_doc())
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: None)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["executor_analysis"] is None


def test_get_run_includes_analysis_when_draft_exists(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: _run_doc())

    captured_task_id = []

    def fake_get_draft(task_id):
        captured_task_id.append(task_id)
        return {
            "payload": {
                "anomalies": ["같은 가맹점·같은 금액 2건"],
                "summary_text": "중복 의심 1건",
            },
            "created_at": "2026-08-21T00:00:00Z",
        }

    monkeypatch.setattr(routes, "get_agent_draft", fake_get_draft)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    # enqueue.py의 task_id 네임스페이스와 정확히 같은 값으로 조회해야 한다 —
    # 다르면 존재하는 draft를 못 찾고 항상 None이 나온다.
    assert captured_task_id == ["EXECUTOR:run_1"]
    assert result["executor_analysis"] == {
        "anomalies": ["같은 가맹점·같은 금액 2건"],
        "summary_text": "중복 의심 1건",
        "created_at": "2026-08-21T00:00:00Z",
    }


def test_get_run_404_does_not_read_agent_draft(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: None)
    monkeypatch.setattr(
        routes, "get_agent_draft", lambda task_id: (_ for _ in ()).throw(AssertionError())
    )

    with pytest.raises(HTTPException) as exc_info:
        routes.get_settlement_run_route("run_missing", authorization="Bearer t")
    assert exc_info.value.status_code == 404


def test_get_run_still_strips_approval_token_hash(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: _run_doc())
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: None)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert "approval_token_hash" not in result
