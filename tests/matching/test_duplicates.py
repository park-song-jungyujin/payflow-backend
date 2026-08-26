"""schema-contract.md §6 결정론적 매칭 — 중복 청구 판정."""

import json
from datetime import date
from pathlib import Path

from src.matching.duplicates import find_duplicate_groups, find_exact_duplicate_receipts

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


def _serial_receipt(serial="A1234567"):
    """테스트에서는 미리 해시된 값 대신 임의의 불투명 문자열을 그대로 쓴다 —
    find_exact_duplicate_receipts는 receipt_serial_number_hash를 동등 비교만
    할 뿐 실제로 sha256인지는 신경 쓰지 않는다."""
    return {"receipt_serial_number_hash": serial}


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


# --- find_exact_duplicate_receipts — 영수증 고유번호 완전일치 (find_duplicate_groups보다
# 훨씬 강한 신호) ---


def test_matching_serial_and_amount_grouped():
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {"rct_1": _serial_receipt("A1234"), "rct_2": _serial_receipt("A1234")}

    groups = find_exact_duplicate_receipts(claims, receipts)

    assert len(groups) == 1
    assert set(groups[0]["claim_ids"]) == {"clm_1", "clm_2"}
    assert groups[0]["receipt_serial_number_hash"] == "A1234"
    assert groups[0]["amount_minor"] == 10000


def test_different_serial_not_grouped():
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {"rct_1": _serial_receipt("A1234"), "rct_2": _serial_receipt("B5678")}

    assert find_exact_duplicate_receipts(claims, receipts) == []


def test_same_serial_different_amount_not_grouped():
    """OCR 오독으로 서로 다른 영수증이 같은 문자열을 낸 극단적 경우를 막는 이중 확인."""
    claims = [
        _claim("clm_1", receipt_id="rct_1", amount_minor=10000),
        _claim("clm_2", receipt_id="rct_2", amount_minor=20000),
    ]
    receipts = {"rct_1": _serial_receipt("A1234"), "rct_2": _serial_receipt("A1234")}

    assert find_exact_duplicate_receipts(claims, receipts) == []


def test_same_serial_different_recipient_not_grouped():
    claims = [
        _claim("clm_1", recipient_id="rcp_1", receipt_id="rct_1"),
        _claim("clm_2", recipient_id="rcp_2", receipt_id="rct_2"),
    ]
    receipts = {"rct_1": _serial_receipt("A1234"), "rct_2": _serial_receipt("A1234")}

    assert find_exact_duplicate_receipts(claims, receipts) == []


def test_missing_serial_excluded():
    """근거 없는 필드는 비교하지 않는다 — find_duplicate_groups와 같은 원칙."""
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {"rct_1": _serial_receipt(serial=None), "rct_2": _serial_receipt("A1234")}

    assert find_exact_duplicate_receipts(claims, receipts) == []


def test_empty_string_serial_excluded():
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {"rct_1": _serial_receipt(serial=""), "rct_2": _serial_receipt(serial="")}

    assert find_exact_duplicate_receipts(claims, receipts) == []


def test_serial_group_of_one_not_returned():
    claims = [_claim("clm_1", receipt_id="rct_1")]
    receipts = {"rct_1": _serial_receipt("A1234")}

    assert find_exact_duplicate_receipts(claims, receipts) == []


def test_different_long_numeric_serials_are_not_falsely_grouped():
    """회귀 테스트 — 실제로 있었던 버그: 순수 숫자 13~19자리 영수증 고유번호가
    mask_pii의 카드번호 패턴에 걸려 전부 같은 "[CARD]"로 마스킹됐고, 이 함수가
    그 마스킹된 값을 유일성 근거로 썼다. 같은 recipient·같은 금액의 무관한 두
    영수증이 완전일치 중복(및 already_settled_claim_ids가 있으면 "이미 송금
    완료된 영수증 재청구")으로 오판됐다. hash_receipt_serial_number로 실제
    파이프라인이 저장하는 값을 그대로 재현해, 서로 다른 원문이 이제 서로 다른
    값으로 남아 오판되지 않는지 확인한다."""
    from src.parsing.masking import hash_receipt_serial_number

    claims = [
        _claim("clm_1", receipt_id="rct_1", amount_minor=4500, currency="KRW"),
        _claim("clm_2", receipt_id="rct_2", amount_minor=4500, currency="KRW"),
    ]
    receipts = {
        "rct_1": {"receipt_serial_number_hash": hash_receipt_serial_number("2026082101001234")},
        "rct_2": {"receipt_serial_number_hash": hash_receipt_serial_number("2026082201009999")},
    }

    assert find_exact_duplicate_receipts(claims, receipts) == []


def test_no_settled_args_yields_empty_already_settled_field():
    """settled_claims를 안 넘기면(기존 호출부와 하위호환) 기존처럼 후보끼리만
    비교하고, already_settled_claim_ids는 항상 빈 리스트다."""
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {"rct_1": _serial_receipt("A1234"), "rct_2": _serial_receipt("A1234")}

    groups = find_exact_duplicate_receipts(claims, receipts)

    assert groups[0]["already_settled_claim_ids"] == []


# --- find_exact_duplicate_receipts — 과거 IN_RUN·SETTLED claim과의 대조 ---
# (같은 배치 안에서만 비교하면, 이미 송금 끝난 영수증이 다른 달 배치에 다시
# 청구돼도 못 잡는다 — settled_claims/settled_receipts가 그 구멍을 닫는다.)


def test_candidate_matches_settled_claim_is_flagged():
    claims = [_claim("clm_new", receipt_id="rct_new")]
    receipts = {"rct_new": _serial_receipt("A1234")}
    settled_claims = [_claim("clm_old", receipt_id="rct_old")]
    settled_receipts = {"rct_old": _serial_receipt("A1234")}

    groups = find_exact_duplicate_receipts(
        claims, receipts, settled_claims=settled_claims, settled_receipts=settled_receipts
    )

    assert len(groups) == 1
    assert groups[0]["claim_ids"] == ["clm_new"]
    assert groups[0]["already_settled_claim_ids"] == ["clm_old"]
    assert groups[0]["receipt_serial_number_hash"] == "A1234"


def test_settled_only_pair_not_returned():
    """과거 claim끼리만 겹치는 그룹은 지금 새로 보여줄 게 없다 — candidate가
    하나도 없으면 만들지 않는다."""
    claims = []
    receipts = {}
    settled_claims = [
        _claim("clm_old_1", receipt_id="rct_old_1"),
        _claim("clm_old_2", receipt_id="rct_old_2"),
    ]
    settled_receipts = {
        "rct_old_1": _serial_receipt("A1234"),
        "rct_old_2": _serial_receipt("A1234"),
    }

    groups = find_exact_duplicate_receipts(
        claims, receipts, settled_claims=settled_claims, settled_receipts=settled_receipts
    )

    assert groups == []


def test_candidate_matches_settled_different_recipient_not_grouped():
    claims = [_claim("clm_new", recipient_id="rcp_1", receipt_id="rct_new")]
    receipts = {"rct_new": _serial_receipt("A1234")}
    settled_claims = [_claim("clm_old", recipient_id="rcp_2", receipt_id="rct_old")]
    settled_receipts = {"rct_old": _serial_receipt("A1234")}

    groups = find_exact_duplicate_receipts(
        claims, receipts, settled_claims=settled_claims, settled_receipts=settled_receipts
    )

    assert groups == []
