"""schema-contract.md §10 — POST /slack/events.

서명 검증 → Firestore raw 저장 → enqueue → 200. architecture.md §비동기가
목표를 0.5s로 두고 Slack 제한이 3초다.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.ingest import routes
from src.ingest.store import ReceiptStoreUnavailable
from src.main import app

SECRET = "test-signing-secret"


def _post(client, payload: dict, *, timestamp: str | None = None, secret: str = SECRET):
    raw = json.dumps(payload).encode()
    ts = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode(), b"v0:" + ts.encode() + b":" + raw, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/slack/events",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": f"v0={digest}",
        },
    )


def _file_message(file_ids: list[str]) -> dict:
    return {
        "type": "event_callback",
        "event_id": "Ev01ABCDEF",
        "event": {
            "type": "message",
            "user": "U01ABCDEF",
            "channel": "C01ABCDEF",
            "ts": "1755500000.000100",
            "files": [{"id": f, "mimetype": "image/jpeg"} for f in file_ids],
        },
    }


@pytest.fixture
def client(monkeypatch):
    calls = {"created": [], "enqueued": [], "registered": [], "posted": []}

    def fake_create(*, recipient_id, slack_file_id, slack_channel_id, slack_message_ts):
        seen = [c["slack_file_id"] for c in calls["created"]]
        if slack_file_id in seen:
            return f"rct_{slack_file_id}", False
        calls["created"].append(
            {"recipient_id": recipient_id, "slack_file_id": slack_file_id}
        )
        return f"rct_{slack_file_id}", True

    def fake_register(*, slack_user_id, paypal_email):
        calls["registered"].append({"slack_user_id": slack_user_id, "paypal_email": paypal_email})
        return {"recipient_id": "rcp_new", "slack_user_id": slack_user_id, "paypal_email": paypal_email}

    monkeypatch.setattr(
        routes,
        "find_recipient_by_slack_user",
        lambda uid: {"recipient_id": "rcp_1"} if uid == "U01ABCDEF" else None,
    )
    monkeypatch.setattr(routes, "create_receipt_if_absent", fake_create)
    monkeypatch.setattr(routes, "create_recipient_from_slack", fake_register)
    monkeypatch.setattr(
        routes, "enqueue_parse_receipt", lambda rid: calls["enqueued"].append(rid)
    )
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)
    # 실제 Slack API를 부르면 안 된다 — main.py의 load_dotenv()가 개발자 .env의
    # 진짜 SLACK_BOT_TOKEN을 읽어올 수 있어, 모킹 없이 두면 테스트가 실제로
    # Slack에 메시지를 보내려 시도한다.
    monkeypatch.setattr(
        routes, "post_message", lambda **kw: calls["posted"].append(kw) or "1234.5678"
    )

    test_client = TestClient(app)
    test_client.calls = calls
    return test_client


def test_url_verification_echoes_challenge(client):
    """Slack 앱 설정에서 Event Subscriptions URL을 등록할 때 오는 핸드셰이크.
    이게 없으면 앱 등록 자체가 안 된다."""
    response = _post(client, {"type": "url_verification", "challenge": "abc123xyz"})
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123xyz"}


def test_url_verification_still_requires_valid_signature(client):
    """핸드셰이크라고 서명 검증을 건너뛰지 않는다."""
    response = _post(
        client, {"type": "url_verification", "challenge": "abc"}, secret="wrong"
    )
    assert response.status_code == 401


def test_bad_signature_is_rejected(client):
    response = _post(client, _file_message(["F_AAA"]), secret="wrong-secret")
    assert response.status_code == 401
    assert client.calls["created"] == []


def test_stale_timestamp_is_rejected(client):
    stale = str(int(time.time()) - 600)
    response = _post(client, _file_message(["F_AAA"]), timestamp=stale)
    assert response.status_code == 401


def test_image_upload_creates_receipt_and_enqueues(client):
    response = _post(client, _file_message(["F_AAA"]))
    assert response.status_code == 200
    assert client.calls["created"] == [
        {"recipient_id": "rcp_1", "slack_file_id": "F_AAA"}
    ]
    assert client.calls["enqueued"] == ["rct_F_AAA"]


def test_two_files_in_one_message_create_two_receipts(client):
    response = _post(client, _file_message(["F_AAA", "F_BBB"]))
    assert response.status_code == 200
    assert len(client.calls["created"]) == 2
    assert client.calls["enqueued"] == ["rct_F_AAA", "rct_F_BBB"]


def test_slack_retry_does_not_duplicate_receipt(client):
    _post(client, _file_message(["F_AAA"]))
    response = _post(client, _file_message(["F_AAA"]))
    assert response.status_code == 200
    assert len(client.calls["created"]) == 1
    # 문서는 하나지만 enqueue는 다시 한다 — 파싱 태스크는 같은 receipt_id를
    # 덮어쓰므로 멱등이고, 앞 요청이 enqueue 직전에 죽었을 수 있다.
    assert client.calls["enqueued"] == ["rct_F_AAA", "rct_F_AAA"]


def test_unregistered_user_is_acked_without_receipt(client):
    payload = _file_message(["F_AAA"])
    payload["event"]["user"] = "U_NOBODY"
    payload["event"]["text"] = "영수증입니다"  # 이메일이 아니므로 등록되지 않는다
    response = _post(client, payload)
    assert response.status_code == 200
    assert client.calls["created"] == []
    assert client.calls["registered"] == []


def test_unregistered_user_without_email_gets_registration_prompt(client):
    """등록 안내는 조용히 버리지 않는다는 계약의 일부다."""
    payload = _file_message([])
    payload["event"]["user"] = "U_NOBODY"
    payload["event"]["text"] = "안녕하세요"
    response = _post(client, payload)
    assert response.status_code == 200
    assert client.calls["registered"] == []
    assert len(client.calls["posted"]) == 1
    assert "PayPal" in client.calls["posted"][0]["text"]


def test_unregistered_user_with_email_gets_registered(client):
    """이메일만으로 연결한다 — 비밀번호는 절대 묻지 않는다."""
    payload = _file_message([])
    payload["event"]["user"] = "U_NEW"
    payload["event"]["text"] = "제 페이팔 이메일은 new-user@example.com 입니다"
    response = _post(client, payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "reason": "recipient_registered"}
    assert client.calls["registered"] == [
        {"slack_user_id": "U_NEW", "paypal_email": "new-user@example.com"}
    ]
    # 등록 안내만 나가고, 이번 메시지에 파일이 없었으니 receipt는 안 만든다 —
    # 사용자가 등록 후 영수증을 다시 보내야 한다.
    assert client.calls["created"] == []
    assert len(client.calls["posted"]) == 1
    assert "new-user@example.com" in client.calls["posted"][0]["text"]


def test_registration_dm_failure_does_not_undo_registration(client, monkeypatch):
    """Firestore 쓰기(등록)가 이미 끝났으면, 안내 DM 실패로 되돌리지 않는다."""
    from src.ingest.slack_client import SlackSendTransient

    def boom(**kwargs):
        raise SlackSendTransient("slack down")

    monkeypatch.setattr(routes, "post_message", boom)

    payload = _file_message([])
    payload["event"]["user"] = "U_NEW"
    payload["event"]["text"] = "new-user@example.com"
    response = _post(client, payload)

    assert response.status_code == 200
    assert client.calls["registered"] == [
        {"slack_user_id": "U_NEW", "paypal_email": "new-user@example.com"}
    ]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("new-user@example.com", "new-user@example.com"),
        ("제 이메일은 new-user@example.com 입니다", "new-user@example.com"),
        # 회귀 — 도메인에 점이 2개 이상이면(서브도메인) 마지막 TLD 앞에서 잘렸었다.
        ("pf_test4@personal.example.com", "pf_test4@personal.example.com"),
        ("user@mail.corp.example.co.kr", "user@mail.corp.example.co.kr"),
        ("안녕하세요", None),
        ("", None),
        ("이메일 없이 @만 있음", None),
    ],
)
def test_extract_email(text, expected):
    assert routes._extract_email(text) == expected


def test_message_without_files_is_ignored(client):
    payload = _file_message([])
    payload["event"]["text"] = "안녕하세요"
    response = _post(client, payload)
    assert response.status_code == 200
    assert client.calls["created"] == []


def test_bot_message_is_ignored(client):
    """자기가 보낸 메시지에 반응해 무한 루프를 만들지 않는다."""
    payload = _file_message(["F_AAA"])
    payload["event"]["bot_id"] = "B01ABCDEF"
    response = _post(client, payload)
    assert response.status_code == 200
    assert client.calls["created"] == []


def test_non_image_file_is_ignored(client):
    payload = _file_message(["F_AAA"])
    payload["event"]["files"] = [{"id": "F_AAA", "mimetype": "application/pdf"}]
    response = _post(client, payload)
    assert response.status_code == 200
    assert client.calls["created"] == []


def test_queue_not_configured_still_acks(client, monkeypatch):
    """3초 ack가 최우선이다. 큐 문제로 Slack 재전송을 유발하지 않는다 —
    receipts 문서는 이미 남았으므로 수동 재개가 가능하다."""
    from src.ingest.enqueue import QueueNotConfigured

    def boom(_):
        raise QueueNotConfigured("no queue")

    monkeypatch.setattr(routes, "enqueue_parse_receipt", boom)
    response = _post(client, _file_message(["F_AAA"]))
    assert response.status_code == 200
    assert len(client.calls["created"]) == 1


def test_firestore_contention_returns_503_not_500(client, monkeypatch):
    """트랜잭션 재시도가 소진되면 store가 ReceiptStoreUnavailable을 올린다.

    이건 enqueue 실패와 다르게 **receipts 문서가 안 남은** 상황이므로 200으로
    삼키면 영수증이 조용히 사라진다. 재전송을 받아야 맞다. 다만 스택 트레이스
    500이 아니라 명시적 503 + 감사 로그로 남긴다 — 원인 추적이 가능해야 한다.
    """
    audit = []

    def boom(**kwargs):
        raise ReceiptStoreUnavailable("Failed to commit transaction in 5 attempts.")

    monkeypatch.setattr(routes, "create_receipt_if_absent", boom)
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit.append(kw))

    response = _post(client, _file_message(["F_AAA"]))
    assert response.status_code == 503
    assert any(a["action"] == "RECEIPT_INGEST_FAILED" for a in audit)


def test_unrelated_value_error_is_not_masked_as_503(client, monkeypatch):
    """503은 저장 실패 전용이다. 무관한 ValueError를 같이 삼키면 진짜 버그가
    '일시적 저장 장애'로 위장돼 조사에서 빠진다."""

    def boom(**kwargs):
        raise ValueError("응답 파싱 실패 — 저장과 무관한 버그")

    monkeypatch.setattr(routes, "create_receipt_if_absent", boom)

    with pytest.raises(ValueError, match="저장과 무관한 버그"):
        _post(client, _file_message(["F_AAA"]))


def test_file_count_over_cap_is_deferred_not_dropped(client):
    """상한 초과분을 버리면 영수증이 조용히 사라진다. ack는 즉시 주고 처리 못 한
    file_id를 감사 로그에 남긴다 — 나중에 복구할 수 있어야 한다."""
    file_ids = [f"F_{i}" for i in range(routes.MAX_FILES_PER_EVENT + 3)]
    response = _post(client, _file_message(file_ids))

    assert response.status_code == 200
    assert len(client.calls["created"]) == routes.MAX_FILES_PER_EVENT
    assert response.json()["receipt_ids"] == [
        f"rct_F_{i}" for i in range(routes.MAX_FILES_PER_EVENT)
    ]


def test_deferred_file_ids_are_recorded_in_audit_log(client, monkeypatch):
    audit = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit.append(kw))

    file_ids = [f"F_{i}" for i in range(routes.MAX_FILES_PER_EVENT + 3)]
    _post(client, _file_message(file_ids))

    deferred = [a for a in audit if a["action"] == "RECEIPT_INGEST_DEFERRED"]
    assert len(deferred) == 1
    assert deferred[0]["after"]["deferred_slack_file_ids"] == [
        f"F_{i}" for i in range(routes.MAX_FILES_PER_EVENT, routes.MAX_FILES_PER_EVENT + 3)
    ]


def test_file_count_at_cap_is_fully_processed(client):
    """경계값 — 상한과 같으면 전부 처리하고 유예 로그를 남기지 않는다."""
    file_ids = [f"F_{i}" for i in range(routes.MAX_FILES_PER_EVENT)]
    response = _post(client, _file_message(file_ids))

    assert response.status_code == 200
    assert len(client.calls["created"]) == routes.MAX_FILES_PER_EVENT


def test_round_trip_stays_within_slack_budget(client, monkeypatch):
    """서명검증 → Firestore 쓰기 → enqueue 왕복이 3초 안이어야 한다.

    Firestore·Cloud Tasks에 현실적인 지연을 주입해 잰다. fake가 즉시 반환하면
    아무것도 검증하지 못하므로 일부러 느리게 만든다. 이미지 3장짜리 메시지를
    쓰는 이유는 파일 수에 비례해 늘어나는 직렬 호출을 잡기 위해서다 — 여기서
    새는 설계가 실제 배포에서 3초를 넘긴다.

    절대적 보장은 아니다. 실측은 배포 후 Cloud Run 로그로 다시 확인한다.
    """
    FIRESTORE_LATENCY = 0.15
    ENQUEUE_LATENCY = 0.10

    real_create = routes.create_receipt_if_absent

    def slow_create(**kwargs):
        time.sleep(FIRESTORE_LATENCY)
        return real_create(**kwargs)

    def slow_enqueue(receipt_id):
        time.sleep(ENQUEUE_LATENCY)

    monkeypatch.setattr(routes, "create_receipt_if_absent", slow_create)
    monkeypatch.setattr(routes, "enqueue_parse_receipt", slow_enqueue)

    started = time.perf_counter()
    response = _post(client, _file_message(["F_AAA", "F_BBB", "F_CCC"]))
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 3.0, f"Slack 3초 제한 초과: {elapsed:.2f}s"
    # architecture.md §비동기의 목표는 0.5s다. 주입한 지연 합(0.75s)을 뺀
    # 라우트 자체 오버헤드가 그 안에 들어오는지 본다.
    injected = 3 * (FIRESTORE_LATENCY + ENQUEUE_LATENCY)
    assert elapsed - injected < 0.5, f"라우트 자체 오버헤드 과다: {elapsed - injected:.2f}s"
