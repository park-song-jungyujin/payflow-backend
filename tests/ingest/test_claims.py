"""schema-contract.md §2 claims — 파싱 결과에서 청구 항목을 만든다.

이 스위트가 지키는 것 셋:
1. 금액을 **옮기기만** 한다. 계산하지 않는다(절대 규칙 3)
2. settlement_run_id·settled_at을 **명시적 None**으로 쓴다 — Firestore의
   `== None` 쿼리가 필드 없는 문서를 못 잡아서, 빠뜨리면 B의 정산 후보에
   조용히 안 잡힌다
3. 금액이나 통화가 없는 영수증으로는 claim을 만들지 않는다
"""

from datetime import UTC, datetime

import pytest

from src.ingest.claims import (
    CLAIM_STATUS_ON_CREATE,
    DEFAULT_IS_BUSINESS,
    ClaimNotCreatable,
    build_claim,
)
from src.schemas.models import Claim

NOW = datetime(2026, 8, 21, 1, 37, tzinfo=UTC)


def _receipt(**overrides) -> dict:
    r = {
        "receipt_id": "rct_1",
        "org_id": "org_1",
        "recipient_id": "rcp_1",
        "parsed_amount_minor": 76500,
        "currency": "KRW",
        "account_category_code": "EMPLOYEE_BENEFIT",
        "status": "PARSED",
    }
    r.update(overrides)
    return r


def test_builds_claim_from_parsed_receipt():
    claim = build_claim(_receipt(), now=NOW)

    assert claim["claim_id"].startswith("clm_")
    assert claim["recipient_id"] == "rcp_1"
    assert claim["receipt_id"] == "rct_1"
    assert claim["amount_minor"] == 76500
    assert claim["currency"] == "KRW"
    assert claim["account_category_code"] == "EMPLOYEE_BENEFIT"
    assert claim["status"] == CLAIM_STATUS_ON_CREATE.value
    assert claim["created_at"] == NOW
    assert claim["updated_at"] == NOW


def test_amount_is_copied_not_recomputed():
    """절대 규칙 3 — 이 단계에서 금액을 만들지 않는다. 영수증 값을 그대로 옮긴다."""
    claim = build_claim(_receipt(parsed_amount_minor=684730, currency="KRW"), now=NOW)
    assert claim["amount_minor"] == 684730
    assert isinstance(claim["amount_minor"], int)


def test_occupancy_fields_are_explicit_null():
    """★ B의 list_confirmed_claims가 `settlement_run_id == None`으로 필터한다.
    Firestore는 필드가 없는 문서를 이 쿼리로 못 잡는다 — 생략하면 claim이
    만들어져도 정산 후보에 조용히 안 잡힌다."""
    claim = build_claim(_receipt(), now=NOW)
    assert "settlement_run_id" in claim
    assert claim["settlement_run_id"] is None
    assert "settled_at" in claim
    assert claim["settled_at"] is None


def test_is_business_defaults_to_true():
    """청구자 에이전트가 판단할 필드인데 아직 501 스텁이다.
    업무 경비가 개인용으로 잘못 분류되면 청구에서 조용히 빠진다 — 그 방향을 피한다."""
    assert DEFAULT_IS_BUSINESS is True
    assert build_claim(_receipt(), now=NOW)["is_business"] is True


def test_unclassified_category_still_business():
    """UNCLASSIFIED는 "계정과목을 못 정했다"이지 "개인 지출이다"가 아니다."""
    claim = build_claim(_receipt(account_category_code="UNCLASSIFIED"), now=NOW)
    assert claim["is_business"] is True
    assert claim["account_category_code"] == "UNCLASSIFIED"


@pytest.mark.parametrize(
    "missing",
    [
        {"parsed_amount_minor": None},
        {"currency": None},
        {"parsed_amount_minor": None, "currency": None},
        {"recipient_id": None},
    ],
)
def test_refuses_when_required_values_missing(missing):
    """금액 없는 청구는 만들 수 없다. 조용히 0원 claim을 만드느니 안 만든다."""
    with pytest.raises(ClaimNotCreatable):
        build_claim(_receipt(**missing), now=NOW)


def test_missing_category_falls_back_to_unclassified():
    """account_category_code는 claims에서 필수(nullable 아님)다.
    영수증에 없으면 UNCLASSIFIED로 채운다 — 청구 자체를 막을 이유는 아니다."""
    claim = build_claim(_receipt(account_category_code=None), now=NOW)
    assert claim["account_category_code"] == "UNCLASSIFIED"


def test_claim_ids_are_unique():
    a = build_claim(_receipt(), now=NOW)["claim_id"]
    b = build_claim(_receipt(), now=NOW)["claim_id"]
    assert a != b


def test_built_claim_validates_against_contract():
    """schema-contract.md §2 — 만든 문서가 Claim 모델로 검증돼야 한다."""
    Claim.model_validate(build_claim(_receipt(), now=NOW))
