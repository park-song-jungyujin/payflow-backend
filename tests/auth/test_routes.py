"""schema-contract.md §10 /auth/* — Google 로그인·Slack 워크스페이스 설치.

TestClient/ASGI를 거치지 않고 라우트 핸들러를 직접 부른다(guards/settlements
test_routes.py와 같은 패턴). google_oauth·slack_oauth·store 전부 모듈 레벨에서
monkeypatch한다 — 실제 Google/Slack API를 호출하지 않는다.
"""

from fastapi import HTTPException
import pytest

from src.auth import routes


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "https://web.test/auth/callback")
    monkeypatch.setenv("SLACK_OAUTH_REDIRECT_URI", "https://web.test/slack/callback")
    monkeypatch.setenv("SESSION_TTL_SECONDS", "600")


def test_google_callback_creates_org_and_executor_on_first_login(monkeypatch):
    created_orgs, created_executors, sessions = [], [], []

    monkeypatch.setattr(
        routes, "exchange_google_code", lambda code, redirect_uri: {
            "google_sub": "sub_1", "email": "alice@acme.com", "name": "Alice"
        }
    )
    monkeypatch.setattr(routes, "get_executor_by_google_sub", lambda sub: None)
    monkeypatch.setattr(routes, "create_org", lambda org_id, doc: created_orgs.append(doc))
    monkeypatch.setattr(
        routes, "create_executor", lambda executor_id, doc: created_executors.append(doc)
    )
    monkeypatch.setattr(
        routes, "issue_session", lambda executor_id, org_id, email: "raw-session-token"
    )
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)

    result = routes.google_callback({"code": "abc", "org_name": "Acme Inc"})

    assert result["session_token"] == "raw-session-token"
    assert result["email"] == "alice@acme.com"
    assert len(created_orgs) == 1
    assert created_orgs[0]["name"] == "Acme Inc"
    assert len(created_executors) == 1
    assert created_executors[0]["google_sub"] == "sub_1"
    assert created_executors[0]["org_id"] == result["org_id"]


def test_google_callback_unknown_account_without_org_name_is_400(monkeypatch):
    monkeypatch.setattr(
        routes, "exchange_google_code", lambda code, redirect_uri: {
            "google_sub": "sub_1", "email": "alice@acme.com", "name": "Alice"
        }
    )
    monkeypatch.setattr(routes, "get_executor_by_google_sub", lambda sub: None)

    with pytest.raises(HTTPException) as exc:
        routes.google_callback({"code": "abc"})
    assert exc.value.status_code == 400


def test_google_callback_existing_executor_logs_in_without_creating_org(monkeypatch):
    created_orgs = []
    existing = {
        "executor_id": "exe_1",
        "org_id": "org_1",
        "email": "alice@acme.com",
        "google_sub": "sub_1",
        "name": "Alice",
        "status": "ACTIVE",
    }

    monkeypatch.setattr(
        routes, "exchange_google_code", lambda code, redirect_uri: {
            "google_sub": "sub_1", "email": "alice@acme.com", "name": "Alice"
        }
    )
    monkeypatch.setattr(routes, "get_executor_by_google_sub", lambda sub: existing)
    monkeypatch.setattr(routes, "create_org", lambda org_id, doc: created_orgs.append(doc))
    monkeypatch.setattr(
        routes, "issue_session", lambda executor_id, org_id, email: "raw-session-token"
    )
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)

    result = routes.google_callback({"code": "abc"})

    assert result["org_id"] == "org_1"
    assert created_orgs == []


def test_google_callback_disabled_executor_is_403(monkeypatch):
    existing = {
        "executor_id": "exe_1",
        "org_id": "org_1",
        "email": "alice@acme.com",
        "google_sub": "sub_1",
        "status": "DISABLED",
    }
    monkeypatch.setattr(
        routes, "exchange_google_code", lambda code, redirect_uri: {
            "google_sub": "sub_1", "email": "alice@acme.com", "name": "Alice"
        }
    )
    monkeypatch.setattr(routes, "get_executor_by_google_sub", lambda sub: existing)

    with pytest.raises(HTTPException) as exc:
        routes.google_callback({"code": "abc"})
    assert exc.value.status_code == 403


def test_slack_install_requires_session(monkeypatch):
    monkeypatch.setattr(
        routes, "verify_session", lambda token: (_ for _ in ()).throw(HTTPException(401, "no session"))
    )
    with pytest.raises(HTTPException) as exc:
        routes.slack_install(authorization="")
    assert exc.value.status_code == 401


def test_slack_install_returns_authorize_url_scoped_to_org(monkeypatch):
    monkeypatch.setattr(
        routes, "verify_session", lambda token: {"executor_id": "exe_1", "org_id": "org_1"}
    )
    monkeypatch.setenv("SLACK_APP_CLIENT_ID", "slack-client-id")

    result = routes.slack_install(authorization="Bearer t")

    assert "org_1" in result["authorize_url"]
    assert "slack-client-id" in result["authorize_url"]


def test_slack_callback_stores_workspace_scoped_to_session_org(monkeypatch):
    stored = []
    monkeypatch.setattr(
        routes, "verify_session", lambda token: {"executor_id": "exe_1", "org_id": "org_1"}
    )
    monkeypatch.setattr(
        routes, "exchange_slack_code", lambda code, redirect_uri: {
            "team_id": "T01ABCDEF", "bot_token": "xoxb-abc", "bot_user_id": "B01", "scope": "files:read"
        }
    )
    monkeypatch.setattr(
        routes, "create_slack_workspace", lambda team_id, doc: stored.append((team_id, doc))
    )
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)

    result = routes.slack_callback({"code": "abc"}, authorization="Bearer t")

    assert result == {"team_id": "T01ABCDEF", "org_id": "org_1"}
    assert stored[0][0] == "T01ABCDEF"
    assert stored[0][1]["org_id"] == "org_1"
    assert stored[0][1]["bot_token"] == "xoxb-abc"
