"""재촉 루프의 Firestore 창구 — claim_requests 조회와 발송 CAS.

**발송의 원자성을 지키는 유일한 지점이다.** Cloud Tasks가 태스크를 중복 전달하면
같은 DM이 두 번 나간다. 그래서 발송 직전에 `claim_send_slot`이 트랜잭션 안에서
현재 문서를 다시 읽어 판정이 여전히 유효한지 확인한다.

**순서는 "발송 먼저 → 표시"다.** `claim_send_slot`은 예약 표시를 하지 않는다 —
미리 표시해두면 Slack 발송이 실패했을 때 아무도 재시도하지 않는 조용한 유실이 된다.
대신 발송이 성공한 뒤 `record_sent`가 기록한다.

test_store.py·test_draft_apply.py와 같은 한계를 갖는다: FakeTransaction은 락·ABORTED·
재실행이 없어 즉시 반영된다. 검증하는 건 (1) 전이 로직 (2) 구조 — 읽기가
`transaction=`을 받고 모든 쓰기보다 앞에 오는지. 진짜 동시성은 에뮬레이터 몫이다.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.ingest import store
from src.ingest.reminders import ReminderAction
from tests.ingest.test_draft_apply import FakeClient, FakeTransaction

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def fake(monkeypatch):
    log = []
    client = FakeClient(log)
    monkeypatch.setattr(store, "get_client", lambda: client)
    monkeypatch.setattr(store, "_run_in_transaction", lambda fn: fn(FakeTransaction(log)))
    client.log = log
    return client


def _seed(fake, claim_request_id="crq_1", **overrides):
    doc = {
        "claim_request_id": claim_request_id,
        "recipient_id": "rcp_1",
        "receipt_id": "rct_1",
        "reason": "AMOUNT_MISMATCH",
        "slack_dm_ts": None,
        "reminded_at": None,
        "expires_at": NOW + timedelta(hours=12),
        "status": "PENDING",
        "created_at": NOW,
        "updated_at": NOW,
    }
    doc.update(overrides)
    fake.data["claim_requests"][claim_request_id] = doc
    return doc


# --- get_claim_request ---


def test_get_claim_request_returns_document(fake):
    _seed(fake)
    assert store.get_claim_request("crq_1")["recipient_id"] == "rcp_1"


def test_get_missing_claim_request_returns_none(fake):
    assert store.get_claim_request("crq_nope") is None


# --- claim_send_slot ---


def test_claim_send_slot_grants_when_verdict_still_valid(fake):
    _seed(fake)
    assert store.claim_send_slot("crq_1", expected_action=ReminderAction.SEND_INITIAL, now=NOW) is True


def test_claim_send_slot_denies_when_already_sent(fake):
    """이미 slack_dm_ts가 있으면 SEND_INITIAL 판정은 더 이상 유효하지 않다 —
    태스크 중복 전달로 같은 DM이 두 번 나가는 걸 여기서 막는다."""
    _seed(fake, slack_dm_ts="1755500000.000100")
    assert store.claim_send_slot("crq_1", expected_action=ReminderAction.SEND_INITIAL, now=NOW) is False


def test_claim_send_slot_denies_when_human_responded(fake):
    _seed(fake, status="RESPONDED", slack_dm_ts="1755500000.000100")
    assert store.claim_send_slot("crq_1", expected_action=ReminderAction.SEND_REMINDER, now=NOW) is False


def test_claim_send_slot_denies_for_missing_document(fake):
    assert store.claim_send_slot("crq_nope", expected_action=ReminderAction.SEND_INITIAL, now=NOW) is False


def test_claim_send_slot_does_not_reserve(fake):
    """예약 표시를 미리 하면 Slack 발송 실패가 조용한 유실이 된다. 쓰기가 없어야 한다."""
    before = dict(_seed(fake))
    store.claim_send_slot("crq_1", expected_action=ReminderAction.SEND_INITIAL, now=NOW)
    assert fake.data["claim_requests"]["crq_1"] == before
    assert not [entry for entry in fake.log if entry[0] in ("set", "update")]


def test_claim_send_slot_reads_inside_transaction(fake):
    """구조 테스트 — 트랜잭션 밖에서 읽으면 락이 안 걸려 CAS가 무의미해진다."""
    _seed(fake)
    store.claim_send_slot("crq_1", expected_action=ReminderAction.SEND_INITIAL, now=NOW)
    assert fake.log[0] == ("get", "claim_requests/crq_1", True)


# --- record_sent ---


def test_record_sent_initial_fills_slack_dm_ts(fake):
    _seed(fake)
    store.record_sent("crq_1", slack_ts="1755500000.000100", action=ReminderAction.SEND_INITIAL, now=NOW)
    doc = fake.data["claim_requests"]["crq_1"]
    assert doc["slack_dm_ts"] == "1755500000.000100"
    # 최초 발송은 아직 재촉이 아니다 — 상태는 PENDING에 머문다.
    assert doc["status"] == "PENDING"
    assert doc["reminded_at"] is None
    assert doc["updated_at"] == NOW


def test_record_sent_reminder_marks_reminded(fake):
    _seed(fake, slack_dm_ts="1755500000.000100")
    store.record_sent("crq_1", slack_ts="1755500001.000200", action=ReminderAction.SEND_REMINDER, now=NOW)
    doc = fake.data["claim_requests"]["crq_1"]
    assert doc["status"] == "REMINDED"
    assert doc["reminded_at"] == NOW
    # 최초 DM의 thread_ts를 재촉 응답 ts로 덮으면 스레드가 끊긴다.
    assert doc["slack_dm_ts"] == "1755500000.000100"


def test_record_sent_initial_does_not_overwrite_existing_root_ts(fake):
    """동시 워커 둘이 모두 슬롯을 얻는 잔여 창. 두 번째 기록이 스레드 루트 ts를
    나중 값으로 밀면 이후 재촉이 엉뚱한 스레드에 달린다."""
    _seed(fake, slack_dm_ts="1755500000.000100")
    store.record_sent("crq_1", slack_ts="1755500009.000900", action=ReminderAction.SEND_INITIAL, now=NOW)
    assert fake.data["claim_requests"]["crq_1"]["slack_dm_ts"] == "1755500000.000100"


def test_record_sent_does_not_overwrite_responded(fake):
    """발송과 기록 사이에 사람이 답했으면 그 응답이 이긴다."""
    _seed(fake, status="RESPONDED", slack_dm_ts="1755500000.000100")
    store.record_sent("crq_1", slack_ts="1755500001.000200", action=ReminderAction.SEND_REMINDER, now=NOW)
    assert fake.data["claim_requests"]["crq_1"]["status"] == "RESPONDED"


def test_record_sent_ignores_missing_document(fake):
    store.record_sent("crq_nope", slack_ts="1.1", action=ReminderAction.SEND_INITIAL, now=NOW)
    assert fake.data["claim_requests"] == {}


def test_record_sent_reads_before_any_write(fake):
    _seed(fake)
    store.record_sent("crq_1", slack_ts="1755500000.000100", action=ReminderAction.SEND_INITIAL, now=NOW)
    kinds = [entry[0] for entry in fake.log]
    assert kinds[0] == "get", f"첫 연산이 읽기가 아니다: {fake.log}"
    assert fake.log[0][2] is True, "읽기가 트랜잭션 밖에서 일어났다"
    write_at = kinds.index("update")
    assert "get" not in kinds[write_at:], f"쓰기 뒤에 읽기가 있다: {fake.log}"


# --- mark_expired ---


def test_mark_expired_sets_expired(fake):
    _seed(fake, status="REMINDED", slack_dm_ts="1755500000.000100")
    store.mark_expired("crq_1", now=NOW)
    doc = fake.data["claim_requests"]["crq_1"]
    assert doc["status"] == "EXPIRED"
    assert doc["updated_at"] == NOW


def test_mark_expired_does_not_overwrite_responded(fake):
    """사람이 방금 누른 응답을 만료 태스크가 덮으면 응답이 사라진다."""
    _seed(fake, status="RESPONDED", slack_dm_ts="1755500000.000100")
    store.mark_expired("crq_1", now=NOW)
    assert fake.data["claim_requests"]["crq_1"]["status"] == "RESPONDED"


def test_mark_expired_ignores_missing_document(fake):
    store.mark_expired("crq_nope", now=NOW)
    assert fake.data["claim_requests"] == {}


def test_mark_expired_reads_before_any_write(fake):
    _seed(fake)
    store.mark_expired("crq_1", now=NOW)
    kinds = [entry[0] for entry in fake.log]
    assert kinds[0] == "get", f"첫 연산이 읽기가 아니다: {fake.log}"
    assert fake.log[0][2] is True, "읽기가 트랜잭션 밖에서 일어났다"
    write_at = kinds.index("update")
    assert "get" not in kinds[write_at:], f"쓰기 뒤에 읽기가 있다: {fake.log}"


# --- 계약 ---


@pytest.mark.parametrize(
    "apply_write",
    [
        pytest.param(
            lambda: store.record_sent(
                "crq_1", slack_ts="1755500000.000100", action=ReminderAction.SEND_INITIAL, now=NOW
            ),
            id="record_sent_initial",
        ),
        pytest.param(
            lambda: store.record_sent(
                "crq_1", slack_ts="1755500001.000200", action=ReminderAction.SEND_REMINDER, now=NOW
            ),
            id="record_sent_reminder",
        ),
        pytest.param(lambda: store.mark_expired("crq_1", now=NOW), id="mark_expired"),
    ],
)
def test_updated_document_validates_against_contract(fake, apply_write):
    """schema-contract.md §3 — 갱신된 문서가 ClaimRequest로 검증돼야 한다."""
    from src.schemas.models import ClaimRequest

    _seed(fake, slack_dm_ts="1755500000.000100")
    apply_write()
    ClaimRequest.model_validate(fake.data["claim_requests"]["crq_1"])


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: store.claim_send_slot("crq_1", expected_action=ReminderAction.SEND_INITIAL, now=NOW),
            id="claim_send_slot",
        ),
        pytest.param(
            lambda: store.record_sent(
                "crq_1", slack_ts="1.1", action=ReminderAction.SEND_INITIAL, now=NOW
            ),
            id="record_sent",
        ),
        pytest.param(lambda: store.mark_expired("crq_1", now=NOW), id="mark_expired"),
    ],
)
def test_transaction_failure_raises_domain_error(fake, monkeypatch, call):
    """SDK가 재시도를 소진하면 ValueError를 던진다. 라우트가 SDK 내부 사정을
    알지 않도록 도메인 예외로 바꿔 올린다."""

    def boom(fn):
        raise ValueError("Failed to commit transaction in 5 attempts.")

    monkeypatch.setattr(store, "_run_in_transaction", boom)
    with pytest.raises(store.ReceiptStoreUnavailable):
        call()
