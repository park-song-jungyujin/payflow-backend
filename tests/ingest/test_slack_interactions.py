"""schema-contract.md §10 — POST /slack/interactions.

이 라우트가 없는 동안 `RESPONDED`는 도달 불가능한 상태였고 모든 claim_request가
만료됐다. 여기서 지키는 것:

1. 서명은 raw body(form-encoded) 기준이다 — /slack/events와 같은 함수
2. `PENDING`·`REMINDED`에서만 `RESPONDED`로 간다. `EXPIRED`는 되살리지 않는다
3. 모양이 예상과 다른 상호작용에 500·400을 내지 않는다 — Slack이 재전송하면
   사람에게 오류 배너가 뜨고, 다시 불러도 같은 본문이다
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from src.ingest import routes
from src.ingest.slack_client import CLAIM_REQUEST_ACTION_ID
from src.ingest.store import ReceiptStoreUnavailable
from src.main import app

SECRET = "test-signing-secret"


def _post(client, payload: dict | str, *, timestamp: str | None = None, secret: str = SECRET):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    raw = urlencode({"payload": body}).encode()
    ts = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode(), b"v0:" + ts.encode() + b":" + raw, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/slack/interactions",
        content=raw,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": f"v0={digest}",
        },
    )


def _button_click(claim_request_id: str | None = "crq_1", action_id: str = CLAIM_REQUEST_ACTION_ID):
    action = {"type": "button", "action_id": action_id}
    if claim_request_id is not None:
        action["value"] = claim_request_id
    return {
        "type": "block_actions",
        "user": {"id": "U01ABCDEF"},
        "actions": [action],
    }


@pytest.fixture
def client(monkeypatch):
    calls = {"responded": [], "audit": []}

    def fake_mark_responded(claim_request_id, *, now):
        calls["responded"].append(claim_request_id)
        return True

    monkeypatch.setattr(routes, "mark_responded", fake_mark_responded)
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: calls["audit"].append(kw))

    test_client = TestClient(app)
    test_client.calls = calls
    return test_client


def test_button_click_marks_claim_request_responded(client):
    response = _post(client, _button_click("crq_1"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "claim_request_id": "crq_1"}
    assert client.calls["responded"] == ["crq_1"]
    actions = [entry["action"] for entry in client.calls["audit"]]
    assert "CLAIM_REQUEST_RESPONDED" in actions


def test_bad_signature_is_401_and_touches_nothing(client):
    response = _post(client, _button_click("crq_1"), secret="wrong-secret")

    assert response.status_code == 401
    assert client.calls["responded"] == []


def test_stale_timestamp_is_401(client):
    """재전송 공격 — 서명이 맞아도 5분을 넘으면 거부한다."""
    response = _post(client, _button_click("crq_1"), timestamp=str(int(time.time()) - 3600))

    assert response.status_code == 401
    assert client.calls["responded"] == []


def test_already_closed_request_is_not_revived(client, monkeypatch):
    """EXPIRED·RESPONDED에서는 전이가 없다. 감사 로그도 안 남긴다 — Slack
    재전송마다 CLAIM_REQUEST_RESPONDED가 쌓이면 기록이 흐려진다."""
    monkeypatch.setattr(routes, "mark_responded", lambda cid, *, now: False)

    response = _post(client, _button_click("crq_1"))

    assert response.status_code == 200
    assert response.json()["reason"] == "not_open"
    assert [e["action"] for e in client.calls["audit"]] == []


def test_unparsable_payload_is_ignored_not_500(client):
    response = _post(client, "not-json{{{")

    assert response.status_code == 200
    assert response.json()["reason"] == "unparsable"
    assert client.calls["responded"] == []


def test_other_action_ids_are_ignored(client):
    """같은 Request URL로 다른 버튼도 온다. 모르는 action_id에 반응하지 않는다."""
    response = _post(client, _button_click("crq_1", action_id="something_else"))

    assert response.status_code == 200
    assert response.json()["reason"] == "unsupported_action"
    assert client.calls["responded"] == []


def test_non_block_actions_payload_is_ignored(client):
    response = _post(client, {"type": "view_submission"})

    assert response.status_code == 200
    assert response.json()["reason"] == "unsupported_type"
    assert client.calls["responded"] == []


def test_button_without_value_is_ignored(client):
    response = _post(client, _button_click(None))

    assert response.status_code == 200
    assert response.json()["reason"] == "no_target"
    assert client.calls["responded"] == []


def test_store_failure_does_not_500(client, monkeypatch):
    """트랜잭션 소진에 500을 내면 Slack이 재전송하고 사람에게는 오류 배너가 뜬다.
    남은 경로는 만료뿐이라 조용히 사라지지는 않는다."""

    def boom(cid, *, now):
        raise ReceiptStoreUnavailable("transaction retries exhausted")

    monkeypatch.setattr(routes, "mark_responded", boom)

    response = _post(client, _button_click("crq_1"))

    assert response.status_code == 200
    assert response.json()["reason"] == "store_unavailable"
    assert any(
        e["action"] == "CLAIM_REQUEST_RESPONSE_FAILED" for e in client.calls["audit"]
    )
