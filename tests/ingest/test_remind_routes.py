"""schema-contract.md §10 — POST /tasks/remind.

라우트는 조립만 한다. 판정(`reminders.decide`)·CAS(`store.claim_send_slot`)·
발송(`slack_client.post_message`)의 내용은 각자의 테스트 몫이고, 여기서는
**HTTP 코드가 큐의 재시도 손잡이로 맞게 붙었는지**와 조립 순서를 본다:
transient는 503(재시도), permanent는 200(재시도해도 같다), 문안 부재는 200.

test_draft_routes.py와 같은 형태 — TestClient 없이 핸들러를 직접 부르고
모듈 레벨 이름을 monkeypatch한다.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from src.ingest import routes, store
from src.ingest.reminders import ReminderAction
from src.ingest.slack_client import SlackSendPermanent, SlackSendTransient
from src.ingest.store import ReceiptStoreUnavailable

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _claim_request(**overrides):
    doc = {
        "claim_request_id": "crq_1",
        "recipient_id": "rcp_1",
        "receipt_id": "rct_1",
        "reason": "AMOUNT_MISMATCH",
        "slack_dm_ts": None,
        "reminded_at": None,
        # **실제 시각 기준이다.** 라우트의 now는 진짜 datetime.now(UTC)라서
        # NOW(고정 상수) 기준으로 잡으면 시간이 흐를수록 만료된 문서가 되고,
        # 그러면 decide()가 SEND_INITIAL이 아니라 EXPIRE를 낸다.
        "expires_at": datetime.now(UTC) + timedelta(hours=12),
        "status": "PENDING",
        "created_at": NOW,
        "updated_at": NOW,
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def env(monkeypatch):
    """라우트가 붙잡는 협력자를 전부 스텁으로 바꾼다. 실제 Firestore·Slack·
    Cloud Tasks에 붙지 않는다."""
    monkeypatch.setenv("REMINDER_DELAY_SECONDS", "20")
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})

    state = {
        "claim_request": _claim_request(),
        "receipt": {"slack_channel_id": None, "slack_message_ts": None},
        "recipient": {"slack_user_id": "U_CLAIMANT"},
        "draft": {"payload": {"needs_requery": True, "requery_message": "영수증 금액을 확인해 주세요"}},
        "slot": True,
        "audit": [],
        "sent": [],
        "recorded": [],
        "expired": [],
        "enqueued": [],
        "post_result": "1755500000.000100",
    }

    def fake_post(*, channel, text, thread_ts=None):
        state["sent"].append({"channel": channel, "text": text, "thread_ts": thread_ts})
        result = state["post_result"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: state["audit"].append(kw))
    monkeypatch.setattr(routes, "get_claim_request", lambda _id: state["claim_request"])
    monkeypatch.setattr(routes, "get_receipt", lambda _id: state["receipt"])
    monkeypatch.setattr(routes, "get_recipient", lambda _id: state["recipient"])
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: state["draft"])
    monkeypatch.setattr(
        routes, "claim_send_slot", lambda _id, *, expected_action, now: state["slot"]
    )
    monkeypatch.setattr(
        routes,
        "record_sent",
        lambda _id, *, slack_ts, action, now: state["recorded"].append((_id, slack_ts, action)),
    )
    monkeypatch.setattr(
        routes, "mark_expired", lambda _id, *, now: state["expired"].append(_id)
    )
    monkeypatch.setattr(
        routes,
        "enqueue_remind",
        lambda _id, *, delay_seconds: state["enqueued"].append((_id, delay_seconds)),
    )
    monkeypatch.setattr(routes, "post_message", fake_post)
    return state


def _actions(state):
    return [entry["action"] for entry in state["audit"]]


def _only_enqueued_delay(state):
    """발송 없이 끝나는 경로가 붙인 만료 태스크의 지연. 정확히 1건이어야 한다."""
    assert len(state["enqueued"]) == 1, (
        f"재예약이 {len(state['enqueued'])}건이다 — 0건이면 claim_request가 PENDING에 영구 고착한다"
    )
    claim_request_id, delay = state["enqueued"][0]
    assert claim_request_id == "crq_1"
    return delay


# --- 입력 검증 ---


def test_oidc_verified_first(monkeypatch):
    """OIDC 없으면 401 — claim_request_id 검사보다 먼저 걸려야 한다."""
    from src.guards.oidc import verify_oidc as real_verify_oidc

    monkeypatch.setattr(routes, "verify_oidc", real_verify_oidc)
    with pytest.raises(HTTPException) as exc:
        routes.task_remind({}, authorization="")
    assert exc.value.status_code == 401


def test_missing_claim_request_id_returns_400(env):
    with pytest.raises(HTTPException) as exc:
        routes.task_remind({})
    assert exc.value.status_code == 400


def test_unknown_claim_request_returns_404(env, monkeypatch):
    monkeypatch.setattr(routes, "get_claim_request", lambda _id: None)
    with pytest.raises(HTTPException) as exc:
        routes.task_remind({"claim_request_id": "crq_nope"})
    assert exc.value.status_code == 404


# --- 판정 분기 ---


def test_skip_returns_200_without_sending(env):
    env["claim_request"] = _claim_request(status="RESPONDED", slack_dm_ts="1.1")
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result == {"status": "ok", "action": "SKIP"}
    assert env["sent"] == []
    assert env["enqueued"] == []


def test_expire_marks_and_does_not_reschedule(env):
    env["claim_request"] = _claim_request(
        status="REMINDED", slack_dm_ts="1.1", expires_at=NOW - timedelta(hours=1)
    )
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result == {"status": "ok", "action": "EXPIRE"}
    assert env["expired"] == ["crq_1"]
    assert env["sent"] == []
    assert env["enqueued"] == []
    assert "CLAIM_REQUEST_EXPIRED" in _actions(env)


# --- 문안 ---


def test_missing_draft_does_not_send(env):
    """문안은 코드가 지어내지 않는다. draft가 아예 없으면 발송하지 않는다."""
    env["draft"] = None
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result == {"status": "ignored", "reason": "no_message"}
    assert env["sent"] == []
    assert _actions(env) == ["CLAIM_REQUEST_NO_MESSAGE"]
    # 문안이 없어도 건은 닫혀야 한다 — 만료 태스크가 붙는다.
    assert _only_enqueued_delay(env) == pytest.approx(12 * 3600, abs=2)


def test_no_message_schedules_expiry_even_if_the_audit_sink_is_down(env, monkeypatch):
    """생명선은 감사 로그보다 먼저 붙는다 — 싱크가 죽어 500이 나도 만료 태스크는 남는다."""
    env["draft"] = None
    monkeypatch.setattr(
        routes, "record_audit_log", lambda **kw: (_ for _ in ()).throw(RuntimeError("audit sink down"))
    )
    with pytest.raises(RuntimeError):
        routes.task_remind({"claim_request_id": "crq_1"})
    assert _only_enqueued_delay(env) == pytest.approx(12 * 3600, abs=2)


@pytest.mark.parametrize("message", [None, "", "   ", 123, {"text": "x"}])
def test_blank_or_non_string_message_does_not_send(env, message):
    env["draft"] = {"payload": {"needs_requery": True, "requery_message": message}}
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result == {"status": "ignored", "reason": "no_message"}
    assert env["sent"] == []
    assert _actions(env) == ["CLAIM_REQUEST_NO_MESSAGE"]


def test_non_dict_payload_does_not_500(env):
    env["draft"] = {"payload": "not-a-dict"}
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result == {"status": "ignored", "reason": "no_message"}


def test_draft_read_uses_claimant_task_id(env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        routes, "get_agent_draft", lambda task_id: calls.append(task_id) or env["draft"]
    )
    routes.task_remind({"claim_request_id": "crq_1"})
    assert calls == ["CLAIMANT:rct_1"]


# --- 대상 결정 ---


def test_sends_dm_when_receipt_has_no_slack_thread(env):
    routes.task_remind({"claim_request_id": "crq_1"})
    assert env["sent"] == [
        {
            "channel": "U_CLAIMANT",
            "text": "영수증 금액을 확인해 주세요",
            "thread_ts": None,
        }
    ]


def test_sends_thread_reply_when_receipt_has_both_slack_fields(env):
    env["receipt"] = {"slack_channel_id": "C_TEAM", "slack_message_ts": "1755400000.000100"}
    routes.task_remind({"claim_request_id": "crq_1"})
    assert env["sent"][0]["channel"] == "C_TEAM"
    assert env["sent"][0]["thread_ts"] == "1755400000.000100"


@pytest.mark.parametrize(
    "receipt",
    [
        {"slack_channel_id": "C_TEAM", "slack_message_ts": None},
        {"slack_channel_id": None, "slack_message_ts": "1755400000.000100"},
    ],
)
def test_half_filled_slack_fields_fall_back_to_dm(env, receipt):
    """계약 §2 — 한쪽만 있는 상태는 만들지 않는다. 그래도 들어왔다면 스레드로
    보지 않는다: channel 없이 thread_ts만 있으면 발송 자체가 불가능하다."""
    env["receipt"] = receipt
    routes.task_remind({"claim_request_id": "crq_1"})
    assert env["sent"][0]["channel"] == "U_CLAIMANT"
    assert env["sent"][0]["thread_ts"] is None


def test_reminder_replies_under_initial_dm(env):
    """최초 DM의 ts가 스레드 루트다(store.record_sent 주석) — 재촉은 그 아래 붙는다."""
    env["claim_request"] = _claim_request(
        slack_dm_ts="1755500000.000100", expires_at=datetime.now(UTC) + timedelta(hours=12)
    )
    routes.task_remind({"claim_request_id": "crq_1"})
    assert env["sent"][0]["thread_ts"] == "1755500000.000100"


def test_no_slack_target_does_not_send(env):
    env["recipient"] = {"slack_user_id": None}
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result == {"status": "ignored", "reason": "no_target"}
    assert env["sent"] == []
    assert _actions(env) == ["CLAIM_REQUEST_NO_TARGET"]
    # 보낼 곳이 없어도 건은 닫혀야 한다 — 만료 태스크가 붙는다.
    assert _only_enqueued_delay(env) == pytest.approx(12 * 3600, abs=2)


def test_unknown_recipient_does_not_send(env):
    env["recipient"] = None
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result == {"status": "ignored", "reason": "no_target"}
    assert env["sent"] == []


# --- CAS ---


def test_denied_slot_does_not_send(env):
    """태스크 중복 전달 — CAS가 거절하면 같은 DM이 두 번 나가면 안 된다."""
    env["slot"] = False
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result == {"status": "ignored", "reason": "slot_taken"}
    assert env["sent"] == []
    assert env["recorded"] == []
    assert env["enqueued"] == []


def test_slot_is_claimed_with_the_decided_action(env, monkeypatch):
    captured = {}

    def fake_slot(claim_request_id, *, expected_action, now):
        captured["expected_action"] = expected_action
        return True

    monkeypatch.setattr(routes, "claim_send_slot", fake_slot)
    routes.task_remind({"claim_request_id": "crq_1"})
    assert captured["expected_action"] == ReminderAction.SEND_INITIAL


def test_slot_is_claimed_before_sending(env, monkeypatch):
    order = []
    monkeypatch.setattr(
        routes, "claim_send_slot", lambda _id, *, expected_action, now: order.append("slot") or True
    )
    monkeypatch.setattr(
        routes,
        "post_message",
        lambda **kw: order.append("send") or "1755500000.000100",
    )
    monkeypatch.setattr(
        routes, "record_sent", lambda _id, **kw: order.append("record")
    )
    routes.task_remind({"claim_request_id": "crq_1"})
    assert order == ["slot", "send", "record"]


# --- 발송 실패 ---


def test_transient_slack_failure_returns_503(env):
    """503이어야 Cloud Tasks가 재시도한다."""
    env["post_result"] = SlackSendTransient("chat.postMessage returned 429")
    with pytest.raises(HTTPException) as exc:
        routes.task_remind({"claim_request_id": "crq_1"})
    assert exc.value.status_code == 503
    assert env["recorded"] == []


def test_permanent_slack_failure_returns_200_with_audit_log(env):
    """재시도해도 같은 실패다 — 큐를 계속 돌릴 이유가 없다."""
    env["post_result"] = SlackSendPermanent("chat.postMessage error: channel_not_found")
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result == {"status": "ignored", "reason": "send_failed"}
    assert env["recorded"] == []
    assert "CLAIM_REQUEST_SEND_FAILED" in _actions(env)
    # 발송은 실패했지만 건은 닫혀야 한다 — 만료 태스크가 붙는다.
    assert _only_enqueued_delay(env) == pytest.approx(12 * 3600, abs=2)


def test_store_unavailable_returns_503(env, monkeypatch):
    def boom(_id, *, expected_action, now):
        raise ReceiptStoreUnavailable("Failed to commit transaction in 5 attempts.")

    monkeypatch.setattr(routes, "claim_send_slot", boom)
    with pytest.raises(HTTPException) as exc:
        routes.task_remind({"claim_request_id": "crq_1"})
    assert exc.value.status_code == 503


def test_store_unavailable_stays_503_when_the_audit_sink_is_down(env, monkeypatch):
    """감사 로그가 맨몸으로 호출되면 싱크가 죽었을 때 503이 500으로 뒤집힌다 —
    큐가 보는 재시도 신호가 바뀐다."""

    def boom(_id, *, expected_action, now):
        raise ReceiptStoreUnavailable("Failed to commit transaction in 5 attempts.")

    monkeypatch.setattr(routes, "claim_send_slot", boom)
    monkeypatch.setattr(
        routes, "record_audit_log", lambda **kw: (_ for _ in ()).throw(RuntimeError("audit sink down"))
    )
    with pytest.raises(HTTPException) as exc:
        routes.task_remind({"claim_request_id": "crq_1"})
    assert exc.value.status_code == 503


# --- 기록 · 재예약 ---


def test_successful_initial_send_records_and_reschedules(env):
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result == {
        "status": "ok",
        "action": "SEND_INITIAL",
        "slack_ts": "1755500000.000100",
    }
    assert env["recorded"] == [("crq_1", "1755500000.000100", ReminderAction.SEND_INITIAL)]
    # 최초 발송 뒤에는 REMINDER_DELAY_SECONDS 뒤에 재촉을 깨운다.
    assert env["enqueued"] == [("crq_1", 20)]
    assert "CLAIM_REQUEST_SENT" in _actions(env)


def test_initial_reschedule_default_stays_under_the_ttl(env, monkeypatch):
    """이 env는 로컬 `.env`에 없다 — 기본값으로 도는 환경이 실재한다.

    **기본 지연은 CLAIM_REQUEST_TTL_SECONDS보다 반드시 짧아야 한다.** 같으면
    `apply_claimant_verdict`가 만든 `expires_at = t0 + TTL`과 재촉 깨어남이
    정확히 겹쳐 decide()가 EXPIRE로 빠지고 재촉 DM이 0회 나간다. 리터럴로
    고정하지 않고 TTL과의 **관계**를 조인다 — 어느 쪽 기본값을 바꿔도 여기서 걸린다.
    """
    monkeypatch.delenv("REMINDER_DELAY_SECONDS", raising=False)
    routes.task_remind({"claim_request_id": "crq_1"})
    (_, delay), = env["enqueued"]
    assert delay < store.CLAIM_REQUEST_TTL_SECONDS, (
        f"기본 지연 {delay}s가 TTL {store.CLAIM_REQUEST_TTL_SECONDS}s 이상이다 — 재촉이 만료에 먹힌다"
    )


def test_non_integer_delay_env_fails_before_sending(env, monkeypatch):
    """env가 비정수면 ValueError는 나야 한다 — 다만 **발송 전에** 나야 한다.
    발송 뒤에 나면 DM은 이미 갔는데 재예약이 영영 안 붙는 흡수 상태가 된다."""
    monkeypatch.setenv("REMINDER_DELAY_SECONDS", "1h")
    with pytest.raises(ValueError):
        routes.task_remind({"claim_request_id": "crq_1"})
    assert env["sent"] == [], "지연 파싱이 발송보다 뒤에 있다 — DM이 나간 뒤 500이 난다"
    assert env["recorded"] == []


def test_reminder_send_reschedules_to_expiry(env):
    """다음 깨어남은 **expires_at 이후**여야 한다.

    절삭(`int()`)하면 마이크로초가 있는 Firestore 타임스탬프에서 깨어남이
    expires_at보다 이르게 잡히고, 그 태스크는 아직 만료 전이라 decide()가 SKIP을
    내며 SKIP은 재예약하지 않는다 — claim_request가 REMINDED에 영구 정체한다.
    그래서 `delay`가 남은 시간을 **덮는지**를 조인다. 범위로 느슨하게 보면
    1초 모자란 값이 그대로 통과한다.
    """
    before = datetime.now(UTC)
    # 소수부를 남긴다 — 절삭과 올림이 갈리는 지점이 여기다.
    expires_at = before + timedelta(seconds=300, microseconds=500000)
    env["claim_request"] = _claim_request(slack_dm_ts="1755500000.000100", expires_at=expires_at)

    routes.task_remind({"claim_request_id": "crq_1"})

    assert env["recorded"][0][2] == ReminderAction.SEND_REMINDER
    claim_request_id, delay = env["enqueued"][0]
    assert claim_request_id == "crq_1"
    # 라우트의 now는 before 이후다 — before + delay가 expires_at을 덮으면
    # 실제 예약 시각은 반드시 만료 이후다.
    assert before + timedelta(seconds=delay) >= expires_at, (
        f"delay={delay}s는 expires_at을 덮지 못한다 — 깨어난 태스크가 SKIP으로 빠진다"
    )


def test_reminder_reschedule_rounds_up_sub_second_remainder(env):
    """1초 미만의 잔여도 올려야 한다. 내리면 delay가 0이 되어 만료 전에 깨어난다."""
    before = datetime.now(UTC)
    expires_at = before + timedelta(microseconds=400000)
    env["claim_request"] = _claim_request(slack_dm_ts="1755500000.000100", expires_at=expires_at)

    routes.task_remind({"claim_request_id": "crq_1"})

    assert env["enqueued"][0][1] >= 1


def test_reminder_send_without_expires_at_does_not_reschedule(env):
    """만료 시각을 모르면 추측하지 않는다 — decide()와 같은 판단이다."""
    env["claim_request"] = _claim_request(slack_dm_ts="1755500000.000100", expires_at=None)
    result = routes.task_remind({"claim_request_id": "crq_1"})
    # expires_at이 없으면 decide는 SKIP이다 — 발송 자체가 없다.
    assert result == {"status": "ok", "action": "SKIP"}
    assert env["enqueued"] == []


def test_enqueue_failure_is_swallowed(env, monkeypatch):
    """전이도 발송도 이미 끝났다. 여기서 500을 내면 재시도가 CAS에 걸려
    아무 의미 없이 같은 지점에서 다시 죽는다."""

    def boom(_id, *, delay_seconds):
        raise RuntimeError("CLOUD_TASKS_QUEUE not configured")

    monkeypatch.setattr(routes, "enqueue_remind", boom)
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result["status"] == "ok"
    assert "REMIND_ENQUEUE_FAILED" in _actions(env)


def test_sent_audit_failure_does_not_500(env, monkeypatch):
    """발송은 이미 나갔다 — 감사 로그 실패로 500을 내면 재시도가 중복 발송을
    시도하게 된다."""

    def failing_audit(**kw):
        env["audit"].append(kw)
        if kw["action"] == "CLAIM_REQUEST_SENT":
            raise RuntimeError("audit sink down")

    monkeypatch.setattr(routes, "record_audit_log", failing_audit)
    result = routes.task_remind({"claim_request_id": "crq_1"})
    assert result["status"] == "ok"
