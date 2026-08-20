"""src/matching — TEMP(B) 필터 적용 로직(결정론적 매칭 자체는 아직 미구현이지만
지금 있는 필터 조합 코드는 실제로 A/C의 E2E를 떠받치고 있어 회귀에 취약하다)."""

from datetime import date

import pytest

from src.matching import select_claims_for_run
from src.schemas.models import SettlementFilter


class FakeDoc:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class FakeDocRef:
    def __init__(self, data):
        self._data = data

    def get(self):
        return FakeDoc(self._data)


class FakeCollection:
    def __init__(self, receipts):
        self._receipts = receipts

    def document(self, doc_id):
        return FakeDocRef(self._receipts.get(doc_id))


class FakeClient:
    def __init__(self, receipts):
        self._receipts = receipts

    def collection(self, name):
        assert name == "receipts"
        return FakeCollection(self._receipts)


def _claim(claim_id, **overrides):
    claim = {
        "claim_id": claim_id,
        "recipient_id": "rcp_1",
        "receipt_id": f"rct_{claim_id}",
        "account_category_code": "EMPLOYEE_BENEFIT",
    }
    claim.update(overrides)
    return claim


@pytest.fixture
def candidates(monkeypatch):
    store = {"claims": [], "receipts": {}}
    import src.matching as matching_mod

    monkeypatch.setattr(matching_mod, "list_confirmed_claims", lambda: store["claims"])
    monkeypatch.setattr(matching_mod, "get_client", lambda: FakeClient(store["receipts"]))
    return store


def test_no_filters_returns_all_confirmed_claims(candidates):
    candidates["claims"] = [_claim("c1"), _claim("c2")]
    result = select_claims_for_run(SettlementFilter())
    assert {c["claim_id"] for c in result} == {"c1", "c2"}


def test_filters_by_recipient_ids(candidates):
    candidates["claims"] = [_claim("c1", recipient_id="rcp_1"), _claim("c2", recipient_id="rcp_2")]
    result = select_claims_for_run(SettlementFilter(recipient_ids=["rcp_1"]))
    assert [c["claim_id"] for c in result] == ["c1"]


def test_filters_by_account_categories(candidates):
    candidates["claims"] = [
        _claim("c1", account_category_code="EMPLOYEE_BENEFIT"),
        _claim("c2", account_category_code="TRAVEL"),
    ]
    result = select_claims_for_run(SettlementFilter(account_categories=["EMPLOYEE_BENEFIT"]))
    assert [c["claim_id"] for c in result] == ["c1"]


def test_excludes_claim_ids(candidates):
    candidates["claims"] = [_claim("c1"), _claim("c2")]
    result = select_claims_for_run(SettlementFilter(exclude_claim_ids=["c1"]))
    assert [c["claim_id"] for c in result] == ["c2"]


def test_period_filter_excludes_claims_with_no_transaction_date(candidates):
    """영수증에 transaction_date가 아예 없으면(파싱 실패 등) 기간 필터에서
    무조건 빠져야 한다 — None을 '기간 안'으로 잘못 통과시키면 정산 누락이 조용히 생긴다."""
    candidates["claims"] = [_claim("c1")]
    candidates["receipts"]["rct_c1"] = {"transaction_date": None}
    result = select_claims_for_run(SettlementFilter(period_start="2026-01-01"))
    assert result == []


def test_period_filter_includes_claims_within_window(candidates):
    candidates["claims"] = [_claim("c1")]
    candidates["receipts"]["rct_c1"] = {"transaction_date": date(2026, 1, 15)}
    result = select_claims_for_run(
        SettlementFilter(period_start="2026-01-01", period_end="2026-01-31")
    )
    assert [c["claim_id"] for c in result] == ["c1"]


def test_period_filter_excludes_claims_outside_window(candidates):
    candidates["claims"] = [_claim("c1")]
    candidates["receipts"]["rct_c1"] = {"transaction_date": date(2026, 2, 1)}
    result = select_claims_for_run(
        SettlementFilter(period_start="2026-01-01", period_end="2026-01-31")
    )
    assert result == []


def test_missing_receipt_document_treated_as_out_of_period(candidates):
    """receipt 문서 자체가 없으면(참조 무결성 깨짐) 크래시 대신 필터에서 빠뜨린다."""
    candidates["claims"] = [_claim("c1")]
    # receipts에 rct_c1을 아예 안 넣음 -> to_dict() None
    result = select_claims_for_run(SettlementFilter(period_start="2026-01-01"))
    assert result == []


def test_filters_combine_with_and_semantics(candidates):
    candidates["claims"] = [
        _claim("c1", recipient_id="rcp_1", account_category_code="EMPLOYEE_BENEFIT"),
        _claim("c2", recipient_id="rcp_1", account_category_code="TRAVEL"),
        _claim("c3", recipient_id="rcp_2", account_category_code="EMPLOYEE_BENEFIT"),
    ]
    result = select_claims_for_run(
        SettlementFilter(recipient_ids=["rcp_1"], account_categories=["EMPLOYEE_BENEFIT"])
    )
    assert [c["claim_id"] for c in result] == ["c1"]
