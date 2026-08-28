"""schema-contract.md §10 — GET /auth/slack/install, POST /auth/slack/callback.

`SLACK_APP_CLIENT_SECRET`을 아는 유일한 지점이다. 기관은 여기서 **자기 기존
워크스페이스**에 앱을 설치한다 — Slack에는 새 워크스페이스를 만들어주는 API가
없다(architecture.md "org 스코핑과 로그인"). `SLACK_SIGNING_SECRET`은 이 흐름과
무관하다 — 그건 `ingest/signature.py`가 앱 전체 단위로 이미 쓰고 있고 여기서
바뀌지 않는다.
"""

import os

import requests

_OAUTH_ACCESS_ENDPOINT = "https://slack.com/api/oauth.v2.access"
_AUTHORIZE_ENDPOINT = "https://slack.com/oauth/v2/authorize"
_BOT_SCOPES = "files:read,users:read,chat:write,im:history"
_TIMEOUT_SECONDS = 10


class SlackOAuthError(RuntimeError):
    """code 교환 실패. 라우트가 401/502로 바꾼다."""


def build_authorize_url(state: str) -> str:
    """`state`에 org_id를 실어 보낸다 — 콜백이 어느 기관의 설치인지 안다."""
    redirect_uri = os.environ["SLACK_OAUTH_REDIRECT_URI"]
    client_id = os.environ["SLACK_APP_CLIENT_ID"]
    return (
        f"{_AUTHORIZE_ENDPOINT}?client_id={client_id}&scope={_BOT_SCOPES}"
        f"&redirect_uri={redirect_uri}&state={state}"
    )


def exchange_slack_code(code: str, redirect_uri: str) -> dict:
    """authorization code → `{team_id, bot_token, bot_user_id, scope}`."""
    try:
        res = requests.post(
            _OAUTH_ACCESS_ENDPOINT,
            data={
                "code": code,
                "client_id": os.environ["SLACK_APP_CLIENT_ID"],
                "client_secret": os.environ["SLACK_APP_CLIENT_SECRET"],
                "redirect_uri": redirect_uri,
            },
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise SlackOAuthError(f"oauth.v2.access failed: {e}") from e

    if res.status_code != 200:
        raise SlackOAuthError(f"oauth.v2.access returned {res.status_code}")

    body = res.json()
    if not body.get("ok"):
        raise SlackOAuthError(f"oauth.v2.access error: {body.get('error')}")

    team = body.get("team") or {}
    bot_token = body.get("access_token")
    bot_user_id = body.get("bot_user_id")
    if not bot_token or not bot_user_id or not team.get("id"):
        raise SlackOAuthError("oauth.v2.access response missing team/bot fields")

    return {
        "team_id": team["id"],
        "bot_token": bot_token,
        "bot_user_id": bot_user_id,
        "scope": body.get("scope", ""),
    }
