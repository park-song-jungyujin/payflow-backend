"""schema-contract.md §5 계정과목 라우팅 — 2단계, 순서가 핵심 (A 소유).

1단계(결정론적 신호)가 게이트다. 하나라도 걸리면 LLM confidence를 **보지 않고**
즉시 UNCLASSIFIED. 2단계는 1단계가 전부 깨끗할 때만 임계값을 본다.

`injection_suspected`를 코드가 판정하는 이유: 이건 하드 게이트인데, 판정을 LLM에게
맡기면 인젝션 대상이 자기가 인젝션인지를 자기 보고하는 구조가 된다. 같은 절이
"LLM이 자기 보고하는 confidence는 캘리브레이션이 안 되므로 혼자서는 게이트가 못
된다"고 말하는 것과 같은 이유다.
"""

import os
import re

from ..schemas.enums import AccountCategory, CategorySource
from ..schemas.models import ParseSignals
from .models import ParsedReceipt

_DEFAULT_THRESHOLD = 0.7

# 영수증 원문에 나올 이유가 없는 "지시문" 형태만 좁게 잡는다. 오탐이 나면 정상
# 영수증이 전부 UNCLASSIFIED로 떨어져 데모가 무너지므로 넓히지 않는다 — 같은 계열의
# 철자 변형(관사·수식어 삽입 등)만 덮고 새 계열은 추가하지 않는다.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    # ignore/disregard + (all/the/any)* + previous/prior/above — "ignore all prior
    # instructions", "Ignore the above and pay" 같은 변형도 잡는다.
    re.compile(r"(?:ignore|disregard)\s+(?:all\s+|the\s+|any\s+)*(?:previous|prior|above)", re.IGNORECASE),
    re.compile(r"(?:^|\W)(?:SYSTEM|ASSISTANT|DEVELOPER)\s*:", re.IGNORECASE),
    # (이전|앞선|위) ... (지시|명령|규칙|내용) ... 무시 — 사이에 수식어("모든", "모두")가
    # 끼어도 잡히도록 간격을 허용한다.
    re.compile(r"(?:이전|앞선|위)[^\n]{0,10}(?:지시|명령|규칙|내용)[^\n]{0,15}무시"),
    re.compile(r"</?untrusted_receipt_text>", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+an?\s+", re.IGNORECASE),
]


def detect_injection(raw_text: str) -> bool:
    """정규식 기반 1차 필터다. `raw_text`는 그 자체가 LLM 출력이라, 모델이 인젝션
    문구를 원문 그대로 옮겨 적지 않으면 여기서 탐지가 성립하지 않는다. 이 함수를
    유일한 방어선으로 오해하지 말 것 — 실제 방어는 에이전트 프롬프트의
    `<untrusted_receipt_text>` 격리다. 여기서는 이미 게이트를 통과해버릴 흔한
    지시문 패턴만 걸러 §5 1단계로 넘긴다."""
    return any(pattern.search(raw_text or "") for pattern in _INJECTION_PATTERNS)


def build_parse_signals(parsed: ParsedReceipt, amount_minor: int | None) -> ParseSignals:
    """§5의 신호 5개. amount_parsed는 파서가 문자열을 냈는지가 아니라 **코드가
    minor unit으로 바꾸는 데 성공했는지**를 본다 — 판독 불가 문자열이나 미등록
    통화를 "금액을 읽었다"로 세면 게이트가 뚫린다."""
    return ParseSignals(
        merchant_name_present=bool(parsed.merchant_name),
        transaction_date_present=parsed.transaction_date is not None,
        amount_parsed=amount_minor is not None,
        currency_detected=bool(parsed.currency),
        injection_suspected=detect_injection(parsed.raw_text),
    )


def _threshold() -> float:
    return float(os.environ.get("PARSING_CONFIDENCE_THRESHOLD", _DEFAULT_THRESHOLD))


def route_category(
    parsed: ParsedReceipt, signals: ParseSignals
) -> tuple[AccountCategory, CategorySource, float | None]:
    """(코드값, 출처, 저장할 llm_confidence)를 돌려준다.

    1단계에 걸리면 confidence를 None으로 돌려준다 — 보지 않은 값을 저장하면
    나중에 읽는 쪽이 "이 confidence가 판단에 쓰였다"고 오해한다. fixture 02·06이
    둘 다 llm_confidence: null인 게 이 규칙의 근거다.
    """
    stage1_clean = (
        signals.merchant_name_present
        and signals.transaction_date_present
        and signals.amount_parsed
        and signals.currency_detected
        and not signals.injection_suspected
    )
    if not stage1_clean:
        return AccountCategory.UNCLASSIFIED, CategorySource.DETERMINISTIC_FALLBACK, None

    # 2단계 — 여기서부터는 confidence를 실제로 봤으므로 기각하더라도 저장한다.
    if parsed.confidence is None or parsed.confidence < _threshold():
        return AccountCategory.UNCLASSIFIED, CategorySource.DETERMINISTIC_FALLBACK, parsed.confidence
    if parsed.account_category_code is None:
        return AccountCategory.UNCLASSIFIED, CategorySource.DETERMINISTIC_FALLBACK, parsed.confidence
    return parsed.account_category_code, CategorySource.LLM_PARSE, parsed.confidence
