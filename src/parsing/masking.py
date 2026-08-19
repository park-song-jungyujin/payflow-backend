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
