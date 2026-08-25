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


def test_explicit_bot_token_overrides_env(monkeypatch):
    calls = _wire(monkeypatch, FakeResponse(json_body={"ok": True, "ts": "1"}))
    slack_client.post_message(channel="U1", text="hi", bot_token="xoxb-install-specific")
    assert calls[0]["headers"]["Authorization"] == "Bearer xoxb-install-specific"


def test_explicit_bot_token_works_without_env_var(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    calls = _wire(monkeypatch, FakeResponse(json_body={"ok": True, "ts": "1"}))
    slack_client.post_message(channel="U1", text="hi", bot_token="xoxb-install-specific")
    assert calls[0]["headers"]["Authorization"] == "Bearer xoxb-install-specific"


# --- get_display_name — 셀프 등록 recipients.display_name 조회 (읽기 전용, best-effort) ---


def _wire_get(monkeypatch, response=None, *, raises=None):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        if raises:
            raise raises
        return response

    monkeypatch.setattr(slack_client.http_requests, "get", fake_get)
    return calls


def test_get_display_name_prefers_display_name_over_real_name(monkeypatch):
    _wire_get(
        monkeypatch,
        FakeResponse(
            json_body={"ok": True, "user": {"profile": {"display_name": "수현", "real_name": "박수현"}}}
        ),
    )
    assert slack_client.get_display_name("U_NEW") == "수현"


def test_get_display_name_falls_back_to_real_name(monkeypatch):
    """display_name을 프로필에 안 채운 사용자가 많다 — real_name으로 대체한다."""
    _wire_get(
        monkeypatch,
        FakeResponse(json_body={"ok": True, "user": {"profile": {"display_name": "", "real_name": "박수현"}}}),
    )
    assert slack_client.get_display_name("U_NEW") == "박수현"


def test_get_display_name_returns_none_when_ok_is_false(monkeypatch):
    """users:read 스코프가 없는 등 논리적 실패 — 예외를 던지지 않고 None만
    돌려준다. 등록 자체를 막으면 안 된다."""
    _wire_get(monkeypatch, FakeResponse(json_body={"ok": False, "error": "missing_scope"}))
    assert slack_client.get_display_name("U_NEW") is None


def test_get_display_name_returns_none_on_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise slack_client.http_requests.RequestException("connection reset")

    monkeypatch.setattr(slack_client.http_requests, "get", boom)
    assert slack_client.get_display_name("U_NEW") is None


def test_get_display_name_returns_none_without_bot_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert slack_client.get_display_name("U_NEW") is None


# --- get_user_locale — 번역된 재요청 DM 발송 여부 판단용(읽기 전용, best-effort) ---


def test_get_user_locale_returns_locale_and_requests_include_locale(monkeypatch):
    calls = _wire_get(monkeypatch, FakeResponse(json_body={"ok": True, "user": {"locale": "en-US"}}))
    assert slack_client.get_user_locale("U_NEW") == "en-US"
    assert calls[0]["params"] == {"user": "U_NEW", "include_locale": "true"}


def test_get_user_locale_returns_none_when_ok_is_false(monkeypatch):
    _wire_get(monkeypatch, FakeResponse(json_body={"ok": False, "error": "missing_scope"}))
    assert slack_client.get_user_locale("U_NEW") is None


def test_get_user_locale_returns_none_on_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise slack_client.http_requests.RequestException("connection reset")

    monkeypatch.setattr(slack_client.http_requests, "get", boom)
    assert slack_client.get_user_locale("U_NEW") is None


def test_get_user_locale_returns_none_without_bot_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert slack_client.get_user_locale("U_NEW") is None


# --- list_workspace_members — 설치 직후 온보딩 DM 대상 조회 ---


def test_list_workspace_members_filters_bots_deleted_and_slackbot(monkeypatch):
    _wire_get(
        monkeypatch,
        FakeResponse(
            json_body={
                "ok": True,
                "members": [
                    {"id": "U1", "deleted": False, "is_bot": False, "is_app_user": False},
                    {"id": "U2", "deleted": True, "is_bot": False, "is_app_user": False},
                    {"id": "U3", "deleted": False, "is_bot": True, "is_app_user": False},
                    {"id": "U4", "deleted": False, "is_bot": False, "is_app_user": True},
                    {"id": "USLACKBOT", "deleted": False, "is_bot": False, "is_app_user": False},
                ],
                "response_metadata": {"next_cursor": ""},
            }
        ),
    )
    members = slack_client.list_workspace_members("xoxb-abc")
    assert [m["id"] for m in members] == ["U1"]


def test_list_workspace_members_follows_pagination_cursor(monkeypatch):
    responses = [
        FakeResponse(
            json_body={
                "ok": True,
                "members": [{"id": "U1", "deleted": False, "is_bot": False, "is_app_user": False}],
                "response_metadata": {"next_cursor": "page2"},
            }
        ),
        FakeResponse(
            json_body={
                "ok": True,
                "members": [{"id": "U2", "deleted": False, "is_bot": False, "is_app_user": False}],
                "response_metadata": {"next_cursor": ""},
            }
        ),
    ]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params)
        return responses[len(calls) - 1]

    monkeypatch.setattr(slack_client.http_requests, "get", fake_get)

    members = slack_client.list_workspace_members("xoxb-abc")

    assert [m["id"] for m in members] == ["U1", "U2"]
    assert "cursor" not in calls[0]
    assert calls[1]["cursor"] == "page2"


def test_list_workspace_members_ok_false_raises_fetch_error(monkeypatch):
    _wire_get(monkeypatch, FakeResponse(json_body={"ok": False, "error": "missing_scope"}))
    with pytest.raises(slack_client.SlackFetchError):
        slack_client.list_workspace_members("xoxb-abc")


def test_list_workspace_members_network_error_raises_fetch_error(monkeypatch):
    def boom(*args, **kwargs):
        raise slack_client.http_requests.RequestException("connection reset")

    monkeypatch.setattr(slack_client.http_requests, "get", boom)
    with pytest.raises(slack_client.SlackFetchError):
        slack_client.list_workspace_members("xoxb-abc")
