"""Slack chat.postMessage 발송 (A 소유).

src/parsing/slack_files.py가 이미 봇 토큰 읽기·타임아웃·재시도 분류의
관용구를 정해뒀다 — 이 스위트도 그대로 맞춘다.

Slack Web API는 논리적 실패도 HTTP 200으로 준다. `ok` 필드를 반드시 봐야 한다.
"""

import pytest

from src.ingest import slack_client
from src.ingest.slack_client import SlackSendPermanent, SlackSendTransient


class FakeResponse:
    def __init__(self, *, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def _bot_token(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")


def _wire(monkeypatch, response):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return response

    monkeypatch.setattr(slack_client.http_requests, "post", fake_post)
    return calls


def test_sends_with_bearer_token(monkeypatch):
    calls = _wire(monkeypatch, FakeResponse(json_body={"ok": True, "ts": "1234.5678"}))

    ts = slack_client.post_message(channel="C123", text="hello")

    assert ts == "1234.5678"
    assert calls[0]["headers"]["Authorization"] == "Bearer xoxb-test"
    assert calls[0]["json"]["channel"] == "C123"
    assert calls[0]["json"]["text"] == "hello"


def test_thread_ts_included_when_given(monkeypatch):
    calls = _wire(monkeypatch, FakeResponse(json_body={"ok": True, "ts": "1"}))
    slack_client.post_message(channel="C123", text="hi", thread_ts="999.111")
    assert calls[0]["json"]["thread_ts"] == "999.111"


def test_thread_ts_omitted_when_absent(monkeypatch):
    calls = _wire(monkeypatch, FakeResponse(json_body={"ok": True, "ts": "1"}))
    slack_client.post_message(channel="C123", text="hi")
    assert "thread_ts" not in calls[0]["json"]


def test_ok_false_is_permanent(monkeypatch):
    _wire(monkeypatch, FakeResponse(json_body={"ok": False, "error": "channel_not_found"}))
    with pytest.raises(SlackSendPermanent):
        slack_client.post_message(channel="C123", text="hi")


def test_ok_false_not_in_channel_is_permanent(monkeypatch):
    _wire(monkeypatch, FakeResponse(json_body={"ok": False, "error": "not_in_channel"}))
    with pytest.raises(SlackSendPermanent):
        slack_client.post_message(channel="C123", text="hi")


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_retryable_status_is_transient(monkeypatch, status):
    _wire(monkeypatch, FakeResponse(status_code=status))
    with pytest.raises(SlackSendTransient):
        slack_client.post_message(channel="C123", text="hi")


def test_network_error_is_transient(monkeypatch):
    def boom(*args, **kwargs):
        raise slack_client.http_requests.RequestException("connection reset")

    monkeypatch.setattr(slack_client.http_requests, "post", boom)
    with pytest.raises(SlackSendTransient):
        slack_client.post_message(channel="C123", text="hi")


def test_missing_bot_token_is_transient(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(SlackSendTransient):
        slack_client.post_message(channel="C123", text="hi")
