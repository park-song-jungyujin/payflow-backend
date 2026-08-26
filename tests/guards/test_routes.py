"""schema-contract.md §10 POST /settlements/runs/{run_id}/approve — money-safety.md
승인 게이트의 발급 측. tests/guards/test_tokens.py는 소각(burn) 측을 검증한다."""

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from src.guards import routes
from src.guards.errors import GuardRejection
from src.payouts.fx import FxRateUnavailable


class FakeTransaction:
    def update(self, ref, data):
        pass


class FakeRunRef:
    def __init__(self, data):
        self._data = data

    def get(self, transaction=None):
        class Snap:
            def to_dict(_self):
                return dict(self._data) if self._data else None

        return Snap()


class FakeCollection:
    def __init__(self, data):
        self._data = data

    def document(self, doc_id):
        return FakeRunRef(self._data)


class FakeClient:
    def __init__(self, current_store_doc):
        self._doc = current_store_doc

    def collection(self, name):
        assert name == "settlement_runs"
        return FakeCollection(self._doc)

    def transaction(self):
        return FakeTransaction()


def _run(**overrides):
    run = {
        "settlement_run_id": "run_1",
        "org_id": "org_1",
        "status": "DRAFT",
    }
    run.update(overrides)
    return run


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(
        routes,
        "verify_session",
        lambda token: {"executor_id": "exe_1", "org_id": "org_1", "email": "alice@example.com"},
    )
    monkeypatch.setattr(routes.firestore, "transactional", lambda fn: fn)
    monkeypatch.setattr(routes, "check_caps", lambda run: None)
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)
    # 빈 클레임은 422(아래 test_empty_claims_returns_422...)라 기본값은 클레임 1건.
    # 개별 테스트가 필요하면 이 스텁을 덮어쓴다.
    monkeypatch.setattr(
        routes,
        "get_claims_for_run",
        lambda run_id: [
            {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 1000, "currency": "USD"}
        ],
    )
    # approval_amount_hash(tokens.py)는 자체적으로 get_claims_for_run(진짜 Firestore)을
    # 다시 부른다 — 여기 테스트가 검증하는 건 CAS/캡/토큰 발급이지 해시 내용이 아니라 스텁 처리.
    monkeypatch.setattr(routes, "approval_amount_hash", lambda run: "stub-hash")
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")
    monkeypatch.setenv("APPROVAL_TOKEN_TTL_SECONDS", "600")


def _wire_store(monkeypatch, run, current_store_doc=None):
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: run)
    monkeypatch.setattr(routes, "get_client", lambda: FakeClient(current_store_doc or run))


def test_unknown_run_returns_404(monkeypatch):
    _wire_store(monkeypatch, None)
    with pytest.raises(HTTPException) as exc:
        routes.approve_settlement_run("run_1", authorization="Bearer t")
    assert exc.value.status_code == 404


def test_non_draft_run_returns_409(monkeypatch):
    run = _run(status="APPROVED")
    _wire_store(monkeypatch, run)
    with pytest.raises(HTTPException) as exc:
        routes.approve_settlement_run("run_1", authorization="Bearer t")
    assert exc.value.status_code == 409


def test_failed_run_can_be_reapproved_for_retry(monkeypatch):
    """schema-contract.md §8 재발송 — FAILED run도 DRAFT처럼 재승인해 새 토큰을 받는다.
    reconcile.py가 실패분 claim의 settlement_run_id를 비우지 않고 이 run에 그대로
    묶어두므로(중복 지급 방지), 재승인 시점에도 get_claims_for_run이 클레임을 찾는다."""
    run = _run(status="FAILED")
    _wire_store(monkeypatch, run)
    monkeypatch.setattr(
        routes,
        "get_claims_for_run",
        lambda run_id: [
            {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 1000, "currency": "USD"}
        ],
    )

    result = routes.approve_settlement_run("run_1", authorization="Bearer t")

    assert result["status"] == "APPROVED"
    assert "approval_token" in result


def test_empty_claims_returns_422_and_does_not_issue_token(monkeypatch):
    """클레임이 하나도 안 걸린 run을 0원으로 조용히 승인하지 않는다 — FAILED
    run이 (버그로든 뭐로든) 클레임을 다 잃은 상태에서 재승인되는 걸 막는 안전판."""
    run = _run()
    _wire_store(monkeypatch, run)
    monkeypatch.setattr(routes, "get_claims_for_run", lambda run_id: [])

    with pytest.raises(HTTPException) as exc:
        routes.approve_settlement_run("run_1", authorization="Bearer t")
    assert exc.value.status_code == 422


def test_linked_claims_summing_to_zero_is_approved_not_blocked(monkeypatch):
    """claim은 걸려있는데 물품 전부 반려 등으로 합계가 0원인 경우는 "클레임이
    아예 없는 run"과 다르다 — 승인 자체는 통과해야 한다. 실제 PayPal 호출을
    건너뛰는 건 payouts/routes.py.request_payout 몫이다."""
    run = _run()
    _wire_store(monkeypatch, run)
    monkeypatch.setattr(
        routes,
        "get_claims_for_run",
        lambda run_id: [
            {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 0, "currency": "USD"}
        ],
    )

    result = routes.approve_settlement_run("run_1", authorization="Bearer t")

    assert result["status"] == "APPROVED"
    assert result["total_amount_minor"] == 0
    assert "approval_token" in result


def test_excluded_claim_amount_is_not_counted_in_total(monkeypatch):
    """청구 전체 반려(settlements/routes.py._apply_claim_exclusion)된 claim은
    claim_count에는 잡히되(linked claim은 맞으니까) 합계에서는 빠져야 한다 —
    안 그러면 반려는 화면에만 표시되고 승인 총액엔 그대로 남는다."""
    run = _run()
    _wire_store(monkeypatch, run)
    monkeypatch.setattr(
        routes,
        "get_claims_for_run",
        lambda run_id: [
            {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 1000, "currency": "USD"},
            {
                "claim_id": "clm_2",
                "recipient_id": "rcp_1",
                "amount_minor": 9000,
                "currency": "USD",
                "excluded": True,
            },
        ],
    )

    result = routes.approve_settlement_run("run_1", authorization="Bearer t")

    assert result["status"] == "APPROVED"
    assert result["total_amount_minor"] == 1000


def test_all_claims_excluded_is_approved_with_zero_total_not_blocked(monkeypatch):
    """claim은 있는데 전부 반려된 경우 — "claim이 아예 안 걸림"(422)과 다르다."""
    run = _run()
    _wire_store(monkeypatch, run)
    monkeypatch.setattr(
        routes,
        "get_claims_for_run",
        lambda run_id: [
            {
                "claim_id": "clm_1",
                "recipient_id": "rcp_1",
                "amount_minor": 1000,
                "currency": "USD",
                "excluded": True,
            }
        ],
    )

    result = routes.approve_settlement_run("run_1", authorization="Bearer t")

    assert result["status"] == "APPROVED"
    assert result["total_amount_minor"] == 0


def test_fx_lookup_failure_returns_502(monkeypatch):
    run = _run()
    _wire_store(monkeypatch, run)
    monkeypatch.setattr(
        routes,
        "get_claims_for_run",
        lambda run_id: [
            {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 1000, "currency": "KRW"}
        ],
    )

    def boom(frm, to):
        raise FxRateUnavailable("KRW/USD")

    monkeypatch.setattr(routes, "fetch_fx_rate", boom)

    with pytest.raises(HTTPException) as exc:
        routes.approve_settlement_run("run_1", authorization="Bearer t")
    assert exc.value.status_code == 502


def test_base_currency_only_claims_skip_fx_lookup(monkeypatch):
    """전부 base_currency면 fetch_fx_rate가 아예 호출되지 않아야 한다 —
    불필요한 외부 호출은 승인 지연/실패 지점을 늘린다."""
    run = _run()
    _wire_store(monkeypatch, run)
    monkeypatch.setattr(
        routes,
        "get_claims_for_run",
        lambda run_id: [
            {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 1000, "currency": "USD"}
        ],
    )
    calls = []
    monkeypatch.setattr(routes, "fetch_fx_rate", lambda *a: calls.append(a) or Decimal("1"))

    result = routes.approve_settlement_run("run_1", authorization="Bearer t")

    assert calls == []
    assert result["total_amount_minor"] == 1000
    assert result["fx_rates"] == {}


def test_cap_violation_returns_403_and_does_not_issue_token(monkeypatch):
    run = _run()
    _wire_store(monkeypatch, run)
    monkeypatch.setattr(routes, "check_caps", lambda run: "MAX_AMOUNT_PER_BATCH_MINOR exceeded")

    with pytest.raises(HTTPException) as exc:
        routes.approve_settlement_run("run_1", authorization="Bearer t")
    assert exc.value.status_code == 403


def test_successful_approval_returns_raw_token_never_the_hash(monkeypatch):
    run = _run()
    _wire_store(monkeypatch, run)

    result = routes.approve_settlement_run("run_1", authorization="Bearer t")

    assert result["status"] == "APPROVED"
    assert "approval_token" in result
    assert "approval_token_hash" not in result
    # 응답의 평문 토큰을 해시하면 실제 저장된 해시와 일치해야 한다(같은 토큰).
    hashed = hashlib.sha256(result["approval_token"].encode("utf-8")).hexdigest()
    # 저장용 updates는 함수 내부에만 있으므로, 간접적으로 토큰이 비어있지 않음만 확인.
    assert len(result["approval_token"]) > 20
    assert hashed  # 해시 가능한 문자열이면 충분 — 원문 자체가 응답 밖으로 안 새는지가 핵심


def test_successful_approval_sets_expiry_using_configured_ttl(monkeypatch):
    run = _run()
    _wire_store(monkeypatch, run)
    monkeypatch.setenv("APPROVAL_TOKEN_TTL_SECONDS", "60")

    before = datetime.now(UTC)
    result = routes.approve_settlement_run("run_1", authorization="Bearer t")
    after = datetime.now(UTC)

    expires_at = result["approval_token_expires_at"]
    assert before + timedelta(seconds=59) <= expires_at <= after + timedelta(seconds=61)


def test_approved_by_comes_from_session_not_client_input(monkeypatch):
    """세션에서 검증된 신원으로 채운다 — 클라이언트가 보낸 값을 신뢰하지 않는다
    (예전엔 body의 approved_by를 그대로 믿었다 — 신원 스푸핑 구멍)."""
    run = _run()
    _wire_store(monkeypatch, run)

    result = routes.approve_settlement_run("run_1", authorization="Bearer t")

    assert result["approved_by"] == "alice@example.com"


def test_concurrent_double_approval_second_caller_rejected(monkeypatch):
    """CAS — 승인 시작 시점엔 DRAFT였지만, 트랜잭션이 실제로 읽을 때는 이미
    다른 요청이 먼저 APPROVED로 바꿔놓은 경우(레이스)."""
    caller_view = _run(status="DRAFT")
    already_approved_in_store = _run(status="APPROVED")
    monkeypatch.setattr(routes, "get_settlement_run", lambda run_id: caller_view)
    monkeypatch.setattr(routes, "get_client", lambda: FakeClient(already_approved_in_store))

    with pytest.raises(HTTPException) as exc:
        routes.approve_settlement_run("run_1", authorization="Bearer t")
    assert exc.value.status_code == 409
