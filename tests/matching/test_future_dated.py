"""schema-contract.md §6 결정론적 매칭 — 미래 거래일 판정.

이 회귀 테스트가 지키는 버그: 2026-07-17(과거)를 LLM이 미래로 오판했던 사례
(payflow-agent tests/test_executor_tools.py와 같은 근거). today를 명시 인자로
받아 서버 시계 대신 고정값으로 테스트한다."""

from datetime import date

from src.matching.future_dated import find_future_dated_claims


def _claim(claim_id, receipt_id=None):
    return {"claim_id": claim_id, "receipt_id": receipt_id or f"rct_{claim_id}"}


def test_filters_by_today():
    claims = [
        _claim("clm_past"),
        _claim("clm_today"),
        _claim("clm_future"),
    ]
    receipts = {
        "rct_clm_past": {"transaction_date": "2026-07-17"},
        "rct_clm_today": {"transaction_date": "2026-08-23"},
        "rct_clm_future": {"transaction_date": "2026-08-24"},
    }

    result = find_future_dated_claims(claims, receipts, today=date(2026, 8, 23))

    assert result == [{"claim_id": "clm_future", "transaction_date": "2026-08-24"}]


def test_skips_missing_or_unknown_receipts():
    """근거 없는 필드는 비교하지 않는다 — find_duplicate_groups와 같은 원칙."""
    claims = [
        _claim("clm_no_receipt"),
        _claim("clm_no_date"),
    ]
    receipts = {"rct_clm_no_date": {"transaction_date": None}}

    result = find_future_dated_claims(claims, receipts, today=date(2026, 8, 23))

    assert result == []


def test_accepts_date_object_transaction_date():
    """Firestore에서 읽은 receipt는 문자열이 아니라 date 객체일 수 있다."""
    claims = [_claim("clm_1")]
    receipts = {"rct_clm_1": {"transaction_date": date(2026, 8, 24)}}

    result = find_future_dated_claims(claims, receipts, today=date(2026, 8, 23))

    assert result == [{"claim_id": "clm_1", "transaction_date": "2026-08-24"}]
