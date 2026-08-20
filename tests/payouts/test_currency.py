"""money-safety.md — float 금지, minor unit 변환. 여태 테스트가 없던 순수 함수라
반올림 경계·exponent 0 통화(KRW/JPY) 처리가 회귀에 가장 취약한 지점이다."""

from decimal import Decimal

import pytest

from src.payouts.currency import (
    UnsupportedPayoutCurrency,
    assert_supported_payout_currency,
    convert_minor,
    minor_to_paypal_value,
)


def test_convert_minor_same_currency_passes_through_unchanged():
    assert convert_minor(12345, "USD", "USD", Decimal("1")) == 12345


def test_convert_minor_krw_exponent_zero_to_usd_exponent_two():
    # 1000 KRW * 0.00075 = 0.75 USD = 75 minor
    assert convert_minor(1000, "KRW", "USD", Decimal("0.00075")) == 75


def test_convert_minor_rounds_half_up_at_exact_boundary():
    """ROUND_HALF_UP 명시 — 은행반올림(HALF_EVEN)이 아니다. 정확히 .5는 올림이어야 한다."""
    # 50 minor(0.50 USD) -> JPY(exponent 0), rate 1: 0.5*1 = 0.5 -> half up -> 1
    assert convert_minor(50, "USD", "JPY", Decimal("1")) == 1


def test_convert_minor_rounds_down_when_below_half():
    assert convert_minor(49, "USD", "JPY", Decimal("1")) == 0  # 0.49 -> round to 0


def test_convert_minor_three_decimal_exponent_currency():
    # TND는 exponent 3 — 1 USD(100 minor) * rate 3.1 -> 3.1 TND = 3100 minor
    assert convert_minor(100, "USD", "TND", Decimal("3.1")) == 3100


def test_minor_to_paypal_value_zero_exponent_currency_has_no_decimal_point():
    assert minor_to_paypal_value(1500, "KRW") == "1500"


def test_minor_to_paypal_value_pads_small_remainder():
    """5센트가 '.5'가 아니라 '.05'로 나와야 한다 — PayPal이 형식 위반으로 400을 낸다."""
    assert minor_to_paypal_value(5, "USD") == "0.05"


def test_minor_to_paypal_value_whole_amount():
    assert minor_to_paypal_value(100, "USD") == "1.00"


def test_minor_to_paypal_value_three_decimal_currency():
    assert minor_to_paypal_value(3100, "TND") == "3.100"


def test_assert_supported_payout_currency_accepts_usd():
    assert_supported_payout_currency("USD")  # 예외 없으면 통과


def test_assert_supported_payout_currency_rejects_krw():
    """KRW는 CURRENCY_EXPONENT엔 있지만 PayPal Payouts 송금 통화로는 미지원 —
    fx.py는 KRW를 base_currency로 쓸 수 있어도 송금 자체는 막혀야 한다."""
    with pytest.raises(UnsupportedPayoutCurrency):
        assert_supported_payout_currency("KRW")


def test_assert_supported_payout_currency_rejects_unknown_code():
    with pytest.raises(UnsupportedPayoutCurrency):
        assert_supported_payout_currency("XXX")
