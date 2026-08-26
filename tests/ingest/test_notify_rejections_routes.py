"""schema-contract.md §10 — POST /tasks/notify-settlement-complete.

정산 완료 통보 — payouts/reconcile.py.reconcile()이 claim을 SETTLED로 바꾼
직후 enqueue한다. "정산이 완료됐다"가 메인 메시지이고, 이번 run에서 제외된
물품(사람이 web에서 직접 뗀 것 + 집행자 에이전트가 자동으로 뗀 것 전부)이
있으면 물품명·사유를 같은 메시지에 덧붙인다. Slack 봇 발송은 전부 영어다
(해커톤 제출 언어 요건) — 헤더는 항상 영어, 항목 사유는 Gemma로 번역하고
번역 실패 시에만 한국어로 폴백한다. test_remind_routes.py와 같은 형태 —
TestClient 없이 핸들러를 직접 부르고 모듈 레벨 이름을 monkeypatch한다.
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
    # 기본값은 "번역 불가"다 — 이 스위트 대부분은 번역 자체가 아니라 어떤 항목이
    # 통보에 포함되는지를 검증한다. 그 결과 한국어 사유 그대로 폴백된 텍스트를
    # 비교한다(_settlement_complete_text의 번역 실패 폴백 경로). 번역 성공 경로는
    # 아래 test_rejection_reasons_translated_via_gemma가 따로 검증한다.
    monkeypatch.setattr(routes, "translate_lines", lambda lines: None)

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
    assert "Your settlement of 45,000 KRW has been completed." == sent[0]["text"]


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
    assert "has been completed" in text
    assert "샴푸: 이 항목이 집행자에 의해 반려되었습니다" in text  # 사유 없음 → 집행자 직접 반려 사실 그대로 안내(번역 실패 폴백)
    assert "케이크: 개인 간식으로 추정" in text


def test_whole_claim_exclusion_is_reported_with_merchant_name(monkeypatch):
    """청구 전체 반려(settlements/routes.py._apply_claim_exclusion, 중복 청구·동일
    영수증 재제출·미래 거래일)도 통보에 포함된다 — 물품 하나가 아니라 영수증
    전체가 빠진 경우라 가맹점명으로 어떤 청구인지 알려준다."""
    claims = [{**_claim("clm_1", "rct_1"), "excluded": True, "rejected_reason": "미래 거래일"}]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "items": []}}
    recipients = {"rcp_1": {"slack_user_id": "U_CLAIMANT"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert "스타벅스 청구 전체: 미래 거래일" in sent[0]["text"]


def test_whole_claim_exclusion_without_merchant_name_falls_back_to_generic_label(monkeypatch):
    claims = [{**_claim("clm_1", "rct_1"), "excluded": True, "rejected_reason": "x"}]
    receipts = {"rct_1": {"items": []}}
    recipients = {"rcp_1": {"slack_user_id": "U_CLAIMANT"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert "가맹점 미상 청구 전체: x" in sent[0]["text"]


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


def test_rejection_reasons_translated_via_gemma(monkeypatch):
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


def test_no_rejections_skips_translation_call(monkeypatch):
    claims = [_claim("clm_1", "rct_1")]
    receipts = {"rct_1": {"items": []}}
    recipients = {"rcp_1": {"slack_user_id": "U_1"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    def boom(lines):
        raise AssertionError("반려 물품이 없으면 translate_lines를 부를 이유가 없다")

    monkeypatch.setattr(routes, "translate_lines", boom)

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert "has been completed" in sent[0]["text"]


def test_translation_failure_falls_back_to_korean_reason(monkeypatch):
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

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    text = sent[0]["text"]
    assert "has been completed" in text  # 헤더는 항상 영어
    assert "케이크: 개인 간식으로 추정" in text  # 번역 실패 시 한국어 사유 폴백


def test_amount_display_formats_non_zero_exponent_currency():
    from src.ingest.routes import _format_amount

    assert _format_amount(45000, "KRW") == "45,000 KRW"
    assert _format_amount(250050, "USD") == "2,500.50 USD"
