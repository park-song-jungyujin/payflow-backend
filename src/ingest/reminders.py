"""재촉 루프 — claim_request 하나를 보고 "지금 무엇을 할지" 정하는 순수 판정.

Firestore·네트워크·os.environ에 접근하지 않는다. `now`는 반드시 호출자가
넘긴다 — 함수 안에서 datetime.now()를 부르지 않는다. 그래야 경계 시각을
테스트로 고정할 수 있다.
"""

from datetime import datetime
from enum import StrEnum


class ReminderAction(StrEnum):
    SEND_INITIAL = "SEND_INITIAL"
    SEND_REMINDER = "SEND_REMINDER"
    EXPIRE = "EXPIRE"
    SKIP = "SKIP"


def parse_expires_at(value) -> datetime | None:
    """Firestore 문서는 datetime으로도, ISO 문자열로도 온다(src/matching/duplicates.py
    의 transaction_date 처리와 같은 관용구). 없거나 형식이 깨졌으면 None —
    만료 여부를 추측하지 않는다.

    라우트가 다음 깨어남 시각을 계산할 때 같은 해석을 써야 해서 공개한다.
    여기서 None인 값을 라우트가 다르게 읽으면 판정과 예약이 어긋난다."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


def decide(claim_request: dict, *, now: datetime) -> ReminderAction:
    status = claim_request.get("status")
    expires_at = parse_expires_at(claim_request.get("expires_at"))

    if status == "PENDING":
        # **만료가 최초 발송보다 앞선다.** 최초 DM이 못 나간 채(문안 부재·대상 부재·
        # Slack 논리 오류) 만료 시각을 넘긴 건이 여기로 다시 오면, 예전에는
        # SEND_INITIAL을 다시 내서 같은 실패를 무한히 반복했다. 만료 시각이 지난
        # 요청은 DM이 나갔든 아니든 끝난 요청이다 — 그래야 라우트가 그 경로들에
        # 걸어둔 만료 태스크가 실제로 EXPIRED에 도달한다.
        if expires_at is not None and now >= expires_at:
            return ReminderAction.EXPIRE
        if not claim_request.get("slack_dm_ts"):
            return ReminderAction.SEND_INITIAL
        if expires_at is None:
            return ReminderAction.SKIP
        return ReminderAction.SEND_REMINDER

    if status == "REMINDED":
        if expires_at is None:
            return ReminderAction.SKIP
        return ReminderAction.EXPIRE if now >= expires_at else ReminderAction.SKIP

    return ReminderAction.SKIP
