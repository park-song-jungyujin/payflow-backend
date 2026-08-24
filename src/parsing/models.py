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


class ParsedReceiptItem(BaseModel):
    """영수증 물품 내역 한 줄. amount_minor가 없는 이유는 위 ParsedReceipt와
    같다 — 금액 계산은 LLM이 하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    amount_text: str | None = None


class ParsedReceipt(BaseModel):
    """Gemini structured output이 채우는 형태. 전부 nullable이다 —
    흐릿한 사진에서 일부만 읽히는 게 정상 경로이고(fixture 02), 못 읽은 필드는
    §5 1단계 게이트의 입력이 된다."""

    model_config = ConfigDict(extra="forbid")

    merchant_name: str | None = None
    transaction_date: date | None = None
    # "HH:MM" 24시간제 문자열. transaction_date와 같은 §1 시각 예외 — 영수증에
    # 찍힌 결제 시각이지 타임존이 없다. 초 단위는 OCR 신뢰도가 낮아 받지 않는다.
    transaction_time: str | None = None
    # 카드 승인번호 · 영수증/거래 고유번호. 카드사·POS가 거래 건마다 고유하게
    # 발급하므로, 있으면 거의 확실한 동일 거래 판정 근거가 된다(중복 청구 탐지).
    receipt_serial_number: str | None = None
    amount_text: str | None = None
    currency: str | None = None
    account_category_code: AccountCategory | None = None
    confidence: float | None = None
    raw_text: str = ""
    # 영수증에 개별 품목이 인쇄돼 있으면 옮긴다. 없거나 못 읽으면 빈 리스트 —
    # 정산 판단(§5 게이트, amount_parsed 등)은 여전히 전체 amount_text 기준이라
    # items가 비어도 파싱 자체는 정상이다.
    items: list[ParsedReceiptItem] = []


def amount_to_minor(amount_text: str | None, currency: str | None) -> int | None:
    """영수증에 찍힌 문자열 → minor unit 정수. float를 거치지 않는다.

    미등록 통화는 None을 돌려준다. §4대로 기본 지수 2로 추측하지 않는다.
    판독 불가(NaN/Infinity/정밀도 초과/지수표기), 지수 0 통화의 소수점(KRW·JPY에
    `.`이 나오면 천단위 구분자 오독이나 OCR 오류이지 소수점일 수 없다 — 추측해서
    자르지 않고 못 읽은 것으로 떨어뜨린다), 음수(정상 영수증에 음수 금액은 없다)는
    전부 None이다. 예외를 던지지 않는다 — 파싱은 "못 읽었다"를 amount_parsed=False로
    표현할 자리가 있고, 그게 1단계 게이트로 이어지는 정상 경로다.
    """
    if not amount_text or not currency:
        return None
    exponent = CURRENCY_EXPONENT.get(currency)
    if exponent is None:
        return None
    cleaned = amount_text.translate(_AMOUNT_NOISE)
    if exponent == 0 and "." in cleaned:
        return None
    try:
        value = Decimal(cleaned)
        if not value.is_finite() or value < 0:
            return None
        scaled = value * (Decimal(10) ** exponent)
        return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        return None
