"""schema-contract.md §10 — POST /tasks/notify-settlement-complete.

정산 완료 통보 — payouts/reconcile.py.reconcile()이 claim을 SETTLED로 바꾼
직후 enqueue한다. "정산이 완료됐다"가 메인 메시지이고, 이번 run에서 제외된
물품(사람이 web에서 직접 뗀 것 + 집행자 에이전트가 자동으로 뗀 것 전부)이
있으면 물품명·사유를 같은 메시지에 덧붙인다. test_remind_routes.py와 같은
형태 — TestClient 없이 핸들러를 직접 부르고 모듈 레벨 이름을 monkeypatch한다.
"""

import pytest
from fastapi import HTTPException

from src.ingest import routes
from src.ingest.slack_client import SlackSendPermanent, SlackSendTransient


def _claim(claim_id, receipt_id, recipient_id="rcp_1"):
    return {"claim_id": claim_id, "receipt_id": receipt_id, "recipient_id": recipient_id}


def _recipient_payload(recipient_id="rcp_1", amount_minor=45000, currency="KRW"):
    return {"recipient_id": recipient_id, "amount_minor": amount_minor, "currency": currency}


@pytest.fixture(autouse=True)
def _oidc(monkeypatch):
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})


def _wire(monkeypatch, *, claims, receipts, recipients=None, post_result="1755500000.000100"):
    monkeypatch.setattr(routes, "get_claims_for_run", lambda run_id: claims)
    monkeypatch.setattr(routes, "get_receipts", lambda receipt_ids: receipts)
    monkeypatch.setattr(routes, "get_recipient", lambda rid: (recipients or {}).get(rid))
    monkeypatch.setattr(routes, "get_user_locale", lambda slack_user_id: None)

    sent = []

    def fake_post(*, channel, text, thread_ts=None, blocks=None):
        result = post_result
        if isinstance(result, Exception):
            raise result
        sent.append({"channel": channel, "text": text})
        return result

    audit = []
    monkeypatch.setattr(routes, "post_message", fake_post)
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit.append(kw))
    return sent, audit


def test_missing_run_id_or_recipients_rejected(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        routes.task_notify_settlement_complete({}, authorization="Bearer t")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        routes.task_notify_settlement_complete(
            {"settlement_run_id": "run_1"}, authorization="Bearer t"
        )
    assert exc.value.status_code == 400


def test_settlement_complete_with_no_rejections_sends_amount_only(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {"rct_1": {"items": [{"name": "아메리카노", "amount_minor": 4500}]}}
    recipients = {"rcp_1": {"slack_user_id": "U_CLAIMANT"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    result = routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert result == {"status": "ok", "notified": 1}
    assert len(sent) == 1
    assert "45,000 KRW 정산이 완료되었습니다." == sent[0]["text"]


def test_both_agent_and_human_excluded_items_are_included(monkeypatch):
    """사람이 web에서 사유 없이 직접 뗀 물품도 이제 통보 대상이다 — rejected_by가
    있든 없든 excluded=true면 전부 포함한다."""
    claims = [_claim("clm_1", "rct_1")]
    receipts = {
        "rct_1": {
            "items": [
                {"name": "샴푸", "amount_minor": 8000, "excluded": True},  # 사람이 직접 제외, 사유 없음
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

    result = routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert result == {"status": "ok", "notified": 1}
    text = sent[0]["text"]
    assert "정산이 완료되었습니다" in text
    assert "샴푸: 담당자 검토에 의해 제외됨" in text  # 사유 없음 → 일반 문구로 대체
    assert "케이크: 개인 간식으로 추정" in text


def test_groups_multiple_claims_of_same_recipient_into_one_dm(monkeypatch):
    claims = [_claim("clm_1", "rct_1"), _claim("clm_2", "rct_2")]
    receipts = {
        "rct_1": {
            "items": [
                {"name": "케이크", "excluded": True, "rejected_by": "EXECUTOR", "rejected_reason": "개인 간식"}
            ]
        },
        "rct_2": {
            "items": [{"name": "샴푸", "excluded": True, "rejected_reason": None}],
        },
    }
    recipients = {"rcp_1": {"slack_user_id": "U_CLAIMANT"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    result = routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert result == {"status": "ok", "notified": 1}
    assert len(sent) == 1  # claim 2건, DM은 하나
    assert "케이크" in sent[0]["text"]
    assert "샴푸" in sent[0]["text"]


def test_only_notifies_recipients_passed_in_by_reconcile(monkeypatch):
    """부분 실패 run에서 아직 결과가 안 난 recipient는 reconcile()이 애초에
    recipients 목록에 안 넣어 보낸다 — 여기서 다시 걸러내지 않는다(정상 경로)."""
    claims = [_claim("clm_1", "rct_1", "rcp_1"), _claim("clm_2", "rct_2", "rcp_2")]
    receipts = {"rct_1": {"items": []}, "rct_2": {"items": []}}
    recipients = {
        "rcp_1": {"slack_user_id": "U_1"},
        "rcp_2": {"slack_user_id": "U_2"},
    }
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    result = routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload("rcp_1")]},
        authorization="Bearer t",
    )

    assert result == {"status": "ok", "notified": 1}
    assert [s["channel"] for s in sent] == ["U_1"]


def test_recipient_without_slack_user_id_is_skipped_not_fatal(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {"rct_1": {"items": []}}
    sent, audit = _wire(monkeypatch, claims=claims, receipts=receipts, recipients={})

    result = routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert result == {"status": "ok", "notified": 0}
    assert sent == []
    assert audit[0]["action"] == "SETTLEMENT_COMPLETE_NOTICE_NO_TARGET"


def test_transient_slack_failure_returns_503_but_still_tries_other_recipients(monkeypatch):
    claims = [_claim("clm_1", "rct_1", "rcp_1"), _claim("clm_2", "rct_2", "rcp_2")]
    receipts = {"rct_1": {"items": []}, "rct_2": {"items": []}}
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
        routes.task_notify_settlement_complete(
            {
                "settlement_run_id": "run_1",
                "recipients": [_recipient_payload("rcp_1"), _recipient_payload("rcp_2")],
            },
            authorization="Bearer t",
        )

    assert exc.value.status_code == 503
    assert sent == ["U_2"]  # U_1이 실패해도 U_2는 계속 시도됐다


def test_permanent_slack_failure_does_not_raise_and_continues(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {"rct_1": {"items": []}}
    recipients = {"rcp_1": {"slack_user_id": "U_1"}}
    sent, audit = _wire(
        monkeypatch,
        claims=claims,
        receipts=receipts,
        recipients=recipients,
        post_result=SlackSendPermanent("channel_not_found"),
    )

    result = routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert result == {"status": "ok", "notified": 0}
    assert audit[-1]["action"] == "SETTLEMENT_COMPLETE_NOTICE_SEND_FAILED"


def test_english_locale_translates_rejection_reasons_via_gemma(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {
        "rct_1": {
            "items": [
                {
                    "name": "케이크",
                    "excluded": True,
                    "rejected_by": "EXECUTOR",
                    "rejected_reason": "개인 간식으로 추정",
                }
            ]
        }
    }
    recipients = {"rcp_1": {"slack_user_id": "U_1"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)
    monkeypatch.setattr(routes, "get_user_locale", lambda slack_user_id: "en-US")
    monkeypatch.setattr(
        routes, "translate_lines", lambda lines: ["Cake: presumed personal snack"]
    )

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    text = sent[0]["text"]
    assert "has been completed" in text
    assert "Cake: presumed personal snack" in text
    assert "케이크" not in text


def test_english_locale_with_no_rejections_skips_translation_call(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {"rct_1": {"items": []}}
    recipients = {"rcp_1": {"slack_user_id": "U_1"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)
    monkeypatch.setattr(routes, "get_user_locale", lambda slack_user_id: "en-US")

    def boom(lines):
        raise AssertionError("반려 물품이 없으면 translate_lines를 부를 이유가 없다")

    monkeypatch.setattr(routes, "translate_lines", boom)

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert "has been completed" in sent[0]["text"]


def test_english_locale_falls_back_to_korean_reason_when_translation_fails(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {
        "rct_1": {
            "items": [
                {
                    "name": "케이크",
                    "excluded": True,
                    "rejected_by": "EXECUTOR",
                    "rejected_reason": "개인 간식으로 추정",
                }
            ]
        }
    }
    recipients = {"rcp_1": {"slack_user_id": "U_1"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)
    monkeypatch.setattr(routes, "get_user_locale", lambda slack_user_id: "en-US")
    monkeypatch.setattr(routes, "translate_lines", lambda lines: None)

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    text = sent[0]["text"]
    assert "has been completed" in text  # 헤더는 영어
    assert "케이크: 개인 간식으로 추정" in text  # 번역 실패 시 한국어 사유 폴백


def test_amount_display_formats_non_zero_exponent_currency():
    from src.ingest.routes import _format_amount

    assert _format_amount(45000, "KRW") == "45,000 KRW"
    assert _format_amount(250050, "USD") == "2,500.50 USD"
