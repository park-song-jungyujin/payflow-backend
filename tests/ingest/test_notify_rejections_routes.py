"""schema-contract.md §10 — POST /tasks/notify-claim-rejections.

청구 반려 자동화 마지막 단계 — 승인 시점에 남아 있는(사람이 web에서 안 되돌린)
물품 반려 내역을 청구자에게 Slack DM으로 안내한다. test_remind_routes.py와 같은
형태 — TestClient 없이 핸들러를 직접 부르고 모듈 레벨 이름을 monkeypatch한다.
"""

import pytest
from fastapi import HTTPException

from src.ingest import routes
from src.ingest.slack_client import SlackSendPermanent, SlackSendTransient


def _claim(claim_id, receipt_id, recipient_id="rcp_1"):
    return {"claim_id": claim_id, "receipt_id": receipt_id, "recipient_id": recipient_id}


@pytest.fixture(autouse=True)
def _oidc(monkeypatch):
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})


def _wire(monkeypatch, *, claims, receipts, recipients=None, post_result="1755500000.000100"):
    monkeypatch.setattr(routes, "get_claims_for_run", lambda run_id: claims)
    monkeypatch.setattr(routes, "get_receipts", lambda receipt_ids: receipts)
    monkeypatch.setattr(routes, "get_recipient", lambda rid: (recipients or {}).get(rid))
    monkeypatch.setattr(routes, "get_user_locale", lambda slack_user_id: None)

    sent = []
    audit = []

    def fake_post(*, channel, text, thread_ts=None, blocks=None):
        result = post_result
        if isinstance(result, Exception):
            raise result
        sent.append({"channel": channel, "text": text})
        return result

    monkeypatch.setattr(routes, "post_message", fake_post)
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit.append(kw))
    return sent, audit


def test_missing_run_id_rejected(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        routes.task_notify_claim_rejections({}, authorization="Bearer t")
    assert exc.value.status_code == 400


def test_no_rejected_items_is_a_silent_no_op(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {"rct_1": {"items": [{"name": "아메리카노", "amount_minor": 4500}]}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts)

    result = routes.task_notify_claim_rejections(
        {"settlement_run_id": "run_1"}, authorization="Bearer t"
    )

    assert result == {"status": "ok", "notified": 0}
    assert sent == []


def test_only_executor_rejected_items_trigger_notification(monkeypatch):
    """excluded=True인데 rejected_by가 없는(사람이 web에서 직접 뗀) 물품은
    이 자동 알림 대상이 아니다 — 이 기능은 집행자 에이전트의 자동 반려 전용이다."""
    claims = [_claim("clm_1", "rct_1")]
    receipts = {
        "rct_1": {
            "items": [
                {"name": "샴푸", "amount_minor": 8000, "excluded": True},  # 사람이 직접 제외
                {
                    "name": "케이크",
                    "amount_minor": 5500,
                    "excluded": True,
                    "rejected_by": "EXECUTOR",
                    "rejected_reason": "개인 간식으로 추정",
                },
            ]
        }
    }
    recipients = {"rcp_1": {"slack_user_id": "U_CLAIMANT"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    result = routes.task_notify_claim_rejections(
        {"settlement_run_id": "run_1"}, authorization="Bearer t"
    )

    assert result == {"status": "ok", "notified": 1}
    assert len(sent) == 1
    assert "케이크" in sent[0]["text"]
    assert "개인 간식으로 추정" in sent[0]["text"]
    assert "샴푸" not in sent[0]["text"]


def test_groups_multiple_claims_of_same_recipient_into_one_dm(monkeypatch):
    claims = [_claim("clm_1", "rct_1"), _claim("clm_2", "rct_2")]
    receipts = {
        "rct_1": {
            "items": [
                {"name": "케이크", "rejected_by": "EXECUTOR", "rejected_reason": "개인 간식"}
            ]
        },
        "rct_2": {
            "items": [
                {"name": "샴푸", "rejected_by": "EXECUTOR", "rejected_reason": "개인 생필품"}
            ]
        },
    }
    recipients = {"rcp_1": {"slack_user_id": "U_CLAIMANT"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    result = routes.task_notify_claim_rejections(
        {"settlement_run_id": "run_1"}, authorization="Bearer t"
    )

    assert result == {"status": "ok", "notified": 1}
    assert len(sent) == 1  # claim 2건, DM은 하나
    assert "케이크" in sent[0]["text"]
    assert "샴푸" in sent[0]["text"]


def test_recipient_without_slack_user_id_is_skipped_not_fatal(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {
        "rct_1": {"items": [{"name": "케이크", "rejected_by": "EXECUTOR", "rejected_reason": "x"}]}
    }
    sent, audit = _wire(monkeypatch, claims=claims, receipts=receipts, recipients={})

    result = routes.task_notify_claim_rejections(
        {"settlement_run_id": "run_1"}, authorization="Bearer t"
    )

    assert result == {"status": "ok", "notified": 0}
    assert sent == []
    assert audit[0]["action"] == "CLAIM_REJECTION_NOTICE_NO_TARGET"


def test_transient_slack_failure_returns_503_but_still_tries_other_recipients(monkeypatch):
    claims = [_claim("clm_1", "rct_1", "rcp_1"), _claim("clm_2", "rct_2", "rcp_2")]
    receipts = {
        "rct_1": {"items": [{"name": "a", "rejected_by": "EXECUTOR", "rejected_reason": "x"}]},
        "rct_2": {"items": [{"name": "b", "rejected_by": "EXECUTOR", "rejected_reason": "y"}]},
    }
    recipients = {
        "rcp_1": {"slack_user_id": "U_1"},
        "rcp_2": {"slack_user_id": "U_2"},
    }
    monkeypatch.setattr(routes, "get_claims_for_run", lambda run_id: claims)
    monkeypatch.setattr(routes, "get_receipts", lambda receipt_ids: receipts)
    monkeypatch.setattr(routes, "get_recipient", lambda rid: recipients.get(rid))
    monkeypatch.setattr(routes, "get_user_locale", lambda slack_user_id: None)
    audit = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit.append(kw))

    sent = []

    def flaky_post(*, channel, text, thread_ts=None, blocks=None):
        if channel == "U_1":
            raise SlackSendTransient("Slack returned 503")
        sent.append(channel)
        return "ts"

    monkeypatch.setattr(routes, "post_message", flaky_post)

    with pytest.raises(HTTPException) as exc:
        routes.task_notify_claim_rejections({"settlement_run_id": "run_1"}, authorization="Bearer t")

    assert exc.value.status_code == 503
    assert sent == ["U_2"]  # U_1이 실패해도 U_2는 계속 시도됐다


def test_permanent_slack_failure_does_not_raise_and_continues(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {
        "rct_1": {"items": [{"name": "a", "rejected_by": "EXECUTOR", "rejected_reason": "x"}]}
    }
    recipients = {"rcp_1": {"slack_user_id": "U_1"}}
    sent, audit = _wire(
        monkeypatch,
        claims=claims,
        receipts=receipts,
        recipients=recipients,
        post_result=SlackSendPermanent("channel_not_found"),
    )

    result = routes.task_notify_claim_rejections(
        {"settlement_run_id": "run_1"}, authorization="Bearer t"
    )

    assert result == {"status": "ok", "notified": 0}
    assert audit[-1]["action"] == "CLAIM_REJECTION_NOTICE_SEND_FAILED"


def test_english_locale_translates_via_gemma(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {
        "rct_1": {
            "items": [{"name": "케이크", "rejected_by": "EXECUTOR", "rejected_reason": "개인 간식으로 추정"}]
        }
    }
    recipients = {"rcp_1": {"slack_user_id": "U_1"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)
    monkeypatch.setattr(routes, "get_user_locale", lambda slack_user_id: "en-US")
    monkeypatch.setattr(
        routes, "translate_lines", lambda lines: ["Cake: presumed personal snack"]
    )

    routes.task_notify_claim_rejections({"settlement_run_id": "run_1"}, authorization="Bearer t")

    assert "Cake: presumed personal snack" in sent[0]["text"]
    assert "케이크" not in sent[0]["text"]


def test_english_locale_falls_back_to_korean_when_translation_fails(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {
        "rct_1": {
            "items": [{"name": "케이크", "rejected_by": "EXECUTOR", "rejected_reason": "개인 간식으로 추정"}]
        }
    }
    recipients = {"rcp_1": {"slack_user_id": "U_1"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)
    monkeypatch.setattr(routes, "get_user_locale", lambda slack_user_id: "en-US")
    monkeypatch.setattr(routes, "translate_lines", lambda lines: None)

    routes.task_notify_claim_rejections({"settlement_run_id": "run_1"}, authorization="Bearer t")

    assert "케이크" in sent[0]["text"]
