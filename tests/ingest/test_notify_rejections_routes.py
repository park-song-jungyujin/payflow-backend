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

from src.guards import translate as translate_module
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
    # 워크스페이스 조회는 이 스위트가 검증하는 대상이 아니다 — recipient에
    # team_id가 없는 기본 케이스로 두고 get_slack_workspace_by_org가 항상
    # None을 돌려주게 해 실제 Firestore를 안 건드린다.
    monkeypatch.setattr(routes, "get_slack_workspace_by_team", lambda team_id: None)
    monkeypatch.setattr(routes, "get_slack_workspace_by_org", lambda org_id: None)
    # 기본값은 "번역 불가"다 — 이 스위트 대부분은 번역 자체가 아니라 어떤 항목이
    # 통보에 포함되는지를 검증한다. 그 결과 한국어 사유 그대로 폴백된 텍스트를
    # 이 경로는 Gemma를 부르지 않는다 — 물품명은 파싱 시점 번역(name_en),
    # 사유는 집행자 에이전트가 영어로 쓴 값, 대체 문구는 코드 상수다.
    # test_settlement_complete_never_calls_gemma가 그걸 지킨다.

    sent = []

    def fake_post(*, channel, text, thread_ts=None, blocks=None, bot_token=None):
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
                # 사람이 직접 제외, 사유 없음
                {
                    "name": "샴푸",
                    "name_en": "Shampoo",
                    "amount_minor": 8000,
                    "excluded": True,
                },
                {
                    "name": "케이크",
                    "name_en": "Cake",
                    "amount_minor": 5500,
                    "excluded": True,
                    "rejected_by": "EXECUTOR",
                    "rejected_reason": "Presumed personal snack",
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
    # 사유 없음 → 집행자가 직접 반려했다는 사실 자체를 알린다(없는 사유를
    # 지어내지 않는다). 물품명은 파싱 시점에 번역된 name_en을 쓴다.
    assert "Shampoo: This item was rejected by the approver" in text
    assert "Cake: Presumed personal snack" in text


def test_whole_claim_exclusion_is_reported_with_merchant_name(monkeypatch):
    """청구 전체 반려(settlements/routes.py._apply_claim_exclusion, 중복 청구·동일
    영수증 재제출·미래 거래일)도 통보에 포함된다 — 물품 하나가 아니라 영수증
    전체가 빠진 경우라 가맹점명으로 어떤 청구인지 알려준다."""
    claims = [
        {**_claim("clm_1", "rct_1"), "excluded": True, "rejected_reason": "Future transaction date"}
    ]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "merchant_name_en": "Starbucks", "items": []}}
    recipients = {"rcp_1": {"slack_user_id": "U_CLAIMANT"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert "Starbucks (whole claim): Future transaction date" in sent[0]["text"]


def test_whole_claim_exclusion_without_merchant_name_falls_back_to_generic_label(monkeypatch):
    claims = [{**_claim("clm_1", "rct_1"), "excluded": True, "rejected_reason": "x"}]
    receipts = {"rct_1": {"items": []}}
    recipients = {"rcp_1": {"slack_user_id": "U_CLAIMANT"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert "Unknown merchant (whole claim): x" in sent[0]["text"]


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
    monkeypatch.setattr(routes, "get_slack_workspace_by_team", lambda team_id: None)
    monkeypatch.setattr(routes, "get_slack_workspace_by_org", lambda org_id: None)
    audit = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit.append(kw))

    sent = []

    def flaky_post(*, channel, text, thread_ts=None, blocks=None, bot_token=None):
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


def test_settlement_complete_never_calls_gemma(monkeypatch):
    """이 DM에는 번역할 것이 남아 있지 않다 — 물품명은 파싱 시점에 번역된
    name_en, 사유는 집행자 에이전트가 영어로 쓴 값, 사유 없을 때의 대체 문구는
    코드 상수다. 한때 여기서 Gemma를 불렀는데, 실패하면 조용히 한국어로
    폴백해 "- 자카페 쿠키: 이 항목이 집행자에 의해 반려되었습니다" 같은 줄이
    영어 DM 한가운데 섞여 나갔다."""
    def boom(*args, **kwargs):
        raise AssertionError("이 경로는 Gemma를 부르지 않는다")

    # routes는 이제 translate_lines를 import조차 하지 않는다 — 원본 모듈을
    # 패치해 이 요청 경로에서 Gemma가 불리면 즉시 터지게 한다.
    monkeypatch.setattr(translate_module, "translate_lines", boom)
    claims = [_claim("clm_1", "rct_1")]
    receipts = {
        "rct_1": {
            "items": [
                {
                    "name": "자카페 쿠키",
                    "name_en": "Jacafe Cookie",
                    "amount_minor": 3000,
                    "excluded": True,
                }
            ]
        }
    }
    recipients = {"rcp_1": {"slack_user_id": "U_CLAIMANT"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert "Jacafe Cookie: This item was rejected by the approver" in sent[0]["text"]


def test_item_without_name_en_falls_back_to_the_original_name(monkeypatch):
    """번역이 실패했거나 name_en이 생기기 전에 파싱된 영수증 — 빈 칸보다
    한국어 원문이 낫다. 새로 올라오는 영수증에는 name_en이 붙는다."""
    claims = [_claim("clm_1", "rct_1")]
    receipts = {
        "rct_1": {"items": [{"name": "자카페 쿠키", "amount_minor": 3000, "excluded": True}]}
    }
    recipients = {"rcp_1": {"slack_user_id": "U_CLAIMANT"}}
    sent, _ = _wire(monkeypatch, claims=claims, receipts=receipts, recipients=recipients)

    routes.task_notify_settlement_complete(
        {"settlement_run_id": "run_1", "recipients": [_recipient_payload()]},
        authorization="Bearer t",
    )

    assert "자카페 쿠키: This item was rejected by the approver" in sent[0]["text"]


def test_amount_display_formats_non_zero_exponent_currency():
    from src.ingest.routes import _format_amount

    assert _format_amount(45000, "KRW") == "45,000 KRW"
    assert _format_amount(250050, "USD") == "2,500.50 USD"
