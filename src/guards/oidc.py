"""schema-contract.md §9/§10 — Cloud Tasks·agent가 부르는 라우트 전용 OIDC 검증.

main.py의 /tasks/ping, payouts/routes.py의 /tasks/*, 이 파일의 /agents/*가
전부 같은 방식을 쓴다 — 하나로 모아 셋이 갈라지지 않게 한다.
"""

import os

from fastapi import HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

_google_request = google_requests.Request()


def verify_oidc(authorization: str) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    token = authorization.removeprefix("Bearer ")
    audience = os.environ["OIDC_AUDIENCE"]
    try:
        return id_token.verify_oauth2_token(token, _google_request, audience=audience)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
