"""schema-contract.md §4 — per_recipient_amounts가 실제 PayPal 송금액을 만드는
지점이다. 청구 전체 반려(excluded=true, settlements/routes.py._apply_claim_exclusion)
된 claim이 여기서 빠지는지가 이 스위트의 핵심 계약이다 — 안 빠지면 반려는
화면에만 표시되고 돈은 그대로 나간다."""

from src.payouts import amounts


def test_sums_claims_per_recipient_in_base_currency(monkeypatch):
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")
    monkeypatch.setattr(
        amounts,
        "get_claims_for_run",
        lambda run_id: [
            {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 1000, "currency": "USD"},
            {"claim_id": "clm_2", "recipient_id": "rcp_1", "amount_minor": 500, "currency": "USD"},
            {"claim_id": "clm_3", "recipient_id": "rcp_2", "amount_minor": 200, "currency": "USD"},
        ],
    )

    result = amounts.per_recipient_amounts({"settlement_run_id": "run_1", "fx_rates": {}})

    assert result == {"rcp_1": 1500, "rcp_2": 200}


def test_excluded_claim_is_not_paid(monkeypatch):
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")
    monkeypatch.setattr(
        amounts,
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

    result = amounts.per_recipient_amounts({"settlement_run_id": "run_1", "fx_rates": {}})

    assert result == {"rcp_1": 1000}


def test_recipient_with_only_excluded_claims_is_absent_from_totals(monkeypatch):
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")
    monkeypatch.setattr(
        amounts,
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

    result = amounts.per_recipient_amounts({"settlement_run_id": "run_1", "fx_rates": {}})

    assert result == {}


def test_converts_non_base_currency_using_locked_fx_rates(monkeypatch):
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")
    monkeypatch.setattr(
        amounts,
        "get_claims_for_run",
        lambda run_id: [
            {"claim_id": "clm_1", "recipient_id": "rcp_1", "amount_minor": 1000, "currency": "KRW"}
        ],
    )

    result = amounts.per_recipient_amounts(
        {"settlement_run_id": "run_1", "fx_rates": {"KRW/USD": "0.001"}}
    )

    # 1000 KRW(exponent 0) * 0.001 = 1.00 USD = 100 minor(exponent 2)
    assert result == {"rcp_1": 100}
