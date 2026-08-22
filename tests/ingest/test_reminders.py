"""task-2-brief.md 판정 표 — src/ingest/reminders.py의 순수 판정 함수."""

from datetime import UTC, datetime

from src.ingest.reminders import ReminderAction, decide

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
BEFORE_EXPIRES = datetime(2026, 8, 22, 11, 0, 0, tzinfo=UTC)
AFTER_EXPIRES = datetime(2026, 8, 22, 13, 0, 0, tzinfo=UTC)


def test_pending_without_dm_sends_initial():
    claim_request = {"status": "PENDING", "expires_at": EXPIRES_AT}
    assert decide(claim_request, now=NOW) == ReminderAction.SEND_INITIAL


def test_pending_without_dm_after_expiry_expires():
    """최초 DM이 못 나간 채(문안 부재·대상 부재·Slack 논리 오류) 만료 시각을 넘긴
    건은 EXPIRE다. SEND_INITIAL을 다시 내면 같은 실패를 무한 반복하고, 라우트가
    그 경로들에 걸어둔 만료 태스크가 EXPIRED에 도달하지 못한다."""
    claim_request = {"status": "PENDING", "expires_at": BEFORE_EXPIRES}
    assert decide(claim_request, now=NOW) == ReminderAction.EXPIRE


def test_pending_without_dm_boundary_now_equals_expires_at_expires():
    claim_request = {"status": "PENDING", "expires_at": NOW}
    assert decide(claim_request, now=NOW) == ReminderAction.EXPIRE


def test_pending_without_dm_and_without_expires_at_still_sends_initial():
    """만료 시각을 모르면 만료로 추측하지 않는다 — 최초 발송은 그대로 시도한다."""
    claim_request = {"status": "PENDING"}
    assert decide(claim_request, now=NOW) == ReminderAction.SEND_INITIAL


def test_pending_with_dm_before_expiry_sends_reminder():
    claim_request = {"status": "PENDING", "slack_dm_ts": "123.456", "expires_at": AFTER_EXPIRES}
    assert decide(claim_request, now=NOW) == ReminderAction.SEND_REMINDER


def test_pending_with_dm_after_expiry_expires():
    claim_request = {"status": "PENDING", "slack_dm_ts": "123.456", "expires_at": BEFORE_EXPIRES}
    assert decide(claim_request, now=NOW) == ReminderAction.EXPIRE


def test_reminded_after_expiry_expires():
    claim_request = {"status": "REMINDED", "expires_at": BEFORE_EXPIRES}
    assert decide(claim_request, now=NOW) == ReminderAction.EXPIRE


def test_reminded_before_expiry_skips():
    claim_request = {"status": "REMINDED", "expires_at": AFTER_EXPIRES}
    assert decide(claim_request, now=NOW) == ReminderAction.SKIP


def test_responded_skips():
    claim_request = {"status": "RESPONDED", "expires_at": AFTER_EXPIRES}
    assert decide(claim_request, now=NOW) == ReminderAction.SKIP


def test_expired_skips():
    claim_request = {"status": "EXPIRED", "expires_at": AFTER_EXPIRES}
    assert decide(claim_request, now=NOW) == ReminderAction.SKIP


def test_boundary_now_equals_expires_at_expires():
    """now == expires_at은 EXPIRE — 경계는 만료 쪽으로 붙는다."""
    claim_request = {"status": "REMINDED", "expires_at": NOW}
    assert decide(claim_request, now=NOW) == ReminderAction.EXPIRE


def test_pending_with_dm_boundary_now_equals_expires_at_expires():
    claim_request = {"status": "PENDING", "slack_dm_ts": "123.456", "expires_at": NOW}
    assert decide(claim_request, now=NOW) == ReminderAction.EXPIRE


def test_expires_at_as_iso_string_with_z_suffix():
    """Firestore에서 온 문서가 ISO 문자열로 오는 경우도 흡수한다."""
    claim_request = {"status": "REMINDED", "expires_at": "2026-08-22T11:00:00Z"}
    assert decide(claim_request, now=NOW) == ReminderAction.EXPIRE


def test_missing_expires_at_skips():
    """expires_at이 없으면 만료 여부를 판단할 수 없다 — 추측해서 만료 처리하지 않는다."""
    claim_request = {"status": "REMINDED"}
    assert decide(claim_request, now=NOW) == ReminderAction.SKIP


def test_unknown_status_skips():
    """모르는 status는 SKIP이 안전하다 — 추측해서 DM을 보내지 않는다."""
    claim_request = {"status": "SOME_FUTURE_STATUS", "expires_at": EXPIRES_AT}
    assert decide(claim_request, now=NOW) == ReminderAction.SKIP
