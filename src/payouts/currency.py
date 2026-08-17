"""schema-contract.md §4 — minor unit을 PayPal 문자열 소수로 변환한다.
통화별 지수 테이블과 변환 함수는 이 모듈이 단독으로 갖는다. float 금지(money-safety.md).
"""

from decimal import ROUND_HALF_UP, Decimal

CURRENCY_EXPONENT: dict[str, int] = {
    "KRW": 0,
    "JPY": 0,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "TND": 3,
}

# PayPal Payouts API가 송금 통화로 지원하는 목록 (KRW 미포함 — 2026-08-17 sandbox 400 확인).
PAYPAL_SUPPORTED_PAYOUT_CURRENCIES: frozenset[str] = frozenset(
    {
        "AUD", "BRL", "CAD", "CZK", "DKK", "EUR", "GBP", "HKD", "HUF", "ILS",
        "JPY", "MXN", "MYR", "NOK", "NZD", "PHP", "PLN", "RUB", "SEK", "SGD",
        "THB", "TWD", "USD",
    }
)


class UnsupportedPayoutCurrency(Exception):
    pass


def assert_supported_payout_currency(currency: str) -> None:
    if currency not in PAYPAL_SUPPORTED_PAYOUT_CURRENCIES:
        raise UnsupportedPayoutCurrency(currency)


def convert_minor(amount_minor: int, from_currency: str, to_currency: str, rate: Decimal) -> int:
    """schema-contract.md §4 환산 순서 — 항목별로 환산한다. 합산은 호출자가 한다."""
    if from_currency == to_currency:
        return amount_minor
    real_amount = Decimal(amount_minor) / (Decimal(10) ** CURRENCY_EXPONENT[from_currency])
    converted = real_amount * rate * (Decimal(10) ** CURRENCY_EXPONENT[to_currency])
    return int(converted.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def minor_to_paypal_value(amount_minor: int, currency: str) -> str:
    exponent = CURRENCY_EXPONENT[currency]
    if exponent == 0:
        return str(amount_minor)
    divisor = 10**exponent
    whole, remainder = divmod(amount_minor, divisor)
    return f"{whole}.{remainder:0{exponent}d}"
