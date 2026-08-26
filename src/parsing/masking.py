"""PII 마스킹 — schema-contract.md §2 "Firestore 쓰기 전에".

파이프라인과 감사 로그가 **같은 함수**를 부른다. 두 경로가 갈라지면 한쪽에만
원문이 남고, 그게 가장 흔한 유출 형태다.

**과소 마스킹보다 과대 마스킹이 더 위험한 필드가 있다.** merchant_name은 §6
결정론적 매칭의 비교 축이라, 상호를 지워버리면 매칭이 전부 조용히 실패한다.
그래서 패턴은 전부 "구분자·자릿수가 뚜렷한 것"으로만 좁혔고, 상호에 흔히 붙는
숫자(1호점, GS25)는 어느 패턴에도 걸리지 않는다.

**이건 인젝션 방어가 아니다.** 인젝션은 §5 1단계 게이트(categorize.detect_injection)가
막는다. 마스커는 지시문을 지우지 않는다 — 지우면 게이트가 볼 근거가 사라진다.
"""

import hashlib
import re

# 순서가 있다. 좁은 패턴을 먼저 태워야 넓은 패턴이 잡아먹지 않는다.
# (주민번호 900101-1234567은 자릿수만 보면 카드번호로도 읽힌다)
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"\b\d{6}-\d{7}\b"), "[RRN]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{5}\b"), "[BIZNO]"),
    # 카드번호: 13~19자리. 구분자가 있으면 4자리 묶음만, 없으면 연속 13자리 이상.
    # 하한을 13으로 두어 "45,000"이나 "99000" 같은 금액이 걸리지 않는다.
    (re.compile(r"\b(?:\d{4}[- ]){3}\d{4}\b"), "[CARD]"),
    (re.compile(r"\b\d{13,19}\b"), "[CARD]"),
    # 한국 전화번호: 0으로 시작하고 구분자가 있는 것만. 구분자를 필수로 둬서
    # "GS25 역삼2호점" 같은 상호 속 숫자를 건드리지 않는다.
    (re.compile(r"\b0\d{1,2}-\d{3,4}-\d{4}\b"), "[PHONE]"),
]


def mask_pii(text: str | None) -> str | None:
    if text is None:
        return None
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def hash_receipt_serial_number(value: str | None) -> str | None:
    """receipt_serial_number(카드 승인번호·거래고유번호·영수증 일련번호)는
    mask_pii로 마스킹하면 안 된다 — 실제로 발생한 버그: 이 필드는 거의 항상
    순수 숫자 문자열인데, mask_pii의 카드번호 패턴(13~19자리 연속 숫자)이 여기
    걸려 서로 다른 영수증의 고유번호가 전부 같은 리터럴 "[CARD]"로 뭉개졌다.
    matching/duplicates.py.find_exact_duplicate_receipts는 이 필드가
    거래마다 고유하다는 전제로 완전일치 판정을 하므로, 마스킹으로 유일성이
    사라지면 같은 recipient·같은 금액의 무관한 영수증들이 전부 "완전일치 중복"
    (already_settled_claim_ids가 있으면 "이미 송금 완료된 영수증 재청구")으로
    오판되고, 지금은 그 판정이 자동 반려로 이어진다.

    원문을 그대로 Firestore에 저장할 수는 없으므로(§2, 원본은 GCS에만) 되돌릴
    수 없는 해시로 바꿔 저장한다 — 완전일치 비교에는 그대로 쓸 수 있고, 해시에서
    원래 번호를 복원할 수는 없다."""
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
