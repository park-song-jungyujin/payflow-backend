"""schema-contract.md §6 결정론적 매칭 — 중복 청구 판정."""

import json
from datetime import date
from pathlib import Path

from src.matching.duplicates import find_duplicate_groups

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _claim(claim_id, recipient_id="rcp_1", receipt_id=None, amount_minor=10000, currency="KRW"):
    return {
        "claim_id": claim_id,
        "recipient_id": recipient_id,
        "receipt_id": receipt_id or f"rct_{claim_id}",
        "amount_minor": amount_minor,
        "currency": currency,
    }


def _receipt(merchant_name="스타벅스", transaction_date="2026-08-10"):
    return {"merchant_name": merchant_name, "transaction_date": transaction_date}


def test_exact_match_groups_two_claims():
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {"rct_1": _receipt(), "rct_2": _receipt()}

    groups = find_duplicate_groups(claims, receipts)

    assert len(groups) == 1
    assert set(groups[0]["claim_ids"]) == {"clm_1", "clm_2"}
    assert groups[0]["amount_minor"] == 10000
    assert groups[0]["merchant_name"] == "스타벅스"


def test_different_merchant_not_grouped():
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": _receipt(merchant_name="스타벅스"),
        "rct_2": _receipt(merchant_name="투썸플레이스"),
    }

    assert find_duplicate_groups(claims, receipts) == []


def test_different_amount_not_grouped():
    claims = [
        _claim("clm_1", receipt_id="rct_1", amount_minor=10000),
        _claim("clm_2", receipt_id="rct_2", amount_minor=10001),
    ]
    receipts = {"rct_1": _receipt(), "rct_2": _receipt()}

    assert find_duplicate_groups(claims, receipts) == []


def test_different_currency_not_grouped():
    claims = [
        _claim("clm_1", receipt_id="rct_1", currency="KRW"),
        _claim("clm_2", receipt_id="rct_2", currency="USD"),
    ]
    receipts = {"rct_1": _receipt(), "rct_2": _receipt()}

    assert find_duplicate_groups(claims, receipts) == []


def test_different_recipient_not_grouped():
    claims = [
        _claim("clm_1", recipient_id="rcp_1", receipt_id="rct_1"),
        _claim("clm_2", recipient_id="rcp_2", receipt_id="rct_2"),
    ]
    receipts = {"rct_1": _receipt(), "rct_2": _receipt()}

    assert find_duplicate_groups(claims, receipts) == []


def test_date_within_window_grouped(monkeypatch):
    monkeypatch.setenv("MATCHING_DATE_WINDOW_DAYS", "3")
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": _receipt(transaction_date="2026-08-10"),
        "rct_2": _receipt(transaction_date="2026-08-13"),  # 정확히 경계값
    }

    groups = find_duplicate_groups(claims, receipts)

    assert len(groups) == 1


def test_date_outside_window_not_grouped(monkeypatch):
    monkeypatch.setenv("MATCHING_DATE_WINDOW_DAYS", "3")
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": _receipt(transaction_date="2026-08-10"),
        "rct_2": _receipt(transaction_date="2026-08-14"),  # 경계값 + 1일
    }

    assert find_duplicate_groups(claims, receipts) == []


def test_default_window_is_three_days():
    """MATCHING_DATE_WINDOW_DAYS 미설정 시 문서(schema-contract.md §11) 기본값 3."""
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": _receipt(transaction_date="2026-08-10"),
        "rct_2": _receipt(transaction_date="2026-08-13"),
    }

    assert len(find_duplicate_groups(claims, receipts)) == 1


def test_missing_merchant_excluded():
    """근거 없는 필드는 비교하지 않는다 — merchant_name이 없으면 판정에서 뺀다."""
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {"rct_1": _receipt(merchant_name=None), "rct_2": _receipt()}

    assert find_duplicate_groups(claims, receipts) == []


def test_missing_transaction_date_excluded():
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {"rct_1": _receipt(transaction_date=None), "rct_2": _receipt()}

    assert find_duplicate_groups(claims, receipts) == []


def test_merchant_name_normalized_whitespace_and_case():
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": _receipt(merchant_name="Starbucks Korea"),
        "rct_2": _receipt(merchant_name="starbucks   korea"),
    }

    assert len(find_duplicate_groups(claims, receipts)) == 1


def test_transitive_chain_via_window(monkeypatch):
    """A-B, B-C는 윈도우 안이지만 A-C는 밖이어도 B를 통해 한 그룹으로 묶인다."""
    monkeypatch.setenv("MATCHING_DATE_WINDOW_DAYS", "3")
    claims = [
        _claim("clm_a", receipt_id="rct_a"),
        _claim("clm_b", receipt_id="rct_b"),
        _claim("clm_c", receipt_id="rct_c"),
    ]
    receipts = {
        "rct_a": _receipt(transaction_date="2026-08-01"),
        "rct_b": _receipt(transaction_date="2026-08-04"),
        "rct_c": _receipt(transaction_date="2026-08-07"),
    }

    groups = find_duplicate_groups(claims, receipts)

    assert len(groups) == 1
    assert set(groups[0]["claim_ids"]) == {"clm_a", "clm_b", "clm_c"}


def test_group_of_one_not_returned():
    claims = [_claim("clm_1", receipt_id="rct_1")]
    receipts = {"rct_1": _receipt()}

    assert find_duplicate_groups(claims, receipts) == []


def test_no_receipt_for_claim_excluded():
    claims = [_claim("clm_1", receipt_id="rct_missing")]
    receipts = {}

    assert find_duplicate_groups(claims, receipts) == []


def test_fixture_03_duplicate_claim_detected():
    """schema-contract.md §12 fixture 3 — 카카오모빌리티 택시비 18,500원 2건."""
    data = json.loads((FIXTURES_DIR / "03_duplicate_claim.json").read_text(encoding="utf-8"))
    receipts = {r["receipt_id"]: r for r in data["receipts"]}

    groups = find_duplicate_groups(data["claims"], receipts)

    assert len(groups) == 1
    assert set(groups[0]["claim_ids"]) == {
        "clm_01SCN03TAXICLAIM000000001",
        "clm_01SCN03TAXICLAIM000000002",
    }
    assert groups[0]["amount_minor"] == 18500
    assert groups[0]["merchant_name"] == "카카오모빌리티"
    assert groups[0]["transaction_date"] == "2026-08-10"


def test_transaction_date_accepts_date_object():
    """receipts가 Firestore에서 오면 문자열이 아니라 date 객체다."""
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": _receipt(transaction_date=date(2026, 8, 10)),
        "rct_2": _receipt(transaction_date=date(2026, 8, 10)),
    }

    assert len(find_duplicate_groups(claims, receipts)) == 1
