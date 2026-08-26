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
    """create_settlement_run/link_claims_to_run_cas 호출만 기록한다. CAS는 기본적으로
    넘겨받은 claim_id 전부를 링크 성공으로 취급한다 — 동시성 충돌 시나리오는
    link_result_override로 일부만 반환하게 만든다."""

    def __init__(self, link_result_override=None):
        self.created = []
        self.linked = []
        self._link_result_override = link_result_override

    def create(self, run_id, doc):
        self.created.append((run_id, doc))

    def link(self, run_id, claim_ids):
        self.linked.append((run_id, claim_ids))
        if self._link_result_override is not None:
            return self._link_result_override
        return list(claim_ids)


def _wire(monkeypatch, *, claims, receipts, enqueue_error=None, settled_claims=None, link_result_override=None):
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
    monkeypatch.setattr(routes, "list_settled_claims", lambda org_id: settled_claims or [])

    stub = _FakeStub(link_result_override=link_result_override)
    monkeypatch.setattr(routes, "create_settlement_run", stub.create)
    monkeypatch.setattr(routes, "link_claims_to_run_cas", stub.link)

    enqueue_calls = []

    def fake_enqueue(
        run_id,
        claim_summaries,
        duplicate_groups,
        exact_duplicate_groups,
        future_dated_claims,
        org_id,
        force_reanalyze=False,
    ):
        enqueue_calls.append(
            (
                run_id,
                claim_summaries,
                duplicate_groups,
                exact_duplicate_groups,
                future_dated_claims,
                org_id,
                force_reanalyze,
            )
        )
        if enqueue_error:
            raise enqueue_error

    monkeypatch.setattr(routes, "enqueue_executor_analyze", fake_enqueue)
    monkeypatch.setattr(routes, "enqueue_safety_report", lambda run_id, snapshot: None)

    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))

    status_calls = []
    monkeypatch.setattr(
        routes,
        "set_executor_analysis_status",
        lambda run_id, status, reason=None: status_calls.append((run_id, status, reason)),
    )
    stub.status_calls = status_calls

    safety_status_calls = []
    monkeypatch.setattr(
        routes,
        "set_safety_report_status",
        lambda run_id, status, reason=None: safety_status_calls.append((run_id, status, reason)),
    )
    stub.safety_status_calls = safety_status_calls

    return stub, enqueue_calls, audit_calls


def test_create_run_enqueues_executor_analyze_with_claim_summaries(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    stub, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts=receipts)

    result = routes.create_settlement_run_route(body={}, authorization="Bearer t")

    assert len(enqueue_calls) == 1
    run_id, claim_summaries, duplicate_groups, exact_duplicate_groups, future_dated_claims, org_id, force_reanalyze = enqueue_calls[0]
    assert run_id == result["settlement_run_id"]
    assert org_id == "org_1"
    assert force_reanalyze is False
    assert claim_summaries == [
        {
            "claim_id": "clm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 10000,
            "currency": "KRW",
            "account_category_code": "TRAVEL",
            "merchant_name": "스타벅스",
            "transaction_date": "2026-08-10",
            "items": [],
            "excluded": False,
            "rejected_reason": None,
            "short_id": "clm_1",
        }
    ]
    assert duplicate_groups == []  # claim 1건뿐이라 중복 그룹이 안 생긴다
    assert exact_duplicate_groups == []  # receipt_serial_number_hash가 없으니 판정 대상도 없다
    assert stub.status_calls == [(run_id, "PROCESSING", None)]
    assert stub.safety_status_calls == [(run_id, "PROCESSING", None)]


def test_create_run_claim_summaries_include_items_for_reject_automation(monkeypatch):
    """청구 반려 자동화(집행자가 개인적 사용 의심 물품을 골라내는 것)에 필요해서
    _claim_summary(§6 최소화 대상)와 달리 여기 얹는다."""
    claims = [_claim("clm_1", receipt_id="rct_1")]
    receipts = {
        "rct_1": {
            "merchant_name": "스타벅스",
            "transaction_date": date(2026, 8, 10),
            "items": [{"name": "아메리카노", "amount_minor": 4500}],
        }
    }
    _, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts=receipts)

    routes.create_settlement_run_route(body={}, authorization="Bearer t")

    _, claim_summaries, _, _, _, _, _ = enqueue_calls[0]
    assert claim_summaries[0]["items"] == [{"name": "아메리카노", "amount_minor": 4500}]


def test_create_run_finds_duplicate_group_among_passed_claims(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)},
        "rct_2": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)},
    }
    _, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts=receipts)

    routes.create_settlement_run_route(body={}, authorization="Bearer t")

    _, _, duplicate_groups, _, _, _, _ = enqueue_calls[0]
    assert len(duplicate_groups) == 1
    assert set(duplicate_groups[0]["claim_ids"]) == {"clm_1", "clm_2"}


def test_missing_receipt_produces_null_merchant_and_date(monkeypatch):
    """receipt가 없는 claim도(이론상 발생하지 않아야 하지만) 요약 생성이 죽지 않는다."""
    claims = [_claim("clm_1", receipt_id="rct_missing")]
    _, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts={})

    routes.create_settlement_run_route(body={}, authorization="Bearer t")

    _, claim_summaries, _, _, _, _, _ = enqueue_calls[0]
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
    assert stub.status_calls == [(result["settlement_run_id"], "FAILED", "boom")]


def test_safety_enqueue_failure_records_failed_status(monkeypatch):
    """집행자 enqueue 실패와 같은 패턴 — 안전 확인은 조언일 뿐이라 enqueue가
    실패해도 정산 실행은 정상 생성되고, agent_drafts.SAFETY에는 FAILED를 남긴다."""
    claims = [_claim("clm_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    stub, _, audit_calls = _wire(monkeypatch, claims=claims, receipts=receipts)
    monkeypatch.setattr(
        routes,
        "enqueue_safety_report",
        lambda run_id, snapshot: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = routes.create_settlement_run_route(body={}, authorization="Bearer t")

    assert result["status"] == "DRAFT"
    assert stub.safety_status_calls == [(result["settlement_run_id"], "FAILED", "boom")]
    assert {
        "actor": "api/src/settlements",
        "action": "SAFETY_ENQUEUE_FAILED",
        "run_id": result["settlement_run_id"],
        "reason": "boom",
        "after": {"settlement_run_id": result["settlement_run_id"]},
    } in audit_calls


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


def test_cas_conflict_excludes_claim_and_logs_audit(monkeypatch):
    """schema-contract.md §2 — 동시에 다른 배치가 먼저 채간 claim(CAS 전이 실패)은
    조용히 배치에서 빠지고, 남은 claim만으로 정산 실행이 만들어진다."""
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)},
        "rct_2": {"merchant_name": "이디야", "transaction_date": date(2026, 8, 11)},
    }
    stub, enqueue_calls, audit_calls = _wire(
        monkeypatch, claims=claims, receipts=receipts, link_result_override=["clm_1"]
    )

    result = routes.create_settlement_run_route(body={}, authorization="Bearer t")

    assert len(stub.created) == 1  # clm_2가 빠졌어도 남은 clm_1로 run은 만들어진다
    run_id, claim_summaries, _, _, _, _, _ = enqueue_calls[0]
    assert [c["claim_id"] for c in claim_summaries] == ["clm_1"]  # clm_2는 엔큐에서도 빠진다
    assert {
        "actor": "api/src/settlements",
        "action": "CLAIM_CAS_CONFLICT",
        "run_id": run_id,
        "reason": "1건이 동시 배치 선점으로 제외됨",
        "after": {"linked_claim_ids": ["clm_1"]},
    } in audit_calls


def test_cas_conflict_on_every_claim_rejects_without_creating_run(monkeypatch):
    """후보 전부가 CAS에 밀리면(전부 다른 배치에 선점) 빈 run을 만들지 않는다."""
    claims = [_claim("clm_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    stub, enqueue_calls, _ = _wire(
        monkeypatch, claims=claims, receipts=receipts, link_result_override=[]
    )

    with pytest.raises(HTTPException) as exc_info:
        routes.create_settlement_run_route(body={}, authorization="Bearer t")

    assert exc_info.value.status_code == 409
    assert stub.created == []
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
    executor_draft = {
        "payload": {
            "anomalies": ["같은 가맹점·같은 금액 2건"],
            "summary_text": "중복 의심 1건",
            "anomalies_en": ["Same merchant, same amount, 2 claims"],
            "summary_text_en": "1 suspected duplicate",
        },
        "created_at": "2026-08-21T00:00:00Z",
    }
    safety_draft = {
        "payload": {"risk_report": "한도 근접 항목 없음"},
        "created_at": "2026-08-21T00:00:01Z",
    }
    drafts_by_task_id = {"EXECUTOR:run_1": executor_draft, "SAFETY:run_1": safety_draft}
    captured_task_id = []
    _wire_get(monkeypatch)
    monkeypatch.setattr(
        routes,
        "get_agent_draft",
        lambda task_id: captured_task_id.append(task_id) or drafts_by_task_id[task_id],
    )

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    # enqueue.py/safety_enqueue.py의 task_id 네임스페이스와 정확히 같은 값으로
    # 조회해야 한다 — 다르면 존재하는 draft를 못 찾고 항상 None이 나온다.
    assert captured_task_id == ["EXECUTOR:run_1", "SAFETY:run_1"]
    assert result["safety_report"] == {
        "status": "DONE",
        "risk_report": "한도 근접 항목 없음",
        "reason": None,
        "created_at": "2026-08-21T00:00:01Z",
    }
    assert result["executor_analysis"] == {
        "status": "DONE",
        "anomalies": ["같은 가맹점·같은 금액 2건"],
        "summary_text": "중복 의심 1건",
        "anomalies_en": ["Same merchant, same amount, 2 claims"],
        "summary_text_en": "1 suspected duplicate",
        "reason": None,
        "created_at": "2026-08-21T00:00:00Z",
    }


def test_get_run_reports_processing_status_before_agent_writes_final_draft(monkeypatch):
    """set_executor_analysis_status(routes.py)가 정산 실행 생성 시점에 미리 써둔
    placeholder — 에이전트가 아직 submit_settlement_analysis를 안 부른 상태다."""
    draft = {"payload": {"status": "PROCESSING"}, "created_at": "2026-08-21T00:00:00Z"}
    _wire_get(monkeypatch, draft=draft)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["executor_analysis"]["status"] == "PROCESSING"
    assert result["executor_analysis"]["anomalies"] == []


def test_get_run_reports_failed_status_when_enqueue_never_started_analysis(monkeypatch):
    draft = {
        "payload": {"status": "FAILED", "reason": "boom"},
        "created_at": "2026-08-21T00:00:00Z",
    }
    _wire_get(monkeypatch, draft=draft)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["executor_analysis"]["status"] == "FAILED"
    # 실패 사유는 이미 Firestore에 저장돼 있다 — web이 "직접 확인해주세요"에서
    # 멈추지 않으려면 응답에 실려 나가야 한다.
    assert result["executor_analysis"]["reason"] == "boom"


def test_get_run_analysis_reason_is_none_when_not_failed(monkeypatch):
    """정상 분석 결과 draft에는 reason이 없다 — 없어도 죽지 않고 None이어야 한다."""
    draft = {
        "payload": {"status": "DONE", "anomalies": [], "summary_text": "이상 없음"},
        "created_at": "2026-08-21T00:00:00Z",
    }
    _wire_get(monkeypatch, draft=draft)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["executor_analysis"]["reason"] is None


def test_get_run_reports_processing_safety_status_before_agent_writes_final_draft(monkeypatch):
    """set_safety_report_status(routes.py)가 정산 실행 생성 시점에 미리 써둔
    placeholder — 안전 확인 에이전트가 아직 submit_risk_report를 안 부른 상태다."""
    drafts_by_task_id = {
        "EXECUTOR:run_1": None,
        "SAFETY:run_1": {"payload": {"status": "PROCESSING"}, "created_at": "2026-08-21T00:00:00Z"},
    }
    _wire_get(monkeypatch)
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: drafts_by_task_id[task_id])

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["safety_report"]["status"] == "PROCESSING"
    assert result["safety_report"]["risk_report"] is None


def test_get_run_reports_failed_safety_status_when_enqueue_never_started_report(monkeypatch):
    drafts_by_task_id = {
        "EXECUTOR:run_1": None,
        "SAFETY:run_1": {
            "payload": {"status": "FAILED", "reason": "boom"},
            "created_at": "2026-08-21T00:00:00Z",
        },
    }
    _wire_get(monkeypatch)
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: drafts_by_task_id[task_id])

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["safety_report"]["status"] == "FAILED"
    assert result["safety_report"]["reason"] == "boom"


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
    receipts = {
        "rct_1": {
            "merchant_name": "스타벅스",
            "transaction_date": date(2026, 8, 10),
            "items": [{"name": "아메리카노", "amount_minor": 4500}],
        }
    }
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
            "items": [{"name": "아메리카노", "amount_minor": 4500}],
            "excluded": False,
            "rejected_reason": None,
        }
    ]


def test_get_run_claim_items_defaults_to_empty_list_when_receipt_has_none(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    _wire_get(monkeypatch, claims=claims, receipts=receipts)

    result = routes.get_settlement_run_route("run_1", authorization="Bearer t")

    assert result["claims"][0]["items"] == []


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


# --- PATCH /settlements/runs/{run_id}/claims/{claim_id}/items/{item_index} —
# 청구 반려(물품 단위 제외). claim.amount_minor는 항상 receipt.parsed_amount_minor
# 기준으로 다시 계산한다 — 누적 감산이 아니다(반복 토글 오차 방지). ---


def _wire_item_toggle(monkeypatch, *, run=None, claim=None, receipt=None):
    """update_receipt_items 호출을 receipt에 다시 반영해 반복 토글(같은 요청 안에서
    두 번 부르는 테스트)에서도 실제 Firestore처럼 이전 write가 다음 read에 보이게 한다."""
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: run if run is not None else _run_doc())
    monkeypatch.setattr(routes, "get_claim", lambda claim_id: claim)
    monkeypatch.setattr(routes, "get_receipts", lambda receipt_ids: {"rct_1": receipt} if receipt else {})
    update_receipt_calls = []
    update_claim_calls = []

    def fake_update_receipt_items(receipt_id, items):
        update_receipt_calls.append((receipt_id, items))
        if receipt is not None:
            receipt["items"] = items

    monkeypatch.setattr(routes, "update_receipt_items", fake_update_receipt_items)
    monkeypatch.setattr(
        routes, "update_claim", lambda claim_id, updates: update_claim_calls.append((claim_id, updates))
    )
    return update_receipt_calls, update_claim_calls


def _linked_claim(**overrides):
    claim = {
        "claim_id": "clm_1",
        "org_id": "org_1",
        "recipient_id": "rcp_1",
        "receipt_id": "rct_1",
        "settlement_run_id": "run_1",
        "amount_minor": 10000,
        "currency": "KRW",
    }
    claim.update(overrides)
    return claim


def test_exclude_item_subtracts_its_price_from_claim_amount(monkeypatch):
    receipt = {
        "parsed_amount_minor": 10000,
        "items": [{"name": "아메리카노", "amount_minor": 4500}, {"name": "케이크", "amount_minor": 5500}],
    }
    update_receipt_calls, update_claim_calls = _wire_item_toggle(
        monkeypatch, claim=_linked_claim(), receipt=receipt
    )

    result = routes.set_claim_item_excluded_route(
        "run_1", "clm_1", 0, {"excluded": True}, authorization="Bearer t"
    )

    assert result["amount_minor"] == 5500
    assert update_claim_calls == [("clm_1", {"amount_minor": 5500, "updated_at": update_claim_calls[0][1]["updated_at"]})]
    assert update_receipt_calls[0][1][0]["excluded"] is True


def test_reincluding_item_restores_the_full_amount(monkeypatch):
    """누적 감산이 아니라 매번 parsed_amount_minor 기준으로 다시 계산한다 —
    제외했다 되돌리면 원래 금액으로 정확히 복원돼야 한다."""
    receipt = {
        "parsed_amount_minor": 10000,
        "items": [{"name": "아메리카노", "amount_minor": 4500, "excluded": True}, {"name": "케이크", "amount_minor": 5500}],
    }
    _, update_claim_calls = _wire_item_toggle(monkeypatch, claim=_linked_claim(amount_minor=5500), receipt=receipt)

    result = routes.set_claim_item_excluded_route(
        "run_1", "clm_1", 0, {"excluded": False}, authorization="Bearer t"
    )

    assert result["amount_minor"] == 10000
    assert update_claim_calls[0][1]["amount_minor"] == 10000


def test_excluding_all_items_floors_amount_at_zero_not_negative(monkeypatch):
    """항목별 amount_minor 합이 parsed_amount_minor보다 클 수 있다(OCR 오독 등) —
    음수로 내려가지 않는다."""
    receipt = {
        "parsed_amount_minor": 10000,
        "items": [{"name": "a", "amount_minor": 6000}, {"name": "b", "amount_minor": 6000}],
    }
    _, update_claim_calls = _wire_item_toggle(monkeypatch, claim=_linked_claim(), receipt=receipt)

    routes.set_claim_item_excluded_route("run_1", "clm_1", 0, {"excluded": True}, authorization="Bearer t")
    result = routes.set_claim_item_excluded_route("run_1", "clm_1", 1, {"excluded": True}, authorization="Bearer t")

    assert result["amount_minor"] == 0


def test_item_toggle_rejected_when_run_not_draft(monkeypatch):
    _wire_item_toggle(monkeypatch, run=_run_doc(status="APPROVED"), claim=_linked_claim(), receipt={})

    with pytest.raises(HTTPException) as exc_info:
        routes.set_claim_item_excluded_route("run_1", "clm_1", 0, {"excluded": True}, authorization="Bearer t")

    assert exc_info.value.status_code == 409


def test_item_toggle_rejected_for_other_orgs_run(monkeypatch):
    _wire_item_toggle(monkeypatch, run=_run_doc(org_id="org_2"), claim=_linked_claim(), receipt={})

    with pytest.raises(HTTPException) as exc_info:
        routes.set_claim_item_excluded_route("run_1", "clm_1", 0, {"excluded": True}, authorization="Bearer t")

    assert exc_info.value.status_code == 404


def test_item_toggle_rejected_when_claim_not_linked_to_run(monkeypatch):
    _wire_item_toggle(
        monkeypatch, claim=_linked_claim(settlement_run_id="run_other"), receipt={"items": [{"name": "a", "amount_minor": 100}]}
    )

    with pytest.raises(HTTPException) as exc_info:
        routes.set_claim_item_excluded_route("run_1", "clm_1", 0, {"excluded": True}, authorization="Bearer t")

    assert exc_info.value.status_code == 404


def test_item_toggle_rejected_for_unknown_item_index(monkeypatch):
    receipt = {"parsed_amount_minor": 10000, "items": [{"name": "a", "amount_minor": 4500}]}
    _wire_item_toggle(monkeypatch, claim=_linked_claim(), receipt=receipt)

    with pytest.raises(HTTPException) as exc_info:
        routes.set_claim_item_excluded_route("run_1", "clm_1", 5, {"excluded": True}, authorization="Bearer t")

    assert exc_info.value.status_code == 404


def test_item_toggle_rejected_when_excluded_field_not_boolean(monkeypatch):
    receipt = {"parsed_amount_minor": 10000, "items": [{"name": "a", "amount_minor": 4500}]}
    _wire_item_toggle(monkeypatch, claim=_linked_claim(), receipt=receipt)

    with pytest.raises(HTTPException) as exc_info:
        routes.set_claim_item_excluded_route("run_1", "clm_1", 0, {"excluded": "yes"}, authorization="Bearer t")

    assert exc_info.value.status_code == 400


# --- POST /settlements/runs/{run_id}/claims/{claim_id}/exclude — claim 통째
# 제외(재청구 의심 등). item 반려와 달리 amount_minor를 안 건드리고 claim을
# IN_RUN → CONFIRMED로 되돌린다. ---


def _wire_claim_exclude(monkeypatch, *, run=None, claim=None, unlinked=True):
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: run if run is not None else _run_doc())
    monkeypatch.setattr(routes, "get_claim", lambda claim_id: claim)
    monkeypatch.setattr(routes, "unlink_claim_from_run_cas", lambda run_id, claim_id: unlinked)
    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))
    return audit_calls


def test_exclude_claim_unlinks_it_and_records_audit_log(monkeypatch):
    audit_calls = _wire_claim_exclude(monkeypatch, claim=_linked_claim())

    result = routes.exclude_claim_from_run_route(
        "run_1", "clm_1", {"reason": "재청구 의심"}, authorization="Bearer t"
    )

    assert result == {"claim_id": "clm_1", "excluded": True}
    assert audit_calls[0]["action"] == "CLAIM_EXCLUDED_FROM_RUN"
    assert audit_calls[0]["reason"] == "재청구 의심"
    assert audit_calls[0]["after"] == {"claim_id": "clm_1"}


def test_exclude_claim_works_without_a_reason(monkeypatch):
    _wire_claim_exclude(monkeypatch, claim=_linked_claim())

    result = routes.exclude_claim_from_run_route("run_1", "clm_1", None, authorization="Bearer t")

    assert result["excluded"] is True


def test_exclude_claim_rejected_when_run_not_draft(monkeypatch):
    _wire_claim_exclude(monkeypatch, run=_run_doc(status="APPROVED"), claim=_linked_claim())

    with pytest.raises(HTTPException) as exc_info:
        routes.exclude_claim_from_run_route("run_1", "clm_1", None, authorization="Bearer t")

    assert exc_info.value.status_code == 409


def test_exclude_claim_rejected_for_other_orgs_run(monkeypatch):
    _wire_claim_exclude(monkeypatch, run=_run_doc(org_id="org_2"), claim=_linked_claim())

    with pytest.raises(HTTPException) as exc_info:
        routes.exclude_claim_from_run_route("run_1", "clm_1", None, authorization="Bearer t")

    assert exc_info.value.status_code == 404


def test_exclude_claim_rejected_when_claim_not_linked_to_run(monkeypatch):
    _wire_claim_exclude(monkeypatch, claim=_linked_claim(settlement_run_id="run_other"))

    with pytest.raises(HTTPException) as exc_info:
        routes.exclude_claim_from_run_route("run_1", "clm_1", None, authorization="Bearer t")

    assert exc_info.value.status_code == 404


def test_exclude_claim_rejected_when_unknown_claim(monkeypatch):
    _wire_claim_exclude(monkeypatch, claim=None)

    with pytest.raises(HTTPException) as exc_info:
        routes.exclude_claim_from_run_route("run_1", "clm_1", None, authorization="Bearer t")

    assert exc_info.value.status_code == 404


def test_exclude_claim_conflict_when_cas_unlink_fails(monkeypatch):
    """claim이 이미 다른 상태로 바뀌었으면(예: 동시에 SETTLED) CAS가 조용히
    실패하고, 라우트는 이걸 409로 알린다 — 감사 로그도 안 남긴다."""
    audit_calls = _wire_claim_exclude(monkeypatch, claim=_linked_claim(), unlinked=False)

    with pytest.raises(HTTPException) as exc_info:
        routes.exclude_claim_from_run_route("run_1", "clm_1", None, authorization="Bearer t")

    assert exc_info.value.status_code == 409
    assert audit_calls == []


# --- POST /agents/executor/reject-items — 청구 반려 자동화(집행자 에이전트가
# 개인적 사용 의심 물품을 골라 정산 금액에서 제외한다). _apply_item_exclusion을
# PATCH .../items/{i}(사람)와 공유한다 — 여기서는 OIDC로 인증하고 배치로 받는다. ---


def _wire_reject_items(monkeypatch, *, run=None, claim=None, receipt=None):
    update_receipt_calls, update_claim_calls = _wire_item_toggle(
        monkeypatch, run=run, claim=claim, receipt=receipt
    )
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})
    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))
    return update_receipt_calls, update_claim_calls, audit_calls


def test_reject_items_excludes_and_records_reason_and_audit_log(monkeypatch):
    receipt = {
        "parsed_amount_minor": 10000,
        "items": [{"name": "아메리카노", "amount_minor": 4500}, {"name": "케이크", "amount_minor": 5500}],
    }
    _, update_claim_calls, audit_calls = _wire_reject_items(
        monkeypatch, claim=_linked_claim(), receipt=receipt
    )

    result = routes.reject_claim_items_route(
        {
            "settlement_run_id": "run_1",
            "rejections": [{"claim_id": "clm_1", "item_index": 0, "reason": "개인 음료로 추정"}],
        },
        authorization="Bearer t",
    )

    assert result == {
        "results": [{"claim_id": "clm_1", "item_index": 0, "status": "ok", "amount_minor": 5500}]
    }
    assert update_claim_calls == [("clm_1", {"amount_minor": 5500, "updated_at": update_claim_calls[0][1]["updated_at"]})]
    assert receipt["items"][0]["excluded"] is True
    assert receipt["items"][0]["rejected_reason"] == "개인 음료로 추정"
    assert receipt["items"][0]["rejected_by"] == "EXECUTOR"
    assert audit_calls == [
        {
            "org_id": "org_1",
            "actor": "agent/executor",
            "actor_type": "AGENT",
            "action": "CLAIM_ITEM_REJECTED",
            "run_id": "run_1",
            "reason": "개인 음료로 추정",
            "after": {"claim_id": "clm_1", "item_index": 0, "amount_minor": 5500},
        }
    ]


def test_reject_items_partial_failure_does_not_block_the_rest_of_the_batch(monkeypatch):
    receipt = {
        "parsed_amount_minor": 10000,
        "items": [{"name": "아메리카노", "amount_minor": 4500}],
    }
    _, update_claim_calls, audit_calls = _wire_reject_items(
        monkeypatch, claim=_linked_claim(), receipt=receipt
    )

    result = routes.reject_claim_items_route(
        {
            "settlement_run_id": "run_1",
            "rejections": [
                {"claim_id": "clm_1", "item_index": 99, "reason": "존재하지 않는 인덱스"},
                {"claim_id": "clm_1", "item_index": 0, "reason": "개인 음료로 추정"},
            ],
        },
        authorization="Bearer t",
    )

    assert result["results"][0]["status"] == "error"
    assert result["results"][1] == {
        "claim_id": "clm_1",
        "item_index": 0,
        "status": "ok",
        "amount_minor": 5500,
    }
    assert len(update_claim_calls) == 1  # 실패한 첫 항목은 claim을 건드리지 않는다
    assert len(audit_calls) == 1  # 성공한 것만 감사 로그에 남는다


def test_reject_items_missing_claim_id_or_reason_reported_as_error(monkeypatch):
    receipt = {"parsed_amount_minor": 10000, "items": [{"name": "a", "amount_minor": 4500}]}
    _wire_reject_items(monkeypatch, claim=_linked_claim(), receipt=receipt)

    result = routes.reject_claim_items_route(
        {
            "settlement_run_id": "run_1",
            "rejections": [{"claim_id": "clm_1", "item_index": 0, "reason": ""}],
        },
        authorization="Bearer t",
    )

    assert result["results"] == [
        {
            "claim_id": "clm_1",
            "item_index": 0,
            "status": "error",
            "detail": "claim_id, item_index, reason required",
        }
    ]


def test_reject_items_rejected_when_run_not_draft(monkeypatch):
    _wire_reject_items(monkeypatch, run=_run_doc(status="APPROVED"))

    with pytest.raises(HTTPException) as exc_info:
        routes.reject_claim_items_route(
            {
                "settlement_run_id": "run_1",
                "rejections": [{"claim_id": "clm_1", "item_index": 0, "reason": "x"}],
            },
            authorization="Bearer t",
        )

    assert exc_info.value.status_code == 409


def test_reject_items_requires_settlement_run_id_and_rejections(monkeypatch):
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})

    with pytest.raises(HTTPException) as exc_info:
        routes.reject_claim_items_route({}, authorization="Bearer t")

    assert exc_info.value.status_code == 400


def test_reject_items_unknown_run_is_404(monkeypatch):
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: None)

    with pytest.raises(HTTPException) as exc_info:
        routes.reject_claim_items_route(
            {
                "settlement_run_id": "run_missing",
                "rejections": [{"claim_id": "clm_1", "item_index": 0, "reason": "x"}],
            },
            authorization="Bearer t",
        )

    assert exc_info.value.status_code == 404


def test_reject_items_calls_verify_oidc_not_session(monkeypatch):
    """web PATCH 라우트와 달리 세션이 아니라 OIDC로 인증한다 — agent 서비스 계정이
    직접 부른다(agent_invoker_api)."""
    receipt = {"parsed_amount_minor": 10000, "items": [{"name": "a", "amount_minor": 4500}]}
    _wire_item_toggle(monkeypatch, claim=_linked_claim(), receipt=receipt)
    oidc_calls = []
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: oidc_calls.append(auth) or {})
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)

    routes.reject_claim_items_route(
        {
            "settlement_run_id": "run_1",
            "rejections": [{"claim_id": "clm_1", "item_index": 0, "reason": "x"}],
        },
        authorization="Bearer agent-oidc-token",
    )

    assert oidc_calls == ["Bearer agent-oidc-token"]


# --- PATCH /settlements/runs/{run}/claims/{claim} — 청구 전체 반려(사람). POST
# /agents/executor/reject-claims — 같은 것을 집행자 에이전트가 자동으로(중복
# 청구·동일 영수증 재제출·미래 거래일). 둘 다 _apply_claim_exclusion을 공유한다. ---


def _wire_claim_toggle(monkeypatch, *, run=None, claim=None):
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: run if run is not None else _run_doc())
    monkeypatch.setattr(routes, "get_claim", lambda claim_id: claim)
    update_claim_calls = []
    monkeypatch.setattr(
        routes, "update_claim", lambda claim_id, updates: update_claim_calls.append((claim_id, updates))
    )
    return update_claim_calls


def test_exclude_claim_sets_excluded_flag_without_touching_amount(monkeypatch):
    update_claim_calls = _wire_claim_toggle(monkeypatch, claim=_linked_claim())

    result = routes.set_claim_excluded_route("run_1", "clm_1", {"excluded": True}, authorization="Bearer t")

    assert result == {"claim_id": "clm_1", "excluded": True}
    assert len(update_claim_calls) == 1
    claim_id, updates = update_claim_calls[0]
    assert claim_id == "clm_1"
    assert updates["excluded"] is True
    assert "amount_minor" not in updates  # claim.amount_minor는 원래 값 그대로 보존된다


def test_reincluding_claim_clears_excluded_flag(monkeypatch):
    update_claim_calls = _wire_claim_toggle(
        monkeypatch, claim=_linked_claim(excluded=True, rejected_reason="x", rejected_by="EXECUTOR")
    )

    routes.set_claim_excluded_route("run_1", "clm_1", {"excluded": False}, authorization="Bearer t")

    _, updates = update_claim_calls[0]
    assert updates["excluded"] is False
    assert updates["rejected_reason"] is None
    assert updates["rejected_by"] is None


def test_claim_toggle_rejected_when_run_not_draft(monkeypatch):
    _wire_claim_toggle(monkeypatch, run=_run_doc(status="APPROVED"), claim=_linked_claim())

    with pytest.raises(HTTPException) as exc_info:
        routes.set_claim_excluded_route("run_1", "clm_1", {"excluded": True}, authorization="Bearer t")

    assert exc_info.value.status_code == 409


def test_claim_toggle_rejected_for_other_orgs_run(monkeypatch):
    _wire_claim_toggle(monkeypatch, run=_run_doc(org_id="org_2"), claim=_linked_claim())

    with pytest.raises(HTTPException) as exc_info:
        routes.set_claim_excluded_route("run_1", "clm_1", {"excluded": True}, authorization="Bearer t")

    assert exc_info.value.status_code == 404


def test_claim_toggle_rejected_when_claim_not_linked_to_run(monkeypatch):
    _wire_claim_toggle(monkeypatch, claim=_linked_claim(settlement_run_id="run_other"))

    with pytest.raises(HTTPException) as exc_info:
        routes.set_claim_excluded_route("run_1", "clm_1", {"excluded": True}, authorization="Bearer t")

    assert exc_info.value.status_code == 404


def test_claim_toggle_rejected_when_excluded_field_not_boolean(monkeypatch):
    _wire_claim_toggle(monkeypatch, claim=_linked_claim())

    with pytest.raises(HTTPException) as exc_info:
        routes.set_claim_excluded_route("run_1", "clm_1", {"excluded": "yes"}, authorization="Bearer t")

    assert exc_info.value.status_code == 400


def _wire_reject_claims(monkeypatch, *, run=None, claim=None):
    update_claim_calls = _wire_claim_toggle(monkeypatch, run=run, claim=claim)
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})
    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))
    return update_claim_calls, audit_calls


def test_reject_claims_excludes_and_records_reason_and_audit_log(monkeypatch):
    update_claim_calls, audit_calls = _wire_reject_claims(monkeypatch, claim=_linked_claim())

    result = routes.reject_claims_route(
        {
            "settlement_run_id": "run_1",
            "rejections": [{"claim_id": "clm_1", "reason": "동일 영수증 재제출 의심"}],
        },
        authorization="Bearer agent-oidc-token",
    )

    assert result == {"results": [{"claim_id": "clm_1", "status": "ok", "excluded": True}]}
    _, updates = update_claim_calls[0]
    assert updates["excluded"] is True
    assert updates["rejected_reason"] == "동일 영수증 재제출 의심"
    assert updates["rejected_by"] == "EXECUTOR"
    assert audit_calls == [
        {
            "org_id": "org_1",
            "actor": "agent/executor",
            "actor_type": "AGENT",
            "action": "CLAIM_REJECTED",
            "run_id": "run_1",
            "reason": "동일 영수증 재제출 의심",
            "after": {"claim_id": "clm_1"},
        }
    ]


def test_reject_claims_partial_failure_does_not_block_the_rest_of_the_batch(monkeypatch):
    """claim_id 하나가 잘못됐어도(존재하지 않음 등) 나머지 배치는 계속 처리한다 —
    reject_claim_items_route와 같은 부분 실패 허용 정책."""
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: _run_doc())
    claims_by_id = {"clm_1": _linked_claim()}
    monkeypatch.setattr(routes, "get_claim", lambda claim_id: claims_by_id.get(claim_id))
    update_claim_calls = []
    monkeypatch.setattr(
        routes, "update_claim", lambda claim_id, updates: update_claim_calls.append((claim_id, updates))
    )
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)

    result = routes.reject_claims_route(
        {
            "settlement_run_id": "run_1",
            "rejections": [
                {"claim_id": "clm_1", "reason": "x"},
                {"claim_id": "clm_missing", "reason": "y"},
            ],
        },
        authorization="Bearer agent-oidc-token",
    )

    assert result["results"][0] == {"claim_id": "clm_1", "status": "ok", "excluded": True}
    assert result["results"][1]["status"] == "error"
    assert len(update_claim_calls) == 1  # clm_missing은 update_claim이 아예 안 불린다


def test_reject_claims_missing_claim_id_or_reason_reported_as_error(monkeypatch):
    update_claim_calls, _ = _wire_reject_claims(monkeypatch, claim=_linked_claim())

    result = routes.reject_claims_route(
        {"settlement_run_id": "run_1", "rejections": [{"claim_id": "clm_1"}]},
        authorization="Bearer agent-oidc-token",
    )

    assert result["results"] == [
        {"claim_id": "clm_1", "status": "error", "detail": "claim_id, reason required"}
    ]
    assert update_claim_calls == []


def test_reject_claims_rejected_when_run_not_draft(monkeypatch):
    _wire_reject_claims(monkeypatch, run=_run_doc(status="APPROVED"))

    with pytest.raises(HTTPException) as exc_info:
        routes.reject_claims_route(
            {"settlement_run_id": "run_1", "rejections": [{"claim_id": "clm_1", "reason": "x"}]},
            authorization="Bearer agent-oidc-token",
        )

    assert exc_info.value.status_code == 409


def test_reject_claims_requires_settlement_run_id_and_rejections(monkeypatch):
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})

    with pytest.raises(HTTPException) as exc_info:
        routes.reject_claims_route({"settlement_run_id": "run_1"}, authorization="Bearer t")

    assert exc_info.value.status_code == 400


def test_reject_claims_unknown_run_is_404(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: None)
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})

    with pytest.raises(HTTPException) as exc_info:
        routes.reject_claims_route(
            {
                "settlement_run_id": "run_missing",
                "rejections": [{"claim_id": "clm_1", "reason": "x"}],
            },
            authorization="Bearer t",
        )

    assert exc_info.value.status_code == 404


def _wire_retry(monkeypatch, *, run=None, claims=None, receipts=None, enqueue_error=None):
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: run if run is not None else _run_doc())
    monkeypatch.setattr(routes, "get_claims_for_run", lambda run_id: claims if claims is not None else [_claim("clm_1")])
    monkeypatch.setattr(routes, "get_receipts", lambda receipt_ids: receipts or {})
    monkeypatch.setattr(routes, "list_settled_claims", lambda org_id: [])
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: None)

    enqueue_calls = []

    def fake_enqueue(
        run_id,
        claim_summaries,
        duplicate_groups,
        exact_duplicate_groups,
        future_dated_claims,
        org_id,
        force_reanalyze=False,
    ):
        enqueue_calls.append(
            (
                run_id,
                claim_summaries,
                duplicate_groups,
                exact_duplicate_groups,
                future_dated_claims,
                org_id,
                force_reanalyze,
            )
        )
        if enqueue_error:
            raise enqueue_error

    monkeypatch.setattr(routes, "enqueue_executor_analyze", fake_enqueue)

    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))

    status_calls = []
    monkeypatch.setattr(
        routes,
        "set_executor_analysis_status",
        lambda run_id, status, reason=None: status_calls.append((run_id, status, reason)),
    )

    return enqueue_calls, audit_calls, status_calls


def test_retry_executor_analysis_reenqueues_linked_claims(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    enqueue_calls, audit_calls, status_calls = _wire_retry(monkeypatch, claims=claims, receipts=receipts)

    result = routes.retry_executor_analysis_route("run_1", authorization="Bearer t")

    assert len(enqueue_calls) == 1
    run_id, claim_summaries, _, _, _, org_id, force_reanalyze = enqueue_calls[0]
    assert run_id == "run_1"
    assert org_id == "org_1"
    assert claim_summaries[0]["claim_id"] == "clm_1"
    assert force_reanalyze is True  # 재시도는 이미 CLOSED된 세션도 강제로 재분석해야 한다
    assert status_calls == [("run_1", "PROCESSING", None)]
    assert any(c["action"] == "EXECUTOR_ANALYSIS_RETRY_REQUESTED" for c in audit_calls)
    assert result["executor_analysis"] is None  # get_agent_draft가 None을 돌려주므로


def test_retry_executor_analysis_rejected_when_run_not_draft(monkeypatch):
    enqueue_calls, _, _ = _wire_retry(monkeypatch, run=_run_doc(status="EXECUTING"))

    with pytest.raises(HTTPException) as exc_info:
        routes.retry_executor_analysis_route("run_1", authorization="Bearer t")

    assert exc_info.value.status_code == 409
    assert enqueue_calls == []


def test_retry_executor_analysis_rejected_for_other_orgs_run(monkeypatch):
    enqueue_calls, _, _ = _wire_retry(monkeypatch, run=_run_doc(org_id="org_other"))

    with pytest.raises(HTTPException) as exc_info:
        routes.retry_executor_analysis_route("run_1", authorization="Bearer t")

    assert exc_info.value.status_code == 404
    assert enqueue_calls == []


def test_retry_executor_analysis_404_when_no_claims_linked(monkeypatch):
    enqueue_calls, _, _ = _wire_retry(monkeypatch, claims=[])

    with pytest.raises(HTTPException) as exc_info:
        routes.retry_executor_analysis_route("run_1", authorization="Bearer t")

    assert exc_info.value.status_code == 404
    assert enqueue_calls == []


def test_retry_executor_analysis_enqueue_failure_sets_failed_status(monkeypatch):
    enqueue_calls, _, status_calls = _wire_retry(monkeypatch, enqueue_error=RuntimeError("boom"))

    routes.retry_executor_analysis_route("run_1", authorization="Bearer t")

    assert len(enqueue_calls) == 1
    assert status_calls == [("run_1", "FAILED", "boom")]
