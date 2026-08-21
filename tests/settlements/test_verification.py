"""schema-contract.md §2 "검증" — 판정 로직(verify_passed)과 오케스트레이션
(verify_candidates)을 실제 Vertex AI 호출 없이 검증한다.

call_verification_model은 네트워크를 타므로 전부 monkeypatch한다 — 실제 호출
경로는 scripts/test_verification_call.py로 GCP 접근 가능한 사람이 스모크 테스트한다.
"""

import pytest

from src.schemas.models import VerificationSignals
from src.settlements import verification


def _signals(**overrides) -> VerificationSignals:
    defaults = dict(
        image_legible=True,
        amount_matches=True,
        merchant_matches=True,
        date_matches=True,
        injection_suspected=False,
    )
    defaults.update(overrides)
    return VerificationSignals(**defaults)


def _receipt(**overrides) -> dict:
    defaults = dict(
        receipt_id="rct_1",
        recipient_id="rcp_1",
        image_gcs_uri="gs://payflow-demo/receipts/x.jpg",
        parsed_amount_minor=10000,
        merchant_name="스타벅스",
        transaction_date="2026-08-10",
        verified_at=None,
        status="PARSED",
    )
    defaults.update(overrides)
    return defaults


# --- verify_passed ---


def test_all_match_passes():
    assert verification.verify_passed(_receipt(), _signals()) is True


def test_illegible_image_fails():
    assert verification.verify_passed(_receipt(), _signals(image_legible=False)) is False


def test_injection_suspected_fails_even_if_everything_else_matches():
    assert verification.verify_passed(_receipt(), _signals(injection_suspected=True)) is False


def test_amount_mismatch_fails_when_amount_present():
    r = _receipt(parsed_amount_minor=10000)
    assert verification.verify_passed(r, _signals(amount_matches=False)) is False


def test_merchant_mismatch_fails_when_merchant_present():
    r = _receipt(merchant_name="스타벅스")
    assert verification.verify_passed(r, _signals(merchant_matches=False)) is False


def test_date_mismatch_fails_when_date_present():
    r = _receipt(transaction_date="2026-08-10")
    assert verification.verify_passed(r, _signals(date_matches=False)) is False


def test_null_amount_field_not_compared():
    """근거 없는 필드는 비교하지 않는다 — amount_matches=False라도 코드가 금액을
    들고 있지 않으면 그 신호는 무시한다."""
    r = _receipt(parsed_amount_minor=None)
    assert verification.verify_passed(r, _signals(amount_matches=False)) is True


def test_null_merchant_field_not_compared():
    r = _receipt(merchant_name=None)
    assert verification.verify_passed(r, _signals(merchant_matches=False)) is True


def test_null_date_field_not_compared():
    r = _receipt(transaction_date=None)
    assert verification.verify_passed(r, _signals(date_matches=False)) is True


def test_all_fields_null_still_requires_legible_and_no_injection():
    r = _receipt(parsed_amount_minor=None, merchant_name=None, transaction_date=None)
    assert (
        verification.verify_passed(
            r, _signals(amount_matches=False, merchant_matches=False, date_matches=False)
        )
        is True
    )
    assert verification.verify_passed(r, _signals(image_legible=False)) is False


# --- verify_candidates ---


@pytest.fixture
def fake_store(monkeypatch):
    calls = {"model": [], "save": [], "claim_request": []}

    def fake_get_receipts(receipt_ids):
        return {rid: fake_store.receipts[rid] for rid in receipt_ids if rid in fake_store.receipts}

    def fake_save(receipt_id, *, passed, signals):
        calls["save"].append((receipt_id, passed))

    def fake_create_cr(*, recipient_id, receipt_id):
        calls["claim_request"].append((recipient_id, receipt_id))
        return "crq_fake"

    monkeypatch.setattr(verification.store, "get_receipts", fake_get_receipts)
    monkeypatch.setattr(verification.store, "save_verification_result", fake_save)
    monkeypatch.setattr(
        verification.store, "create_verification_failed_claim_request", fake_create_cr
    )
    fake_store.receipts = {}
    fake_store.calls = calls
    return fake_store


def _claim(claim_id, receipt_id):
    return {"claim_id": claim_id, "receipt_id": receipt_id}


def test_cached_pass_skips_model_call(fake_store, monkeypatch):
    fake_store.receipts["rct_1"] = _receipt(verified_at="2026-08-16T00:00:00Z", status="PARSED")
    monkeypatch.setattr(
        verification,
        "call_verification_model",
        lambda r: pytest.fail("이미 verified_at이 있으면 다시 부르지 않는다"),
    )

    outcome = verification.verify_candidates([_claim("clm_1", "rct_1")])

    assert [c["claim_id"] for c in outcome["passed_claims"]] == ["clm_1"]
    assert outcome["failed_claims"] == []
    assert fake_store.calls["save"] == []


def test_cached_failure_skips_model_call_and_stays_excluded(fake_store, monkeypatch):
    fake_store.receipts["rct_1"] = _receipt(
        verified_at="2026-08-16T00:00:00Z", status="VERIFICATION_FAILED"
    )
    monkeypatch.setattr(
        verification,
        "call_verification_model",
        lambda r: pytest.fail("캐싱된 실패도 다시 부르지 않는다"),
    )

    outcome = verification.verify_candidates([_claim("clm_1", "rct_1")])

    assert outcome["passed_claims"] == []
    assert [c["claim_id"] for c in outcome["failed_claims"]] == ["clm_1"]


def test_uncached_pass_calls_model_and_saves(fake_store, monkeypatch):
    fake_store.receipts["rct_1"] = _receipt(verified_at=None)
    monkeypatch.setattr(verification, "call_verification_model", lambda r: _signals())

    outcome = verification.verify_candidates([_claim("clm_1", "rct_1")])

    assert [c["claim_id"] for c in outcome["passed_claims"]] == ["clm_1"]
    assert fake_store.calls["save"] == [("rct_1", True)]
    assert fake_store.calls["claim_request"] == []


def test_uncached_failure_creates_claim_request(fake_store, monkeypatch):
    fake_store.receipts["rct_1"] = _receipt(verified_at=None, recipient_id="rcp_9")
    monkeypatch.setattr(
        verification, "call_verification_model", lambda r: _signals(amount_matches=False)
    )

    outcome = verification.verify_candidates([_claim("clm_1", "rct_1")])

    assert outcome["passed_claims"] == []
    assert [c["claim_id"] for c in outcome["failed_claims"]] == ["clm_1"]
    assert fake_store.calls["save"] == [("rct_1", False)]
    assert fake_store.calls["claim_request"] == [("rcp_9", "rct_1")]


def test_missing_receipt_excluded_without_calling_model(fake_store, monkeypatch):
    monkeypatch.setattr(
        verification,
        "call_verification_model",
        lambda r: pytest.fail("receipt가 없으면 부를 수 없다"),
    )

    outcome = verification.verify_candidates([_claim("clm_1", "rct_missing")])

    assert outcome["passed_claims"] == []
    assert [c["claim_id"] for c in outcome["failed_claims"]] == ["clm_1"]


def test_mixed_batch_splits_correctly(fake_store, monkeypatch):
    fake_store.receipts["rct_pass"] = _receipt(receipt_id="rct_pass", verified_at=None)
    fake_store.receipts["rct_fail"] = _receipt(receipt_id="rct_fail", verified_at=None)

    def fake_call(receipt):
        return _signals() if receipt["receipt_id"] == "rct_pass" else _signals(image_legible=False)

    monkeypatch.setattr(verification, "call_verification_model", fake_call)

    outcome = verification.verify_candidates(
        [_claim("clm_pass", "rct_pass"), _claim("clm_fail", "rct_fail")]
    )

    assert [c["claim_id"] for c in outcome["passed_claims"]] == ["clm_pass"]
    assert [c["claim_id"] for c in outcome["failed_claims"]] == ["clm_fail"]


# --- 순수 헬퍼 ---


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("gs://b/a.jpg", "image/jpeg"),
        ("gs://b/a.JPEG", "image/jpeg"),
        ("gs://b/a.png", "image/png"),
        ("gs://b/a.pdf", "application/pdf"),
        ("gs://b/no-extension", "image/jpeg"),
        ("gs://b/a.unknown", "image/jpeg"),
    ],
)
def test_mime_type_from_extension(uri, expected):
    assert verification._mime_type(uri) == expected


def test_parse_gcs_uri_splits_bucket_and_key():
    assert verification._parse_gcs_uri("gs://payflow-demo/receipts/sc03/taxi-a.jpg") == (
        "payflow-demo",
        "receipts/sc03/taxi-a.jpg",
    )


def test_parse_gcs_uri_rejects_non_gs_scheme():
    with pytest.raises(ValueError, match="gs://"):
        verification._parse_gcs_uri("https://example.com/a.jpg")


def test_parse_gcs_uri_rejects_missing_key():
    with pytest.raises(ValueError, match="파싱하지 못했다"):
        verification._parse_gcs_uri("gs://bucket-only")
