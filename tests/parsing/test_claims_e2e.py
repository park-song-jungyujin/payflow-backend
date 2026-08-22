"""청구 생성이 B의 정산 후보 조회로 실제로 이어지는지 확인한다.

claim이 만들어지는 것만으로는 부족하다. Firestore의 `== None` 쿼리가 필드
없는 문서를 못 잡기 때문에, settlement_run_id를 빠뜨리면 claim이 있어도
정산 후보에 조용히 안 잡힌다 — 이 테스트가 그 함정을 막는다.
"""

from src.ingest.claims import build_claim
from src.matching.candidates import select_claims_for_run
from src.schemas.models import Claim, SettlementFilter


def test_created_claim_matches_downstream_query_shape():
    """B의 list_confirmed_claims는 status == CONFIRMED 이고
    settlement_run_id == None 인 문서만 집는다. 두 조건을 만족해야 한다."""
    from datetime import UTC, datetime

    claim = build_claim(
        {
            "receipt_id": "rct_1",
            "org_id": "org_1",
            "recipient_id": "rcp_1",
            "parsed_amount_minor": 76500,
            "currency": "KRW",
            "account_category_code": "EMPLOYEE_BENEFIT",
        },
        now=datetime.now(UTC),
    )

    assert claim["status"] == "CONFIRMED"
    assert claim["settlement_run_id"] is None
    # 필드가 존재해야 한다 — 없으면 Firestore의 == None 쿼리가 못 잡는다
    assert "settlement_run_id" in claim
    Claim.model_validate(claim)


def test_settlement_filter_category_matching_accepts_created_claim_shape(monkeypatch):
    """`list_confirmed_claims`는 대역화한다 — 이 테스트가 검증하는 건
    status/settlement_run_id 조건이 아니라, `SettlementFilter`의
    account_categories 필터가 build_claim이 만드는 claim dict 형태와
    호환되는지다."""
    from datetime import UTC, datetime
    from src.matching import candidates as matching

    claim = build_claim(
        {
            "receipt_id": "rct_1",
            "org_id": "org_1",
            "recipient_id": "rcp_1",
            "parsed_amount_minor": 76500,
            "currency": "KRW",
            "account_category_code": "EMPLOYEE_BENEFIT",
        },
        now=datetime.now(UTC),
    )
    monkeypatch.setattr(matching, "list_confirmed_claims", lambda org_id: [claim])

    picked = select_claims_for_run("org_1", SettlementFilter(account_categories=["EMPLOYEE_BENEFIT"]))
    assert [c["claim_id"] for c in picked] == [claim["claim_id"]]

    none_picked = select_claims_for_run("org_1", SettlementFilter(account_categories=["TRAVEL"]))
    assert none_picked == []
