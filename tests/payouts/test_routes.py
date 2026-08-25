"""schema-contract.md §10 — /payouts(승인 토큰 게이트 경유), /tasks/execute-payout
(실제 PayPal 호출 지점), /tasks/reconcile. 라우트 함수를 FastAPI 없이 직접 호출해
분기별 상태 코드와 부수효과 순서를 검증한다."""

import requests as http_requests
import pytest
from fastapi import HTTPException

from src.guards.errors import GuardRejection
from src.payouts import routes
from src.payouts.currency import UnsupportedPayoutCurrency
from src.payouts.reconcile import MissingPayoutBatch, NotExecuting, RunNotFound
from src.payouts.tasks_queue import QueueNotConfigured


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)
    monkeypatch.setattr(routes, "verify_oidc", lambda auth: {})
    monkeypatch.setattr(routes, "enqueue_reconcile", lambda rid: None)
    monkeypatch.setenv("PAYOUT_CURRENCY", "USD")


def _run(**overrides):
    run = {"settlement_run_id": "run_1", "status": "EXECUTING", "total_amount_minor": 1000}
    run.update(overrides)
    return run


# ---- POST /payouts ----


def test_request_payout_unknown_run_returns_404(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: None)
    with pytest.raises(HTTPException) as exc:
        routes.request_payout({"settlement_run_id": "run_1"}, x_approval_token="tok")
    assert exc.value.status_code == 404


def test_request_payout_missing_run_id_returns_404(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        routes.request_payout({}, x_approval_token="tok")
    assert exc.value.status_code == 404


def test_request_payout_token_rejection_propagates_status_and_audits(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(status="APPROVED"))

    def reject(*a):
        raise GuardRejection(403, "invalid approval token")

    monkeypatch.setattr(routes, "verify_and_burn_token", reject)
    audits = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audits.append(kw))

    with pytest.raises(HTTPException) as exc:
        routes.request_payout({"settlement_run_id": "run_1"}, x_approval_token="bad")

    assert exc.value.status_code == 403
    assert audits[0]["action"] == "PAYOUT_REJECTED"


def test_request_payout_success_reserves_monthly_amount_before_enqueue(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(status="APPROVED"))
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000})
    monkeypatch.setattr(routes, "verify_and_burn_token", lambda *a: {"status": "EXECUTING"})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: {"recipient_id": rid, "monthly_paid_minor": 0})
    reserved = []
    monkeypatch.setattr(routes, "increment_recipient_monthly", lambda rid, delta: reserved.append((rid, delta)))
    monkeypatch.setattr(routes, "enqueue_execute_payout", lambda rid: None)

    result = routes.request_payout({"settlement_run_id": "run_1"}, x_approval_token="good")

    assert result == {"settlement_run_id": "run_1", "status": "EXECUTING"}
    assert reserved == [("rcp_1", 1000)]


def test_request_payout_multi_recipient_reserves_each_recipients_own_amount(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(status="APPROVED"))
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000, "rcp_2": 2500})
    monkeypatch.setattr(routes, "verify_and_burn_token", lambda *a: {"status": "EXECUTING"})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: {"recipient_id": rid, "monthly_paid_minor": 0})
    reserved = []
    monkeypatch.setattr(routes, "increment_recipient_monthly", lambda rid, delta: reserved.append((rid, delta)))
    monkeypatch.setattr(routes, "enqueue_execute_payout", lambda rid: None)

    routes.request_payout({"settlement_run_id": "run_1"}, x_approval_token="good")

    assert set(reserved) == {("rcp_1", 1000), ("rcp_2", 2500)}


def test_request_payout_missing_recipient_skips_reservation_without_crash(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(status="APPROVED"))
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_ghost": 1000})
    monkeypatch.setattr(routes, "verify_and_burn_token", lambda *a: {})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: None)
    reserved = []
    monkeypatch.setattr(routes, "increment_recipient_monthly", lambda rid, delta: reserved.append((rid, delta)))
    monkeypatch.setattr(routes, "enqueue_execute_payout", lambda rid: None)

    routes.request_payout({"settlement_run_id": "run_1"}, x_approval_token="good")

    assert reserved == []


def test_request_payout_queue_not_configured_still_returns_200_with_note(monkeypatch):
    """Cloud Tasks 큐가 아직 없는 로컬/데모 환경에서도 승인 게이트는 이미 통과했으니
    500으로 죽이지 않고 note를 붙여 알린다."""
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(status="APPROVED"))
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000})
    monkeypatch.setattr(routes, "verify_and_burn_token", lambda *a: {})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: None)
    monkeypatch.setattr(routes, "increment_recipient_monthly", lambda rid, delta: None)

    def boom(rid):
        raise QueueNotConfigured("CLOUD_TASKS_QUEUE not set")

    monkeypatch.setattr(routes, "enqueue_execute_payout", boom)

    result = routes.request_payout({"settlement_run_id": "run_1"}, x_approval_token="good")

    assert result["status"] == "EXECUTING"
    assert "note" in result


# ---- POST /payouts/{run_id}/retry ----


def test_retry_payout_unknown_run_returns_404(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: None)
    with pytest.raises(HTTPException) as exc:
        routes.retry_payout("run_1", x_approval_token="tok")
    assert exc.value.status_code == 404


def test_retry_payout_token_rejection_propagates_status_and_audits(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(status="FAILED"))

    def reject(*a):
        raise GuardRejection(409, "settlement_run status is FAILED, expected APPROVED")

    monkeypatch.setattr(routes, "verify_and_burn_token", reject)
    audits = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audits.append(kw))

    with pytest.raises(HTTPException) as exc:
        routes.retry_payout("run_1", x_approval_token="stale")

    assert exc.value.status_code == 409
    assert audits[0]["action"] == "PAYOUT_REJECTED"


def test_retry_payout_success_bumps_retry_seq_and_enqueues(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(status="APPROVED", retry_seq=1))
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000})
    monkeypatch.setattr(routes, "verify_and_burn_token", lambda *a: {"status": "EXECUTING"})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: {"recipient_id": "rcp_1", "monthly_paid_minor": 0})
    monkeypatch.setattr(routes, "increment_recipient_monthly", lambda rid, delta: None)
    monkeypatch.setattr(routes, "enqueue_execute_payout", lambda rid: None)
    updated = []
    monkeypatch.setattr(routes, "update_settlement_run", lambda rid, updates: updated.append(updates))

    result = routes.retry_payout("run_1", x_approval_token="good")

    assert result == {"settlement_run_id": "run_1", "status": "EXECUTING", "retry_seq": 2}
    assert updated == [{"retry_seq": 2}]  # 이전 retry_seq=1 -> 2, execute-payout이 이 값을 읽는다


def test_retry_payout_queue_not_configured_still_returns_200_with_note(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(status="APPROVED"))
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000})
    monkeypatch.setattr(routes, "verify_and_burn_token", lambda *a: {})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: None)
    monkeypatch.setattr(routes, "increment_recipient_monthly", lambda rid, delta: None)
    monkeypatch.setattr(routes, "update_settlement_run", lambda rid, updates: None)

    def boom(rid):
        raise QueueNotConfigured("CLOUD_TASKS_QUEUE not set")

    monkeypatch.setattr(routes, "enqueue_execute_payout", boom)

    result = routes.retry_payout("run_1", x_approval_token="good")

    assert result["status"] == "EXECUTING"
    assert "note" in result


# ---- POST /tasks/execute-payout ----


def test_execute_payout_run_not_found_returns_404(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: None)
    with pytest.raises(HTTPException) as exc:
        routes.task_execute_payout({"settlement_run_id": "run_1"}, authorization="Bearer x")
    assert exc.value.status_code == 404


def test_execute_payout_wrong_status_returns_409(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(status="APPROVED"))
    with pytest.raises(HTTPException) as exc:
        routes.task_execute_payout({"settlement_run_id": "run_1"}, authorization="Bearer x")
    assert exc.value.status_code == 409


def test_execute_payout_no_claims_returns_404(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run())
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {})
    with pytest.raises(HTTPException) as exc:
        routes.task_execute_payout({"settlement_run_id": "run_1"}, authorization="Bearer x")
    assert exc.value.status_code == 404


def test_execute_payout_unknown_recipient_returns_404(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run())
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: None)
    with pytest.raises(HTTPException) as exc:
        routes.task_execute_payout({"settlement_run_id": "run_1"}, authorization="Bearer x")
    assert exc.value.status_code == 404


def test_execute_payout_unsupported_currency_returns_422_and_never_calls_paypal(monkeypatch):
    monkeypatch.setenv("PAYOUT_CURRENCY", "KRW")  # exponent 있어도 Payouts 미지원
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run())
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: {"paypal_email": "a@b.com"})
    called = []
    monkeypatch.setattr(routes, "create_payout", lambda *a, **kw: called.append(1))

    with pytest.raises(HTTPException) as exc:
        routes.task_execute_payout({"settlement_run_id": "run_1"}, authorization="Bearer x")

    assert exc.value.status_code == 422
    assert called == []


def test_execute_payout_paypal_http_error_returns_502(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run())
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: {"paypal_email": "a@b.com"})

    def boom(*a, **kw):
        raise http_requests.HTTPError("PayPal 500")

    monkeypatch.setattr(routes, "create_payout", boom)

    with pytest.raises(HTTPException) as exc:
        routes.task_execute_payout({"settlement_run_id": "run_1"}, authorization="Bearer x")
    assert exc.value.status_code == 502


def test_execute_payout_success_builds_deterministic_ids_and_records_sender_item(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(retry_seq=0))
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: {"paypal_email": "a@b.com"})

    captured_call = {}

    def fake_create_payout(sender_batch_id, items):
        captured_call["sender_batch_id"] = sender_batch_id
        captured_call["items"] = items
        return {"batch_header": {"payout_batch_id": "batch_1"}}

    monkeypatch.setattr(routes, "create_payout", fake_create_payout)
    monkeypatch.setattr(
        routes,
        "get_payout_batch",
        lambda bid: {
            "items": [
                {
                    "payout_item_id": "item_1",
                    "transaction_status": "SUCCESS",
                    "payout_item": {"sender_item_id": "run_1:rcp_1"},
                }
            ]
        },
    )
    saved_items = []
    monkeypatch.setattr(routes, "set_sender_items", lambda rid, items: saved_items.extend(items))
    updated_run = {}
    monkeypatch.setattr(routes, "update_settlement_run", lambda rid, updates: updated_run.update(updates))

    result = routes.task_execute_payout({"settlement_run_id": "run_1"}, authorization="Bearer x")

    assert captured_call["sender_batch_id"] == "run_1"  # retry_seq=0 -> build_payout_ids
    assert captured_call["items"][0]["sender_item_id"] == "run_1:rcp_1"
    assert saved_items[0]["status"] == "SUCCESS"
    assert saved_items[0]["amount_minor"] == 1000
    assert updated_run["payout_batch_id"] == "batch_1"
    assert result["payout_batch_id"] == "batch_1"


def test_execute_payout_multi_recipient_sends_one_batch_with_matched_items(monkeypatch):
    """recipient가 여럿이면 create_payout()은 한 번만 호출되고, 응답 item은
    payout_item.sender_item_id로 각자의 recipient에 정확히 매칭돼야 한다 — 순서에
    기대면 안 된다(PayPal이 순서를 보장하지 않으므로 응답을 일부러 뒤집어 검증)."""
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run(retry_seq=0))
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000, "rcp_2": 2500})
    recipients = {
        "rcp_1": {"paypal_email": "a@b.com"},
        "rcp_2": {"paypal_email": "c@d.com"},
    }
    monkeypatch.setattr(routes, "get_recipient", lambda rid: recipients[rid])

    calls = []
    monkeypatch.setattr(
        routes,
        "create_payout",
        lambda sender_batch_id, items: calls.append((sender_batch_id, items))
        or {"batch_header": {"payout_batch_id": "batch_1"}},
    )
    monkeypatch.setattr(
        routes,
        "get_payout_batch",
        lambda bid: {
            "items": [
                # 응답 순서를 요청 순서와 일부러 뒤집는다.
                {
                    "payout_item_id": "item_2",
                    "transaction_status": "SUCCESS",
                    "payout_item": {"sender_item_id": "run_1:rcp_2"},
                },
                {
                    "payout_item_id": "item_1",
                    "transaction_status": "FAILED",
                    "payout_item": {"sender_item_id": "run_1:rcp_1"},
                },
            ]
        },
    )
    saved_items = []
    monkeypatch.setattr(routes, "set_sender_items", lambda rid, items: saved_items.extend(items))
    monkeypatch.setattr(routes, "update_settlement_run", lambda rid, updates: None)

    result = routes.task_execute_payout({"settlement_run_id": "run_1"}, authorization="Bearer x")

    assert len(calls) == 1  # 단일 배치, 아이템 2개
    sender_batch_id, items = calls[0]
    assert sender_batch_id == "run_1"
    assert {i["sender_item_id"] for i in items} == {"run_1:rcp_1", "run_1:rcp_2"}

    by_recipient = {i["recipient_id"]: i for i in saved_items}
    assert by_recipient["rcp_1"]["amount_minor"] == 1000
    assert by_recipient["rcp_1"]["status"] == "FAILED"
    assert by_recipient["rcp_1"]["payout_item_id"] == "item_1"
    assert by_recipient["rcp_2"]["amount_minor"] == 2500
    assert by_recipient["rcp_2"]["status"] == "SUCCESS"
    assert by_recipient["rcp_2"]["payout_item_id"] == "item_2"
    assert len(result["sender_items"]) == 2


def test_execute_payout_unknown_transaction_status_maps_to_other(monkeypatch):
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run())
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: {"paypal_email": "a@b.com"})
    monkeypatch.setattr(
        routes, "create_payout", lambda *a, **kw: {"batch_header": {"payout_batch_id": "batch_1"}}
    )
    monkeypatch.setattr(
        routes,
        "get_payout_batch",
        lambda bid: {
            "items": [
                {
                    "payout_item_id": None,
                    "transaction_status": "WEIRD",
                    "payout_item": {"sender_item_id": "run_1:rcp_1"},
                }
            ]
        },
    )
    saved_items = []
    monkeypatch.setattr(routes, "set_sender_items", lambda rid, items: saved_items.extend(items))
    monkeypatch.setattr(routes, "update_settlement_run", lambda rid, updates: None)

    routes.task_execute_payout({"settlement_run_id": "run_1"}, authorization="Bearer x")

    assert saved_items[0]["status"] == "OTHER"


def test_execute_payout_no_payout_item_detail_defaults_to_pending(monkeypatch):
    """생성 응답에서 payout_batch_id를 못 받으면(드묾) 상세 조회도 못 하니 PENDING으로
    남겨 다음 reconcile 폴링이 잡게 한다."""
    monkeypatch.setattr(routes, "get_settlement_run", lambda rid: _run())
    monkeypatch.setattr(routes, "per_recipient_amounts", lambda run: {"rcp_1": 1000})
    monkeypatch.setattr(routes, "get_recipient", lambda rid: {"paypal_email": "a@b.com"})
    monkeypatch.setattr(routes, "create_payout", lambda *a, **kw: {"batch_header": {}})
    called = []
    monkeypatch.setattr(routes, "get_payout_batch", lambda bid: called.append(bid))
    saved_items = []
    monkeypatch.setattr(routes, "set_sender_items", lambda rid, items: saved_items.extend(items))
    monkeypatch.setattr(routes, "update_settlement_run", lambda rid, updates: None)

    routes.task_execute_payout({"settlement_run_id": "run_1"}, authorization="Bearer x")

    assert called == []  # payout_batch_id가 없으니 상세 조회 자체를 안 함
    assert saved_items[0]["status"] == "PENDING"


# ---- POST /tasks/reconcile ----


def test_task_reconcile_missing_run_id_returns_400():
    with pytest.raises(HTTPException) as exc:
        routes.task_reconcile({}, authorization="Bearer x")
    assert exc.value.status_code == 400


def test_task_reconcile_run_not_found_returns_404(monkeypatch):
    def boom(rid):
        raise RunNotFound(rid)

    monkeypatch.setattr(routes, "reconcile", boom)
    with pytest.raises(HTTPException) as exc:
        routes.task_reconcile({"settlement_run_id": "run_1"}, authorization="Bearer x")
    assert exc.value.status_code == 404


def test_task_reconcile_not_executing_returns_409(monkeypatch):
    def boom(rid):
        raise NotExecuting("APPROVED")

    monkeypatch.setattr(routes, "reconcile", boom)
    with pytest.raises(HTTPException) as exc:
        routes.task_reconcile({"settlement_run_id": "run_1"}, authorization="Bearer x")
    assert exc.value.status_code == 409


def test_task_reconcile_missing_payout_batch_returns_409(monkeypatch):
    def boom(rid):
        raise MissingPayoutBatch(rid)

    monkeypatch.setattr(routes, "reconcile", boom)
    with pytest.raises(HTTPException) as exc:
        routes.task_reconcile({"settlement_run_id": "run_1"}, authorization="Bearer x")
    assert exc.value.status_code == 409


def test_task_reconcile_success_passthrough(monkeypatch):
    monkeypatch.setattr(routes, "reconcile", lambda rid: {"settlement_run_id": rid, "status": "SETTLED"})
    rescheduled = []
    monkeypatch.setattr(routes, "enqueue_reconcile", lambda rid: rescheduled.append(rid))
    result = routes.task_reconcile({"settlement_run_id": "run_1"}, authorization="Bearer x")
    assert result == {"settlement_run_id": "run_1", "status": "SETTLED"}
    assert rescheduled == []  # 종결 상태면 재예약 안 함


def test_task_reconcile_pending_reschedules_itself(monkeypatch):
    monkeypatch.setattr(
        routes,
        "reconcile",
        lambda rid: {"settlement_run_id": rid, "status": "EXECUTING", "pending_items": 1, "reconcile_attempts": 1},
    )
    rescheduled = []
    monkeypatch.setattr(routes, "enqueue_reconcile", lambda rid: rescheduled.append(rid))
    result = routes.task_reconcile({"settlement_run_id": "run_1"}, authorization="Bearer x")
    assert result["pending_items"] == 1
    assert rescheduled == ["run_1"]


def test_task_reconcile_reschedule_failure_does_not_break_response(monkeypatch):
    """reconcile 결과 자체는 이미 확정됐으니, 재예약 큐잉이 실패해도 응답은 그대로 나가야 한다."""
    monkeypatch.setattr(
        routes,
        "reconcile",
        lambda rid: {"settlement_run_id": rid, "status": "EXECUTING", "pending_items": 1, "reconcile_attempts": 1},
    )

    def boom(rid):
        raise QueueNotConfigured("CLOUD_TASKS_QUEUE not set")

    monkeypatch.setattr(routes, "enqueue_reconcile", boom)
    audits = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audits.append(kw))

    result = routes.task_reconcile({"settlement_run_id": "run_1"}, authorization="Bearer x")

    assert result["pending_items"] == 1
    assert audits[0]["action"] == "RECONCILE_ENQUEUE_FAILED"


# ---- POST /tasks/sweep-reconcile ----


def test_sweep_reconcile_calls_reconcile_for_each_executing_run(monkeypatch):
    monkeypatch.setattr(
        routes,
        "list_executing_settlement_runs",
        lambda: [{"settlement_run_id": "run_1"}, {"settlement_run_id": "run_2"}],
    )
    called = []
    monkeypatch.setattr(
        routes,
        "reconcile",
        lambda rid: called.append(rid) or {"settlement_run_id": rid, "status": "SETTLED"},
    )

    result = routes.task_sweep_reconcile(authorization="Bearer x")

    assert called == ["run_1", "run_2"]
    assert result["swept"] == 2
    assert result["results"] == [
        {"settlement_run_id": "run_1", "status": "SETTLED"},
        {"settlement_run_id": "run_2", "status": "SETTLED"},
    ]


def test_sweep_reconcile_no_executing_runs_returns_empty(monkeypatch):
    monkeypatch.setattr(routes, "list_executing_settlement_runs", lambda: [])
    result = routes.task_sweep_reconcile(authorization="Bearer x")
    assert result == {"swept": 0, "results": []}


def test_sweep_reconcile_one_run_failing_does_not_break_others(monkeypatch):
    monkeypatch.setattr(
        routes,
        "list_executing_settlement_runs",
        lambda: [{"settlement_run_id": "run_1"}, {"settlement_run_id": "run_2"}],
    )

    def flaky(rid):
        if rid == "run_1":
            raise MissingPayoutBatch(rid)
        return {"settlement_run_id": rid, "status": "SETTLED"}

    monkeypatch.setattr(routes, "reconcile", flaky)

    result = routes.task_sweep_reconcile(authorization="Bearer x")

    assert result["swept"] == 2
    assert result["results"][0] == {"settlement_run_id": "run_1", "error": "MissingPayoutBatch"}
    assert result["results"][1] == {"settlement_run_id": "run_2", "status": "SETTLED"}


def test_sweep_reconcile_requires_oidc(monkeypatch):
    from src.guards.oidc import verify_oidc as real_verify_oidc

    monkeypatch.setattr(routes, "verify_oidc", real_verify_oidc)
    monkeypatch.setattr(routes, "list_executing_settlement_runs", lambda: [])
    with pytest.raises(HTTPException) as exc:
        routes.task_sweep_reconcile(authorization="")
    assert exc.value.status_code == 401
