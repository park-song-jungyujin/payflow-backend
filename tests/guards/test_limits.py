"""money-safety.md 한도 — 항목/배치/월간 캡 3종. 환경변수로 캡을 켜고 끄는 동작과
FX 환산 경유 초과 판정을 검증한다."""

import pytest

from src.guards import limits


@pytest.fixture
def claims(monkeypatch):
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")  # check_caps는 캡 무관하게 항상 읽는다
    data = {"claims": [], "recipients": {}}
    monkeypatch.setattr(limits, "get_claims_for_run", lambda run_id: data["claims"])
    monkeypatch.setattr(limits, "get_recipient", lambda rid: data["recipients"].get(rid))
    return data


def _run(**overrides):
    run = {"settlement_run_id": "run_1", "total_amount_minor": 1000, "fx_rates": {}}
    run.update(overrides)
    return run


def test_no_caps_set_never_rejects(claims, monkeypatch):
    monkeypatch.delenv("MAX_AMOUNT_PER_BATCH_MINOR", raising=False)
    monkeypatch.delenv("MAX_AMOUNT_PER_ITEM_MINOR", raising=False)
    monkeypatch.delenv("MAX_AMOUNT_MONTHLY_MINOR", raising=False)
    assert limits.check_caps(_run(total_amount_minor=999_999_999)) is None


def test_batch_cap_exceeded(claims, monkeypatch):
    monkeypatch.setenv("MAX_AMOUNT_PER_BATCH_MINOR", "500")
    violation = limits.check_caps(_run(total_amount_minor=501))
    assert violation is not None
    assert "MAX_AMOUNT_PER_BATCH_MINOR" in violation


def test_batch_cap_set_to_zero_rejects_any_amount(claims, monkeypatch):
    """`_cap`은 `os.environ.get(name, "")`으로 미설정을 빈 문자열로 본다 — "0"은
    실제 값이라 int("0")=0이 반환된다. 0을 falsy로 오판해 캡 미설정과 같이 취급하면
    캡을 0으로 걸어 잠근 설정이 조용히 무시된다."""
    monkeypatch.setenv("MAX_AMOUNT_PER_BATCH_MINOR", "0")
    violation = limits.check_caps(_run(total_amount_minor=1))
    assert violation is not None


def test_batch_cap_within_limit_passes(claims, monkeypatch):
    monkeypatch.setenv("MAX_AMOUNT_PER_BATCH_MINOR", "500")
    assert limits.check_caps(_run(total_amount_minor=500)) is None


def test_item_cap_exceeded_in_base_currency(claims, monkeypatch):
    monkeypatch.setenv("MAX_AMOUNT_PER_ITEM_MINOR", "100")
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")
    claims["claims"] = [
        {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 101, "currency": "USD"}
    ]
    violation = limits.check_caps(_run())
    assert violation is not None
    assert "clm_1" in violation


def test_item_cap_checks_converted_amount_not_raw(claims, monkeypatch):
    """다른 통화 청구는 fx_rates로 환산한 뒤에 캡과 비교해야 한다."""
    monkeypatch.setenv("MAX_AMOUNT_PER_ITEM_MINOR", "100")
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")
    claims["claims"] = [
        {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 1000, "currency": "KRW"}
    ]
    # 1000 KRW(exponent 0) * 0.001 = 1.00 USD = 100 minor(exponent 2) — 캡 100과 같아 통과해야 한다
    violation = limits.check_caps(_run(fx_rates={"KRW/USD": "0.001"}))
    assert violation is None


def test_monthly_cap_adds_already_paid_amount(claims, monkeypatch):
    monkeypatch.setenv("MAX_AMOUNT_MONTHLY_MINOR", "1000")
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")
    claims["claims"] = [
        {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 400, "currency": "USD"}
    ]
    claims["recipients"]["rcp_1"] = {"monthly_paid_minor": 700}
    violation = limits.check_caps(_run())
    assert violation is not None
    assert "rcp_1" in violation


def test_monthly_cap_treats_missing_recipient_as_zero_paid(claims, monkeypatch):
    monkeypatch.setenv("MAX_AMOUNT_MONTHLY_MINOR", "1000")
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")
    claims["claims"] = [
        {"claim_id": "clm_1", "recipient_id": "rcp_ghost", "amount_minor": 400, "currency": "USD"}
    ]
    assert limits.check_caps(_run()) is None
