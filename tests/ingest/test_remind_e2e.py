"""schema-contract.md §10 — fixture 05의 재촉 루프를 시작점부터 끝까지 한 번 돌리는
종단 회귀.

fixture `05_reminder_expired.json`이 이 시나리오 전용이다 — *"미청구 방치 → 재촉 →
무응답 만료"*. 그 fixture의 `claim_requests` 문서를 **생성 직후 상태로 되감아**
넣고, 시각을 fixture 타임라인(`_fixture_note_timeline`)대로 앞으로 밀면서
`decide` → 발송 → 재예약 → 재촉 → 만료가 실제로 그 순서로 나는지 본다.
fixture가 담고 있는 건 루프가 **끝난 뒤**의 스냅샷(EXPIRED)이므로 그대로 쓰면
첫 판정이 곧장 SKIP이 된다 — test_draft_e2e.py가 fixture 02의 receipt를
"반영 직전"으로 되감아 쓰는 것과 같은 처리다.

돌리는 건 라우트(`routes.task_remind`)와 진짜 store 함수들이다. Firestore만
FakeClient/FakeTransaction으로 바꾸고(test_draft_apply.py 재사용) Slack·Cloud
Tasks·OIDC는 스텁이다 — 실제 Slack·GCP에 붙지 않는다.

fixture 05에는 `agent_drafts`가 없다. DM 문안은 코드가 지어내지 않으므로
(routes._requery_message) 여기서도 테스트가 문장을 만들지 않고 **fixture 02의
CLAIMANT draft payload를 그대로 빌려 쓴다.**
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.ingest import routes, store
from src.ingest.reminders import ReminderAction

from tests.ingest.test_draft_apply import FakeClient, FakeTransaction

FIXTURE_05 = "tests/fixtures/05_reminder_expired.json"
FIXTURE_02 = "tests/fixtures/02_parse_failure_requery.json"


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _ts(value: str) -> datetime:
    """fixture의 ISO 문자열을 Firestore가 돌려주는 형태(aware datetime)로."""
    return datetime.fromisoformat(value).astimezone(UTC)


def _doc(raw: dict) -> dict:
    """fixture 문서의 시각 필드를 datetime으로 바꾼다 — Firestore 읽기와 같은 모양."""
    doc = dict(raw)
    for field in ("slack_dm_ts",):
        doc.setdefault(field, None)
    for field in ("reminded_at", "expires_at", "created_at", "updated_at"):
        if isinstance(doc.get(field), str):
            doc[field] = _ts(doc[field])
    return doc


FIXTURE_05_DATA = _load(FIXTURE_05)
FIXTURE_02_DATA = _load(FIXTURE_02)

CLAIM_REQUESTS_05 = FIXTURE_05_DATA.get("claim_requests", [])
CLAIM_REQUESTS_02 = FIXTURE_02_DATA.get("claim_requests", [])
CLAIMANT_DRAFTS_02 = [d for d in FIXTURE_02_DATA.get("agent_drafts", []) if d.get("agent") == "CLAIMANT"]


# --- 가드: 대상이 0건이면 아래 회귀가 전부 조용히 통과한다 ---


def test_fixture_corpus_is_not_empty():
    """fixture 경로·파일명·필드가 바뀌면 이 스위트 전체가 아무것도 안 보고
    통과한다. 0건이면 여기서 먼저 실패해야 한다."""
    assert len(CLAIM_REQUESTS_05) == 1, (
        f"{FIXTURE_05}의 claim_requests가 {len(CLAIM_REQUESTS_05)}건이다 — 1건이어야 한다"
    )
    assert len(CLAIM_REQUESTS_02) == 1, (
        f"{FIXTURE_02}의 claim_requests가 {len(CLAIM_REQUESTS_02)}건이다 — 1건이어야 한다"
    )
    assert len(CLAIMANT_DRAFTS_02) == 1, (
        f"{FIXTURE_02}의 CLAIMANT draft가 {len(CLAIMANT_DRAFTS_02)}건이다 — DM 문안의 출처다"
    )
    assert FIXTURE_05_DATA.get("recipients"), f"{FIXTURE_05}에 recipients가 없다 — 보낼 곳이 없다"
    assert FIXTURE_05_DATA.get("receipts"), f"{FIXTURE_05}에 receipts가 없다"


def test_fixture_05_timeline_is_the_expected_three_transitions():
    """fixture 05의 타임라인이 이 스위트가 재현하려는 그 상태 기계인지 고정한다."""
    actions = [entry["action"] for entry in FIXTURE_05_DATA["audit_logs"]]
    assert actions == [
        "CLAIM_REQUEST_CREATED",
        "CLAIM_REQUEST_REMINDED",
        "CLAIM_REQUEST_EXPIRED",
    ]


# --- 시나리오 상수 (전부 fixture에서 읽는다) ---

CLAIM_REQUEST_05 = _doc(CLAIM_REQUESTS_05[0])
RECEIPT_05 = FIXTURE_05_DATA["receipts"][0]
RECIPIENT_05 = FIXTURE_05_DATA["recipients"][0]
REQUERY_MESSAGE = CLAIMANT_DRAFTS_02[0]["payload"]["requery_message"]

# 두 fixture의 receipts·recipients를 그대로 조회 테이블로 쓴다.
RECEIPTS = {
    r["receipt_id"]: r
    for data in (FIXTURE_05_DATA, FIXTURE_02_DATA)
    for r in data.get("receipts", [])
}
RECIPIENTS = {
    r["recipient_id"]: r
    for data in (FIXTURE_05_DATA, FIXTURE_02_DATA)
    for r in data.get("recipients", [])
}

CREATED_AT = CLAIM_REQUEST_05["created_at"]
REMINDED_AT = CLAIM_REQUEST_05["reminded_at"]
EXPIRES_AT = CLAIM_REQUEST_05["expires_at"]
# fixture 타임라인이 곧 이 시나리오의 REMINDER_DELAY_SECONDS다(운영값 1일).
INITIAL_DELAY = int((REMINDED_AT - CREATED_AT).total_seconds())


class _Clock:
    """routes가 부르는 datetime.now(UTC)를 테스트가 쥔다. 라우트는 now를 인자로
    받지 않으므로(큐가 부르는 진입점이다) 시각을 미는 유일한 방법이다."""

    def __init__(self, now: datetime):
        self.value = now

    def now(self, tz=None) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value = self.value + timedelta(seconds=seconds)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("REMINDER_DELAY_SECONDS", str(INITIAL_DELAY))
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})

    log = []
    client = FakeClient(log)
    monkeypatch.setattr(store, "get_client", lambda: client)
    monkeypatch.setattr(store, "_run_in_transaction", lambda fn: fn(FakeTransaction(log)))
    monkeypatch.setattr(store, "record_audit_log", lambda **kw: None)

    clock = _Clock(CREATED_AT)
    monkeypatch.setattr(routes, "datetime", clock)

    state = {
        "client": client,
        "clock": clock,
        "sent": [],
        "enqueued": [],
        "audit": [],
        "ts_seq": iter(f"17226828{n:02d}.000100" for n in range(10, 99)),
    }

    def fake_post(*, channel, text, thread_ts=None):
        state["sent"].append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return next(state["ts_seq"])

    monkeypatch.setattr(routes, "post_message", fake_post)
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: state["audit"].append(kw))
    monkeypatch.setattr(
        routes,
        "enqueue_remind",
        lambda _id, *, delay_seconds: state["enqueued"].append((_id, delay_seconds)),
    )
    # 영수증·수신자·draft 읽기는 이 루프의 소유가 아니다(각각 parsing·payouts·
    # settlements store) — fixture 문서를 그대로 돌려주는 스텁으로 둔다.
    monkeypatch.setattr(routes, "get_receipt", lambda _id: RECEIPTS.get(_id))
    monkeypatch.setattr(routes, "get_recipient", lambda _id: RECIPIENTS.get(_id))
    monkeypatch.setattr(
        routes,
        "get_agent_draft",
        lambda task_id: {"payload": {"needs_requery": True, "requery_message": REQUERY_MESSAGE}},
    )
    return state


def _seed_pending(client, overrides=None):
    """fixture 05의 claim_request를 **생성 직후**로 되감아 넣는다 — 아직 아무
    DM도 나가지 않았고(slack_dm_ts=None) 재촉도 없다(reminded_at=None).
    fixture에 담긴 EXPIRED 스냅샷은 이 루프가 끝난 뒤의 모습이다."""
    doc = dict(CLAIM_REQUEST_05)
    doc.update(
        {
            "slack_dm_ts": None,
            "reminded_at": None,
            "status": "PENDING",
            "updated_at": CREATED_AT,
        }
    )
    if overrides:
        doc.update(overrides)
    client.data["claim_requests"][doc["claim_request_id"]] = doc
    return doc


def _stored(client, claim_request_id=None):
    claim_request_id = claim_request_id or CLAIM_REQUEST_05["claim_request_id"]
    return client.data["claim_requests"][claim_request_id]


def _remind(claim_request_id=None):
    return routes.task_remind(
        {"claim_request_id": claim_request_id or CLAIM_REQUEST_05["claim_request_id"]}
    )


def _actions(state):
    return [entry["action"] for entry in state["audit"]]


# --- 상태 기계 종단 ---


def test_fixture_05_full_loop_pending_to_reminded_to_expired(env):
    """생성 → 최초 DM → (하루) → 재촉 DM → (하루) → 만료. fixture 타임라인 그대로.

    각 단계에서 확인하는 건 (1) 판정 (2) 문서에 남는 흔적 (3) 다음 깨어남 예약이다.
    """
    client, clock = env["client"], env["clock"]
    _seed_pending(client)

    # --- t0: 생성 직후 첫 깨어남 → 최초 DM ---
    result = _remind()
    assert result["action"] == ReminderAction.SEND_INITIAL.value
    assert len(env["sent"]) == 1
    first_dm = env["sent"][0]
    # fixture 05의 receipt에는 slack_channel_id·slack_message_ts가 없다 → DM.
    assert first_dm["channel"] == RECIPIENT_05["slack_user_id"]
    assert first_dm["text"] == REQUERY_MESSAGE
    assert first_dm["thread_ts"] is None

    doc = _stored(client)
    assert doc["slack_dm_ts"] == result["slack_ts"], "최초 발송 뒤 slack_dm_ts가 기록돼야 한다"
    # 최초 발송은 아직 재촉이 아니다.
    assert doc["status"] == "PENDING"
    assert doc["reminded_at"] is None
    assert env["enqueued"] == [(doc["claim_request_id"], INITIAL_DELAY)]

    # --- t0 + REMINDER_DELAY_SECONDS: 재촉 ---
    clock.advance(INITIAL_DELAY)
    assert clock.value == REMINDED_AT, "fixture 타임라인의 재촉 시각과 어긋난다"

    result = _remind()
    assert result["action"] == ReminderAction.SEND_REMINDER.value
    assert len(env["sent"]) == 2
    # 재촉은 최초 DM 아래 붙는다 — 사람이 무엇에 대한 재촉인지 알아야 한다.
    assert env["sent"][1]["thread_ts"] == doc["slack_dm_ts"]

    initial_dm_ts = doc["slack_dm_ts"]
    doc = _stored(client)
    assert doc["status"] == "REMINDED"
    assert doc["reminded_at"] == REMINDED_AT
    # 스레드 루트는 재촉 응답 ts로 덮이지 않는다.
    assert doc["slack_dm_ts"] == initial_dm_ts != result["slack_ts"]

    # 다음 깨어남은 expires_at을 덮어야 한다 — 이르면 SKIP으로 빠져 영구 정체다.
    _, delay = env["enqueued"][1]
    assert clock.value + timedelta(seconds=delay) >= EXPIRES_AT

    # --- expires_at: 만료 ---
    clock.advance(delay)
    assert clock.value >= EXPIRES_AT

    result = _remind()
    assert result["action"] == ReminderAction.EXPIRE.value
    assert _stored(client)["status"] == "EXPIRED"

    # **DM은 정확히 2번이다** — 최초 1 + 재촉 1. 3번이면 재촉이 1회가 아니다.
    assert len(env["sent"]) == 2
    # 만료는 재예약하지 않는다 — 루프가 여기서 끝난다.
    assert len(env["enqueued"]) == 2
    assert "CLAIM_REQUEST_EXPIRED" in _actions(env)

    # 문서는 처음부터 끝까지 한 건이다.
    assert len(client.data["claim_requests"]) == 1


def test_expired_claim_request_is_terminal(env):
    """만료된 뒤 태스크가 한 번 더 도착해도(큐 재전달·수동 재개) 아무 일도
    없어야 한다 — EXPIRED는 종착이다."""
    client, clock = env["client"], env["clock"]
    _seed_pending(client, {"status": "EXPIRED"})
    clock.value = EXPIRES_AT + timedelta(hours=1)

    result = _remind()
    assert result == {"status": "ok", "action": "SKIP"}
    assert env["sent"] == []
    assert env["enqueued"] == []


def test_responded_before_reminder_stops_the_loop(env):
    """사람이 답하면 재촉은 나가지 않는다 — 만료 시각 전이어도 SKIP이다."""
    client, clock = env["client"], env["clock"]
    _seed_pending(client, {"status": "RESPONDED", "slack_dm_ts": "1722682810.000100"})
    clock.value = REMINDED_AT

    result = _remind()
    assert result == {"status": "ok", "action": "SKIP"}
    assert env["sent"] == []


def test_fixture_02_claim_request_resumes_at_reminder(env):
    """fixture 02의 claim_request는 최초 DM이 이미 나간 상태(slack_dm_ts 있음,
    status=PENDING)다. 루프가 중간부터 이어져 재촉 → 만료로 끝나는지 본다."""
    client, clock = env["client"], env["clock"]
    doc = _doc(CLAIM_REQUESTS_02[0])
    client.data["claim_requests"][doc["claim_request_id"]] = doc
    claim_request_id = doc["claim_request_id"]
    expires_at = doc["expires_at"]
    clock.value = doc["created_at"] + timedelta(hours=1)

    result = _remind(claim_request_id)
    assert result["action"] == ReminderAction.SEND_REMINDER.value
    stored = _stored(client, claim_request_id)
    assert stored["status"] == "REMINDED"
    assert stored["reminded_at"] == clock.value
    # 재촉은 fixture가 들고 있던 최초 DM ts 아래 붙는다.
    assert env["sent"][0]["thread_ts"] == CLAIM_REQUESTS_02[0]["slack_dm_ts"]

    clock.value = expires_at
    result = _remind(claim_request_id)
    assert result["action"] == ReminderAction.EXPIRE.value
    assert _stored(client, claim_request_id)["status"] == "EXPIRED"
    assert len(env["sent"]) == 1


# --- 크래시 창: 발송 성공 + 표시 실패 ---


def test_crash_between_send_and_record_duplicates_only_the_dm(env, monkeypatch):
    """**이 테스트가 단언하는 것은 "중복이 없다"가 아니다.**

    설계상 순서는 "CAS → 발송 → 표시"다(store.claim_send_slot 주석). 미리
    표시해두면 발송 실패가 조용한 유실이 되므로 그쪽을 택했고, 그 대가로
    "Slack 발송은 성공했는데 record_sent 직전에 태스크가 죽는" 창이 남는다.
    Cloud Tasks가 그 태스크를 재전달하면 **같은 DM이 한 번 더 나간다 — 그게
    이 설계가 감수하기로 한 비용이고, 안 나가면 오히려 설계와 다르다.**

    여기서 고정하는 건 **중복의 범위**다:
      - 중복은 DM 한 건으로 끝난다(문서가 두 개로 늘거나 상태가 앞서가지 않는다)
      - `claim_requests` 문서는 여전히 한 건이다
      - 상태 필드가 모순되지 않는다(status/slack_dm_ts/reminded_at)
      - 이어지는 재촉은 여전히 1회다 — 크래시가 루프를 두 갈래로 늘리지 않는다
    """
    client, clock = env["client"], env["clock"]
    _seed_pending(client)
    real_record_sent = store.record_sent

    def crash_after_send(*args, **kwargs):
        raise RuntimeError("task killed after Slack send, before record_sent")

    # 1) 최초 발송이 Slack까지 성공한 뒤 표시 직전에 죽는다.
    monkeypatch.setattr(routes, "record_sent", crash_after_send)
    with pytest.raises(RuntimeError):
        _remind()

    assert len(env["sent"]) == 1, "DM은 이미 나갔다 — 크래시는 그 뒤였다"
    doc = _stored(client)
    assert doc["slack_dm_ts"] is None, "표시 전에 죽었으므로 문서에는 흔적이 없다"
    assert doc["status"] == "PENDING"
    assert env["enqueued"] == [], "재예약도 표시 뒤라 아직 없다"

    # 2) Cloud Tasks가 같은 태스크를 재전달한다.
    monkeypatch.setattr(routes, "record_sent", real_record_sent)
    clock.advance(30)
    result = _remind()

    # 3) 두 번째 DM은 실제로 나간다 — 이게 감수하기로 한 대가다.
    assert result["action"] == ReminderAction.SEND_INITIAL.value
    assert len(env["sent"]) == 2
    assert env["sent"][1]["text"] == env["sent"][0]["text"]

    # 그런데 문서는 한 건이고 상태는 모순이 없다.
    assert len(client.data["claim_requests"]) == 1
    doc = _stored(client)
    assert doc["claim_request_id"] == CLAIM_REQUEST_05["claim_request_id"]
    assert doc["status"] == "PENDING", "최초 발송은 아직 재촉이 아니다"
    assert doc["slack_dm_ts"] == result["slack_ts"]
    assert doc["reminded_at"] is None
    assert env["enqueued"] == [(doc["claim_request_id"], INITIAL_DELAY)]

    # 4) 이후 루프는 갈라지지 않는다 — 재촉은 여전히 1회, 만료도 1회.
    clock.advance(INITIAL_DELAY)
    assert _remind()["action"] == ReminderAction.SEND_REMINDER.value
    assert len(env["sent"]) == 3  # 중복 1 + 최초 1 + 재촉 1 — 중복분은 최초 DM에서만 늘었다

    _, delay = env["enqueued"][1]
    clock.advance(delay)
    assert _remind()["action"] == ReminderAction.EXPIRE.value
    assert _stored(client)["status"] == "EXPIRED"
    assert len(env["sent"]) == 3
    assert len(client.data["claim_requests"]) == 1


def test_redelivery_after_record_does_not_repeat_the_initial_dm(env):
    """위 테스트의 대조군 — 표시(`record_sent`)까지 끝난 뒤의 재전달은 **최초 DM을
    다시 내지 않는다.** slack_dm_ts가 남아 있어 판정이 SEND_INITIAL에서 벗어나기
    때문이다. 즉 중복 창은 "발송과 표시 사이" 딱 거기 하나다.

    (이 재전달은 예정보다 이른 재촉으로 소비된다 — 상태 기계상 다음 칸이고,
    그 뒤로도 재촉은 늘지 않는다.)"""
    client, clock = env["client"], env["clock"]
    _seed_pending(client)

    first = _remind()
    assert len(env["sent"]) == 1

    clock.advance(1)
    result = _remind()
    assert result["action"] != ReminderAction.SEND_INITIAL.value
    doc = _stored(client)
    # 스레드 루트는 최초 DM 그대로 — 두 번째 최초 DM은 없었다.
    assert doc["slack_dm_ts"] == first["slack_ts"]
    assert doc["status"] == "REMINDED"
    assert len(client.data["claim_requests"]) == 1
