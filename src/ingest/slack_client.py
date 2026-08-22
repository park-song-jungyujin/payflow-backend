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


# 버튼 하나. value에 claim_request_id를 실어 /slack/interactions가 어떤 요청에
# 대한 응답인지 안다 — 스레드 ts로 역추적하면 쿼리가 필요하고, 재촉이 스레드
# 답글로 나가는 경우 ts가 여러 개가 된다.
CLAIM_REQUEST_ACTION_ID = "claim_request_responded"


def requery_blocks(text: str, claim_request_id: str) -> list[dict]:
    """재요청 DM 본문 + 응답 버튼.

    문안(text)은 청구자 에이전트가 쓴 것을 그대로 넣는다 — 코드는 버튼만 붙인다.
    버튼이 없으면 `RESPONDED`가 영영 안 생겨 모든 요청이 만료된다."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "확인했어요"},
                    "action_id": CLAIM_REQUEST_ACTION_ID,
                    "value": claim_request_id,
                }
            ],
        },
    ]


def post_message(
    *, channel: str, text: str, thread_ts: str | None = None, blocks: list[dict] | None = None
) -> str:
    headers = {"Authorization": f"Bearer {_bot_token()}"}
    # blocks를 보내도 text는 함께 보낸다 — 알림 미리보기와 접근성 폴백이 text를 쓴다.
    payload = {"channel": channel, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
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
