"""schema-contract.md §9/§10 — Cloud Tasks/agent 전용 OIDC 검증. main.py, payouts,
guards의 /tasks·/agents 라우트가 전부 이 함수 하나에 기댄다."""

import pytest
from fastapi import HTTPException

from src.guards import oidc


def test_missing_bearer_prefix_rejected():
    with pytest.raises(HTTPException) as exc:
        oidc.verify_oidc("not-bearer-format")
    assert exc.value.status_code == 401
    assert "missing bearer token" in exc.value.detail


def test_empty_authorization_header_rejected():
    with pytest.raises(HTTPException) as exc:
        oidc.verify_oidc("")
    assert exc.value.status_code == 401


def test_invalid_token_rejected_with_underlying_reason(monkeypatch):
    def boom(token, request, audience):
        raise ValueError("Token expired")

    monkeypatch.setattr(oidc.id_token, "verify_oauth2_token", boom)
    with pytest.raises(HTTPException) as exc:
        oidc.verify_oidc("Bearer some-token")
    assert exc.value.status_code == 401
    assert "Token expired" in exc.value.detail


def test_valid_token_returns_claims_and_passes_configured_audience(monkeypatch):
    captured = {}

    def fake_verify(token, request, audience):
        captured["token"] = token
        captured["audience"] = audience
        return {"sub": "cloud-tasks-sa"}

    monkeypatch.setattr(oidc.id_token, "verify_oauth2_token", fake_verify)

    claims = oidc.verify_oidc("Bearer real-token")

    assert claims == {"sub": "cloud-tasks-sa"}
    assert captured["token"] == "real-token"
    assert captured["audience"] == "https://api.test.invalid"  # tests/conftest.py OIDC_AUDIENCE
