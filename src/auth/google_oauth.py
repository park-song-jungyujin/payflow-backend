"""schema-contract.md §10 — POST /auth/google/callback이 쓰는 code 교환.

`GOOGLE_CLIENT_SECRET`을 아는 유일한 지점이다. `web`은 authorization code를
받으면 그대로 여기로 넘기기만 한다 — client secret이 브라우저나 web 서버에
들어가지 않는다(architecture.md "org 스코핑과 로그인").
"""

import os

import requests

_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
_TIMEOUT_SECONDS = 10


class GoogleOAuthError(RuntimeError):
    """code 교환 또는 사용자 정보 조회 실패. 라우트가 401/502로 바꾼다."""


def exchange_google_code(code: str, redirect_uri: str) -> dict:
    """authorization code → `{google_sub, email, name}`.

    Google 토큰 엔드포인트에서 access_token을 받고, userinfo 엔드포인트로
    프로필을 조회한다. 별도 JWT 검증 라이브러리를 새로 들이지 않는다 —
    `requests`는 이미 의존성에 있다.
    """
    try:
        token_res = requests.post(
            _TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise GoogleOAuthError(f"token exchange failed: {e}") from e

    if token_res.status_code != 200:
        raise GoogleOAuthError(f"token exchange returned {token_res.status_code}: {token_res.text}")

    access_token = token_res.json().get("access_token")
    if not access_token:
        raise GoogleOAuthError("token exchange response missing access_token")

    try:
        userinfo_res = requests.get(
            _USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise GoogleOAuthError(f"userinfo fetch failed: {e}") from e

    if userinfo_res.status_code != 200:
        raise GoogleOAuthError(f"userinfo fetch returned {userinfo_res.status_code}")

    userinfo = userinfo_res.json()
    sub = userinfo.get("sub")
    email = userinfo.get("email")
    if not sub or not email:
        raise GoogleOAuthError("userinfo response missing sub or email")

    return {"google_sub": sub, "email": email, "name": userinfo.get("name", email)}
