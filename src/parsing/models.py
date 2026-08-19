"""파싱 호출의 출력 형태 (A 소유).

**`amount_minor`가 여기 없는 게 의도다.** 파서는 영수증에 찍힌 문자열(`amount_text`)과
통화만 낸다. minor unit 곱셈은 `amount_to_minor`가 CURRENCY_EXPONENT를 읽어
Decimal로 한다 — 공통 CLAUDE.md 절대 규칙 3("금액 계산은 LLM이 하지 않는다").
USD 영수증에서 ×100을 LLM에게 시키는 순간 그 규칙이 깨진다.

이 모델은 Firestore 계약이 아니라 파싱 내부 형태다. 그래서 `src/schemas/`(공유)가
아니라 여기 둔다 — v0.5.0을 건드리지 않는다.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict

from ..payouts.currency import CURRENCY_EXPONENT
from ..schemas.enums import AccountCategory

# 통화 기호와 자릿수 구분자. 금액 문자열에서 걷어낸다.
_AMOUNT_NOISE = str.maketrans({",": None, " ": None, "₩": None, "$": None, "¥": None, "€": None, "원": None})


class ParsedReceipt(BaseModel):
    """Gemini structured output이 채우는 형태. 전부 nullable이다 —
    흐릿한 사진에서 일부만 읽히는 게 정상 경로이고(fixture 02), 못 읽은 필드는
    §5 1단계 게이트의 입력이 된다."""

    model_config = ConfigDict(extra="forbid")

    merchant_name: str | None = None
    transaction_date: date | None = None
    amount_text: str | None = None
    currency: str | None = None
    account_category_code: AccountCategory | None = None
    confidence: float | None = None
    raw_text: str = ""


def amount_to_minor(amount_text: str | None, currency: str | None) -> int | None:
    """영수증에 찍힌 문자열 → minor unit 정수. float를 거치지 않는다.

    미등록 통화는 None을 돌려준다. §4대로 기본 지수 2로 추측하지 않는다 —
    다만 여기서는 예외를 던지지 않는다. 파싱은 "못 읽었다"를 amount_parsed=False로
    표현할 자리가 있고, 그게 1단계 게이트로 이어지는 정상 경로다.
    """
    if not amount_text or not currency:
        return None
    exponent = CURRENCY_EXPONENT.get(currency)
    if exponent is None:
        return None
    try:
        value = Decimal(amount_text.translate(_AMOUNT_NOISE))
    except InvalidOperation:
        return None
    scaled = value * (Decimal(10) ** exponent)
    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))
