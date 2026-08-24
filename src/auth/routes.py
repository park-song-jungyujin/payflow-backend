"""schema-contract.md §10 — /auth/* (C 소유).

`web`은 시크릿이 없다(architecture.md). Google/Slack 양쪽 다 authorization
code를 받으면 여기로 그대로 넘기고, client secret이 필요한 교환은 전부 이
파일 아래 helper들이 한다.
"""

import hashlib
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException
from ulid import ULID

from ..guards.audit import record_audit_log
from .google_oauth import GoogleOAuthError, exchange_google_code
from .session import issue_session, verify_session
from .slack_oauth import SlackOAuthError, build_authorize_url, exchange_slack_code
from .store import (
    create_executor,
    create_org,
    create_slack_workspace,
    delete_session,
    get_executor_by_google_sub,
    get_org,
)

router = APIRouter()

_ACTOR = "api/src/auth"


def _session_from_header(authorization: str) -> dict:
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
    return verify_session(token)


@router.post("/auth/google/callback")
def google_callback(body: dict):
    code = body.get("code")
    redirect_uri = body.get("redirect_uri") or os.environ["GOOGLE_OAUTH_REDIRECT_URI"]
    if not code:
        raise HTTPException(status_code=400, detail="code required")

    try:
        profile = exchange_google_code(code, redirect_uri)
    except GoogleOAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    executor = get_executor_by_google_sub(profile["google_sub"])
    now = datetime.now(UTC)

    if executor is None:
        org_name = body.get("org_name")
        if not org_name:
            raise HTTPException(
                status_code=400,
                detail="unknown account — org_name required to create a new organization",
            )
        org_id = f"org_{ULID()}"
        executor_id = f"exe_{ULID()}"
        create_org(
            org_id,
            {
                "org_id": org_id,
                "name": org_name,
                "created_by": executor_id,
                "created_at": now,
                "updated_at": now,
            },
        )
        executor = {
            "executor_id": executor_id,
            "org_id": org_id,
            "email": profile["email"],
            "google_sub": profile["google_sub"],
            "name": profile["name"],
            "status": "ACTIVE",
            "created_at": now,
            "updated_at": now,
        }
        create_executor(executor_id, executor)
        record_audit_log(
            actor=_ACTOR,
            action="ORG_CREATED",
            after={"org_id": org_id, "executor_id": executor_id},
        )

    if executor["status"] != "ACTIVE":
        raise HTTPException(status_code=403, detail="executor account disabled")

    session_token = issue_session(executor["executor_id"], executor["org_id"], executor["email"])
    record_audit_log(
        org_id=executor["org_id"],
        actor=executor["email"],
        actor_type="HUMAN",
        action="EXECUTOR_LOGIN",
        after={"executor_id": executor["executor_id"]},
    )

    return {
        "session_token": session_token,
        "executor_id": executor["executor_id"],
        "org_id": executor["org_id"],
        "email": executor["email"],
        "name": executor["name"],
    }


@router.get("/auth/me")
def me(authorization: str = Header(default="")):
    session = _session_from_header(authorization)
    org = get_org(session["org_id"])
    return {
        "executor_id": session["executor_id"],
        "email": session["email"],
        "org_id": session["org_id"],
        "org_name": org["name"] if org else None,
    }


@router.post("/auth/logout")
def logout(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
    if token:
        delete_session(hashlib.sha256(token.encode("utf-8")).hexdigest())
    return {"status": "ok"}


@router.get("/auth/slack/install")
def slack_install(authorization: str = Header(default="")):
    session = _session_from_header(authorization)
    url = build_authorize_url(state=session["org_id"])
    return {"authorize_url": url}


@router.post("/auth/slack/callback")
def slack_callback(body: dict, authorization: str = Header(default="")):
    session = _session_from_header(authorization)
    code = body.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="code required")

    try:
        install = exchange_slack_code(code, os.environ["SLACK_OAUTH_REDIRECT_URI"])
    except SlackOAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    now = datetime.now(UTC)
    create_slack_workspace(
        install["team_id"],
        {
            "team_id": install["team_id"],
            "org_id": session["org_id"],
            "bot_token": install["bot_token"],
            "bot_user_id": install["bot_user_id"],
            "scope": install["scope"],
            "installed_at": now,
            "installed_by": session["executor_id"],
            "updated_at": now,
        },
    )
    record_audit_log(
        org_id=session["org_id"],
        actor=session["executor_id"],
        actor_type="HUMAN",
        action="SLACK_WORKSPACE_INSTALLED",
        after={"team_id": install["team_id"]},
    )
    return {"team_id": install["team_id"], "org_id": session["org_id"]}
