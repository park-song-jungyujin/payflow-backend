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


# --- GET /settlements/unsettled-claims — web 왼쪽 파트, 정산 실행에 아직
# 안 들어간 확정 청구 조회 전용(선택 UI 없음, 단순 조회) ---


def _wire_session(monkeypatch, *, org_id="org_1"):
    monkeypatch.setattr(
        routes,
        "verify_session",
        lambda token: {"executor_id": "exe_1", "org_id": org_id, "email": "alice@example.com"},
    )


def test_list_unsettled_claims_returns_summaries_with_requester_name(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1")]
    receipts = {
        "rct_1": {
            "merchant_name": "스타벅스",
            "transaction_date": date(2026, 8, 10),
            "items": [{"name": "아메리카노", "amount_minor": 4500}],
        }
    }
    _wire_session(monkeypatch, org_id="org_1")
    captured_args = []
    monkeypatch.setattr(
        routes,
        "select_claims_for_run",
        lambda org_id, filter: captured_args.append((org_id, filter)) or claims,
    )
    monkeypatch.setattr(routes, "get_receipts", lambda receipt_ids: receipts)
    monkeypatch.setattr(routes, "get_recipient", lambda recipient_id: {"display_name": "박수현"})

    result = routes.list_unsettled_claims(authorization="Bearer t")

    # 필터 없이 이 세션의 org 전체 CONFIRMED claims를 본다 — 정산 실행 생성용
    # SettlementFilter와 같은 선정 로직을 재사용하지만 여기서는 조건을 하나도
    # 안 걸고, org_id는 세션에서 검증된 값만 쓴다(클라이언트가 못 정한다).
    assert captured_args == [("org_1", routes.SettlementFilter())]
    assert result == {
        "claims": [
            {
                "claim_id": "clm_1",
                "recipient_id": "rcp_1",
                "amount_minor": 10000,
                "currency": "KRW",
                "account_category_code": "TRAVEL",
                "merchant_name": "스타벅스",
                "transaction_date": "2026-08-10",
                "recipient_name": "박수현",
                "items": [{"name": "아메리카노", "amount_minor": 4500}],
            }
        ]
    }


def test_list_unsettled_claims_does_not_call_verification(monkeypatch):
    """조회 화면이라 Gemini 검증 단발 호출(비용·지연)을 돌리지 않는다 —
    verify_candidates를 아예 안 부르는지가 이 테스트의 계약이다."""
    _wire_session(monkeypatch)
    monkeypatch.setattr(routes, "select_claims_for_run", lambda org_id, filter: [])
    monkeypatch.setattr(routes, "get_receipts", lambda receipt_ids: {})

    def boom(candidates):
        raise AssertionError("verify_candidates는 조회 경로에서 불리면 안 된다")

    monkeypatch.setattr(routes, "verify_candidates", boom)

    assert routes.list_unsettled_claims(authorization="Bearer t") == {"claims": []}


def test_list_unsettled_claims_empty_when_nothing_confirmed(monkeypatch):
    _wire_session(monkeypatch)
    monkeypatch.setattr(routes, "select_claims_for_run", lambda org_id, filter: [])
    monkeypatch.setattr(routes, "get_receipts", lambda receipt_ids: {})

    assert routes.list_unsettled_claims(authorization="Bearer t") == {"claims": []}


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

    def fake_enqueue(run_id, claim_summaries, duplicate_groups, exact_duplicate_groups):
        enqueue_calls.append((run_id, claim_summaries, duplicate_groups, exact_duplicate_groups))
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
    run_id, claim_summaries, duplicate_groups, exact_duplicate_groups = enqueue_calls[0]
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
    assert exact_duplicate_groups == []  # receipt_serial_number가 없으니 판정 대상도 없다


def test_create_run_finds_duplicate_group_among_passed_claims(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)},
        "rct_2": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)},
    }
    _, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts=receipts)

    routes.create_settlement_run_route(body={}, authorization="Bearer t")

    _, _, duplicate_groups, _ = enqueue_calls[0]
    assert len(duplicate_groups) == 1
    assert set(duplicate_groups[0]["claim_ids"]) == {"clm_1", "clm_2"}


def test_missing_receipt_produces_null_merchant_and_date(monkeypatch):
    """receipt가 없는 claim도(이론상 발생하지 않아야 하지만) 요약 생성이 죽지 않는다."""
    claims = [_claim("clm_1", receipt_id="rct_missing")]
    _, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts={})

    routes.create_settlement_run_route(body={}, authorization="Bearer t")

    _, claim_summaries, _, _ = enqueue_calls[0]
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


def test_empty_candidate_batch_rejected_without_creating_run(monkeypatch):
    """청구 항목이 없으면 run 자체를 만들지 않는다 — 승인 불가능한 빈 run이
    목록에 쌓이는 걸 막는다."""
    stub, enqueue_calls, _ = _wire(monkeypatch, claims=[], receipts={})

    with pytest.raises(HTTPException) as exc_info:
        routes.create_settlement_run_route(body={}, authorization="Bearer t")

    assert exc_info.value.status_code == 400
    assert stub.created == []
    assert stub.linked == []
    assert enqueue_calls == []


# --- GET /settlements/runs/{run_id} — agent_drafts.EXECUTOR 읽기 (Part 5) +
# claim 상세(plans/2026-08-21-web-dashboard.md "필요한 백엔드 변경 (a)") ---


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


def _wire_get(monkeypatch, *, run=None, draft=None, claims=None, receipts=None, recipients=None):
    """GET 테스트 공통 배선. claims/receipts를 안 넘기면 빈 값 — Part 5 테스트들이
    claim 조회를 신경 안 써도 되게 기본값을 둔다."""
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: run if run is not None else _run_doc())
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: draft)
    monkeypatch.setattr(routes, "get_claims_for_run", lambda run_id: claims or [])
    monkeypatch.setattr(routes, "get_receipts", lambda receipt_ids: receipts or {})
    monkeypatch.setattr(routes, "get_recipient", lambda recipient_id: (recipients or {}).get(recipient_id))


def test_get_run_returns_none_analysis_when_no_draft_written_yet(monkeypatch):
    """None은 "아직 분석 안 됨"이다 — anomalies가 빈 리스트인 "이상 없음"과
    구분해야 한다. web이 "분석 대기 중" vs "이상 없음"을 다르게 렌더링할 수 있게."""
    _wire_get(monkeypatch)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["executor_analysis"] is None


def test_get_run_includes_analysis_when_draft_exists(monkeypatch):
    draft = {
        "payload": {
            "anomalies": ["같은 가맹점·같은 금액 2건"],
            "summary_text": "중복 의심 1건",
            "anomalies_en": ["Same merchant, same amount, 2 claims"],
            "summary_text_en": "1 suspected duplicate",
        },
        "created_at": "2026-08-21T00:00:00Z",
    }
    captured_task_id = []
    _wire_get(monkeypatch, draft=draft)
    monkeypatch.setattr(
        routes,
        "get_agent_draft",
        lambda task_id: captured_task_id.append(task_id) or draft,
    )

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    # enqueue.py의 task_id 네임스페이스와 정확히 같은 값으로 조회해야 한다 —
    # 다르면 존재하는 draft를 못 찾고 항상 None이 나온다.
    assert captured_task_id == ["EXECUTOR:run_1"]
    assert result["executor_analysis"] == {
        "anomalies": ["같은 가맹점·같은 금액 2건"],
        "summary_text": "중복 의심 1건",
        "anomalies_en": ["Same merchant, same amount, 2 claims"],
        "summary_text_en": "1 suspected duplicate",
        "created_at": "2026-08-21T00:00:00Z",
    }


def test_get_run_analysis_defaults_english_fields_for_old_drafts(monkeypatch):
    """anomalies_en/summary_text_en 추가 전에 쓰인 draft에는 이 필드가 없다 —
    없어도 죽지 않고 빈 값으로 채워져야 한다."""
    draft = {
        "payload": {
            "anomalies": ["같은 가맹점·같은 금액 2건"],
            "summary_text": "중복 의심 1건",
        },
        "created_at": "2026-08-21T00:00:00Z",
    }
    _wire_get(monkeypatch, draft=draft)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["executor_analysis"]["anomalies_en"] == []
    assert result["executor_analysis"]["summary_text_en"] is None


def test_get_run_404_does_not_read_agent_draft_or_claims(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: None)
    monkeypatch.setattr(
        routes, "get_agent_draft", lambda task_id: (_ for _ in ()).throw(AssertionError())
    )
    monkeypatch.setattr(
        routes, "get_claims_for_run", lambda run_id: (_ for _ in ()).throw(AssertionError())
    )

    with pytest.raises(HTTPException) as exc_info:
        routes.get_settlement_run_route("run_missing", authorization="Bearer t")
    assert exc_info.value.status_code == 404


def test_get_run_still_strips_approval_token_hash(monkeypatch):
    _wire_get(monkeypatch)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert "approval_token_hash" not in result


def test_get_run_includes_claim_details(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    recipients = {"rcp_1": {"display_name": "유진"}}
    _wire_get(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["claims"] == [
        {
            "claim_id": "clm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 10000,
            "currency": "KRW",
            "account_category_code": "TRAVEL",
            "merchant_name": "스타벅스",
            "transaction_date": "2026-08-10",
            "recipient_name": "유진",
        }
    ]


def test_get_run_claim_recipient_name_falls_back_to_id_when_recipient_missing(monkeypatch):
    """money-safety.md 정신과 같다 — 조용한 누락보다 눈에 보이는 대체값(export.py와
    동일 패턴)."""
    claims = [_claim("clm_1", receipt_id="rct_1")]
    _wire_get(monkeypatch, claims=claims, receipts={"rct_1": {}}, recipients={})

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["claims"][0]["recipient_name"] == "rcp_1"


def test_get_run_claims_empty_when_none_linked(monkeypatch):
    _wire_get(monkeypatch)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["claims"] == []
