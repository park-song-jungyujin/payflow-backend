"""schema-contract.md §2 `sessions` — 로그인 세션 토큰 발급/검증.

`guards/tokens.py`의 승인 토큰과 같은 패턴이다: 원문은 응답으로 한 번만 나가고
저장은 sha256 해시만 한다. 여기 있는 어떤 함수도 원문을 반환값 밖으로 새어
나가게 하지 않는다.
"""

import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from .store import create_session, get_session


def issue_session(executor_id: str, org_id: str, email: str) -> str:
    token = secrets.token_urlsafe(32)
    ttl = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))
    now = datetime.now(UTC)
    create_session(
        hashlib.sha256(token.encode("utf-8")).hexdigest(),
        {
            "executor_id": executor_id,
            "org_id": org_id,
            "email": email,
            "expires_at": now + timedelta(seconds=ttl),
            "created_at": now,
        },
    )
    return token


def verify_session(token: str | None) -> dict:
    """실패하면 401을 던진다. 성공하면 `{executor_id, org_id, email}`을 반환한다."""
    if not token:
        raise HTTPException(status_code=401, detail="session missing")

    session = get_session(hashlib.sha256(token.encode("utf-8")).hexdigest())
    if session is None:
        raise HTTPException(status_code=401, detail="invalid session")

    if session["expires_at"] < datetime.now(UTC):
        raise HTTPException(status_code=401, detail="session expired")

    return {
        "executor_id": session["executor_id"],
        "org_id": session["org_id"],
        "email": session["email"],
    }
