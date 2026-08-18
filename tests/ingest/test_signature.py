"""schema-contract.md §10 — Slack 서명 검증."""

import hashlib
import hmac
import time

import pytest

from src.ingest.signature import SignatureError, verify_slack_signature

SECRET = "test-signing-secret"


def _sign(raw_body: bytes, timestamp: str) -> str:
    basestring = b"v0:" + timestamp.encode() + b":" + raw_body
    digest = hmac.new(SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_valid_signature_passes():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    verify_slack_signature(body, ts, _sign(body, ts))  # 예외가 없으면 통과


def test_tampered_body_fails():
    ts = str(int(time.time()))
    signature = _sign(b'{"amount":1000}', ts)
    with pytest.raises(SignatureError, match="signature mismatch"):
        verify_slack_signature(b'{"amount":9999999}', ts, signature)


def test_stale_timestamp_fails():
    """재전송 공격 방어. 5분을 넘긴 요청은 서명이 맞아도 거부한다."""
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()) - 600)
    with pytest.raises(SignatureError, match="skew"):
        verify_slack_signature(body, ts, _sign(body, ts))


def test_future_timestamp_fails():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()) + 600)
    with pytest.raises(SignatureError, match="skew"):
        verify_slack_signature(body, ts, _sign(body, ts))


def test_missing_headers_fail():
    with pytest.raises(SignatureError, match="missing"):
        verify_slack_signature(b"{}", "", "")


def test_malformed_timestamp_fails():
    with pytest.raises(SignatureError, match="malformed"):
        verify_slack_signature(b"{}", "not-a-number", "v0=deadbeef")
