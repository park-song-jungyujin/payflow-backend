"""Slack chat.postMessage 발송 (A 소유).

src/parsing/slack_files.py가 정한 관용구를 그대로 따른다: 봇 토큰은
SLACK_BOT_TOKEN, 타임아웃 20초, 429·5xx·네트워크는 재시도 가능으로 본다.

Slack Web API는 논리적 실패도 HTTP 200으로 준다 — `ok` 필드를 반드시 봐야 한다.
"""

import os

import requests as http_requests

_SLACK_POST_MESSAGE = "https://slack.com/api/chat.postMessage"
_TIMEOUT_SECONDS = 20
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class SlackSendError(RuntimeError):
    """chat.postMessage 발송 실패."""


class SlackSendTransient(SlackSendError):
    """재시도하면 될 실패."""


class SlackSendPermanent(SlackSendError):
    """다시 불러도 같은 실패."""


def _bot_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        # 설정 누락이지 이 요청의 문제가 아니다.
        raise SlackSendTransient("SLACK_BOT_TOKEN not configured")
    return token


def post_message(*, channel: str, text: str, thread_ts: str | None = None) -> str:
    headers = {"Authorization": f"Bearer {_bot_token()}"}
    payload = {"channel": channel, "text": text}
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts

    try:
        response = http_requests.post(_SLACK_POST_MESSAGE, headers=headers, json=payload, timeout=_TIMEOUT_SECONDS)
    except http_requests.RequestException as e:
        raise SlackSendTransient(f"chat.postMessage failed: {e}") from e

    if response.status_code in _RETRYABLE_STATUS:
        raise SlackSendTransient(f"chat.postMessage returned {response.status_code}")

    try:
        body = response.json()
    except ValueError as e:
        raise SlackSendTransient(f"chat.postMessage returned non-JSON body: {e}") from e

    if not body.get("ok"):
        raise SlackSendPermanent(f"chat.postMessage error: {body.get('error')}")

    return body["ts"]
