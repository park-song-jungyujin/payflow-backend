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


def _parse_expires_at(value) -> datetime | None:
    """Firestore 문서는 datetime으로도, ISO 문자열로도 온다(src/matching/duplicates.py
    의 transaction_date 처리와 같은 관용구). 없거나 형식이 깨졌으면 None —
    만료 여부를 추측하지 않는다."""
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
    expires_at = _parse_expires_at(claim_request.get("expires_at"))

    if status == "PENDING":
        if not claim_request.get("slack_dm_ts"):
            return ReminderAction.SEND_INITIAL
        if expires_at is None:
            return ReminderAction.SKIP
        return ReminderAction.EXPIRE if now >= expires_at else ReminderAction.SEND_REMINDER

    if status == "REMINDED":
        if expires_at is None:
            return ReminderAction.SKIP
        return ReminderAction.EXPIRE if now >= expires_at else ReminderAction.SKIP

    return ReminderAction.SKIP
