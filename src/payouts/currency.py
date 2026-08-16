"""schema-contract.md §4 — minor unit을 PayPal 문자열 소수로 변환한다.
통화별 지수 테이블과 변환 함수는 이 모듈이 단독으로 갖는다. float 금지(money-safety.md).
"""

CURRENCY_EXPONENT: dict[str, int] = {
    "KRW": 0,
    "JPY": 0,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "TND": 3,
}


def minor_to_paypal_value(amount_minor: int, currency: str) -> str:
    exponent = CURRENCY_EXPONENT[currency]
    if exponent == 0:
        return str(amount_minor)
    divisor = 10**exponent
    whole, remainder = divmod(amount_minor, divisor)
    return f"{whole}.{remainder:0{exponent}d}"
