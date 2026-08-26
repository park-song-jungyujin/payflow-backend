"""schema-contract.md §8 — payout 결과 대조. 돈 상태를 실제로 바꾸는 단 하나의
지점이라 상태 전이 경로를 전부 짚는다: 종결 단축, 대기, 최대 시도 초과, 성공/실패
분기, monthly_paid_minor 역산."""

import pytest

from src.payouts import reconcile


class Store:
    """reconcile.py가 참조하는 store 함수를 in-memory dict로 흉내낸다."""

    def __init__(self):
        self.runs = {}
        self.claims = {}
        self.sender_items = {}
        self.recipients = {}
        self.audit_log = []
        self.enqueued = []

    def wire(self, monkeypatch):
        monkeypatch.setattr(reconcile, "get_settlement_run", lambda rid: self.runs.get(rid))
        monkeypatch.setattr(
            reconcile,
            "get_claims_for_run",
            lambda rid: [c for c in self.claims.values() if c["settlement_run_id"] == rid],
        )
        monkeypatch.setattr(reconcile, "get_sender_items", lambda rid: self.sender_items.get(rid, []))
        monkeypatch.setattr(
            reconcile, "set_sender_items", lambda rid, items: self.sender_items.__setitem__(rid, items)
        )
        monkeypatch.setattr(
            reconcile,
            "update_claim",
            lambda cid, updates: self.claims[cid].update(updates),
        )
        monkeypatch.setattr(
            reconcile,
            "update_settlement_run",
            lambda rid, updates: self.runs[rid].update(updates),
        )
        monkeypatch.setattr(reconcile, "get_recipient", lambda rid: self.recipients.get(rid))
        monkeypatch.setattr(
            reconcile,
            "increment_recipient_monthly",
            lambda rid, delta: self.recipients[rid].__setitem__(
                "monthly_paid_minor", self.recipients[rid]["monthly_paid_minor"] + delta
            ),
        )
        monkeypatch.setattr(reconcile, "record_audit_log", lambda **kw: self.audit_log.append(kw))
        monkeypatch.setattr(
            reconcile, "enqueue_task", lambda path, body: self.enqueued.append((path, body))
        )


@pytest.fixture
def store(monkeypatch):
    s = Store()
    s.wire(monkeypatch)
    return s


def _run(run_id="run_1", **overrides):
    run = {"settlement_run_id": run_id, "status": "EXECUTING", "total_amount_minor": 1000}
    run.update(overrides)
    return run


def _claim(claim_id, run_id, recipient_id="rcp_1"):
    return {"claim_id": claim_id, "settlement_run_id": run_id, "recipient_id": recipient_id, "status": "IN_RUN"}


def test_run_not_found_raises(store):
    with pytest.raises(reconcile.RunNotFound):
        reconcile.reconcile("nope")


def test_already_settled_short_circuits_without_touching_items(store, monkeypatch):
    store.runs["run_1"] = _run(status="SETTLED")
    calls = []
    monkeypatch.setattr(reconcile, "get_sender_items", lambda rid: calls.append(rid) or [])

    result = reconcile.reconcile("run_1")

    assert result == {"settlement_run_id": "run_1", "status": "SETTLED", "already_terminal": True}
    assert calls == []


def test_already_failed_short_circuits(store):
    store.runs["run_1"] = _run(status="FAILED")
    result = reconcile.reconcile("run_1")
    assert result["already_terminal"] is True


def test_non_executing_status_raises_not_executing(store):
    store.runs["run_1"] = _run(status="APPROVED")
    with pytest.raises(reconcile.NotExecuting) as exc:
        reconcile.reconcile("run_1")
    assert exc.value.status == "APPROVED"


def test_missing_payout_batch_id_raises(store):
    store.runs["run_1"] = _run(payout_batch_id=None)
    store.sender_items["run_1"] = [{"payout_item_id": "itm_1"}]
    with pytest.raises(reconcile.MissingPayoutBatch):
        reconcile.reconcile("run_1")


def test_no_sender_items_raises_missing_payout_batch(store):
    store.runs["run_1"] = _run(payout_batch_id="batch_1")
    store.sender_items["run_1"] = []
    with pytest.raises(reconcile.MissingPayoutBatch):
        reconcile.reconcile("run_1")


def _wire_payout_batch(monkeypatch, items):
    monkeypatch.setattr(reconcile, "get_payout_batch", lambda batch_id: {"items": items})


def test_all_pending_below_max_attempts_returns_pending_without_finalizing(store, monkeypatch):
    store.runs["run_1"] = _run(payout_batch_id="batch_1", reconcile_attempts=0)
    store.sender_items["run_1"] = [
        {"payout_item_id": "itm_1", "paypal_transaction_status": "PENDING", "status": "PENDING"}
    ]
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "PENDING"}])
    monkeypatch.setenv("PAYOUT_MAX_RECONCILE_ATTEMPTS", "5")

    result = reconcile.reconcile("run_1")

    assert result["pending_items"] == 1
    assert result["reconcile_attempts"] == 1
    assert store.runs["run_1"]["status"] == "EXECUTING"  # 아직 종결 안 됨


def test_pending_at_max_attempts_forces_other_and_finalizes_as_failed(store, monkeypatch):
    store.runs["run_1"] = _run(payout_batch_id="batch_1", reconcile_attempts=4)
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 1000,
            "paypal_transaction_status": "PENDING",
            "status": "PENDING",
        }
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1")
    store.recipients["rcp_1"] = {"monthly_paid_minor": 1000}
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "PENDING"}])
    monkeypatch.setenv("PAYOUT_MAX_RECONCILE_ATTEMPTS", "5")

    result = reconcile.reconcile("run_1")

    assert result["status"] == "FAILED"
    assert store.claims["clm_1"]["status"] == "CONFIRMED"
    # settlement_run_id는 비우지 않는다 — 재발송(retry)이 같은 run으로 다시 보내야
    # 하고, 비우면 list_confirmed_claims()가 다른 run에 중복으로 골라갈 수 있다.
    assert store.claims["clm_1"]["settlement_run_id"] == "run_1"


def test_all_success_settles_run_and_all_claims(store, monkeypatch):
    store.runs["run_1"] = _run(payout_batch_id="batch_1")
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 1000,
            "currency": "KRW",
            "paypal_transaction_status": "SUCCESS",
            "status": "PENDING",
        }
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1")
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "SUCCESS"}])

    result = reconcile.reconcile("run_1")

    assert result["status"] == "SETTLED"
    assert store.runs["run_1"]["status"] == "SETTLED"
    assert store.claims["clm_1"]["status"] == "SETTLED"


def test_all_success_enqueues_settlement_complete_notification(store, monkeypatch):
    store.runs["run_1"] = _run(payout_batch_id="batch_1")
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 1000,
            "currency": "KRW",
            "paypal_transaction_status": "SUCCESS",
            "status": "PENDING",
        }
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1")
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "SUCCESS"}])

    reconcile.reconcile("run_1")

    assert store.enqueued == [
        (
            "/tasks/notify-settlement-complete",
            {
                "settlement_run_id": "run_1",
                "recipients": [{"recipient_id": "rcp_1", "amount_minor": 1000, "currency": "KRW"}],
            },
        )
    ]


def test_notify_enqueue_failure_does_not_break_reconcile(store, monkeypatch):
    """알림은 조언성 부가 기능이다 — enqueue 실패가 정산 종결 처리를 막으면 안 된다."""
    store.runs["run_1"] = _run(payout_batch_id="batch_1")
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 1000,
            "currency": "KRW",
            "paypal_transaction_status": "SUCCESS",
            "status": "PENDING",
        }
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1")
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "SUCCESS"}])

    def boom(path, body):
        raise RuntimeError("CLOUD_TASKS_QUEUE not configured")

    monkeypatch.setattr(reconcile, "enqueue_task", boom)

    result = reconcile.reconcile("run_1")

    assert result["status"] == "SETTLED"
    assert store.audit_log[-1]["action"] == "SETTLEMENT_COMPLETE_NOTIFY_ENQUEUE_FAILED"


def test_all_success_excluded_claim_reverts_to_confirmed_not_settled(store, monkeypatch):
    """청구 전체 반려(excluded=true, settlements/routes.py._apply_claim_exclusion)된
    claim은 애초에 per_recipient_amounts에서 빠져 이 sender_item 합계에 안 들어갔다
    — 돈이 안 나갔으니 recipient가 성공해도 SETTLED가 되면 안 된다."""
    store.runs["run_1"] = _run(payout_batch_id="batch_1")
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 1000,
            "currency": "KRW",
            "paypal_transaction_status": "SUCCESS",
            "status": "PENDING",
        }
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1")
    store.claims["clm_2"] = {**_claim("clm_2", "run_1"), "excluded": True}
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "SUCCESS"}])

    result = reconcile.reconcile("run_1")

    assert result["status"] == "SETTLED"
    assert store.claims["clm_1"]["status"] == "SETTLED"
    assert store.claims["clm_2"]["status"] == "CONFIRMED"
    assert "settled_at" not in store.claims["clm_2"]


def test_partial_failure_excluded_claim_of_successful_recipient_stays_confirmed(store, monkeypatch):
    """부분 실패 분기에서도 마찬가지 — recipient가 SUCCESS라도 그 recipient의
    excluded claim은 SETTLED로 바뀌면 안 된다."""
    store.runs["run_1"] = _run(payout_batch_id="batch_1", total_amount_minor=1000)
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 1000,
            "currency": "KRW",
            "paypal_transaction_status": "SUCCESS",
            "status": "PENDING",
        },
        {
            "payout_item_id": "itm_2",
            "recipient_id": "rcp_2",
            "amount_minor": 500,
            "currency": "KRW",
            "paypal_transaction_status": "FAILED",
            "status": "PENDING",
        },
    ]
    store.claims["clm_1"] = {**_claim("clm_1", "run_1", recipient_id="rcp_1"), "excluded": True}
    store.claims["clm_2"] = _claim("clm_2", "run_1", recipient_id="rcp_2")
    store.recipients["rcp_2"] = {"monthly_paid_minor": 500}
    _wire_payout_batch(
        monkeypatch,
        [
            {"payout_item_id": "itm_1", "transaction_status": "SUCCESS"},
            {"payout_item_id": "itm_2", "transaction_status": "FAILED"},
        ],
    )

    reconcile.reconcile("run_1")

    assert store.claims["clm_1"]["status"] == "CONFIRMED"


def test_failed_item_reverts_claim_to_confirmed_but_keeps_run_id(store, monkeypatch):
    store.runs["run_1"] = _run(payout_batch_id="batch_1")
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 1000,
            "paypal_transaction_status": "FAILED",
            "status": "PENDING",
        }
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1")
    store.recipients["rcp_1"] = {"monthly_paid_minor": 1000}
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "FAILED"}])

    result = reconcile.reconcile("run_1")

    assert result["status"] == "FAILED"
    assert store.claims["clm_1"]["status"] == "CONFIRMED"
    # settlement_run_id는 비우지 않는다 — 재발송(retry)이 같은 run으로 다시 보내야
    # 하고, 비우면 list_confirmed_claims()가 다른 run에 중복으로 골라갈 수 있다.
    assert store.claims["clm_1"]["settlement_run_id"] == "run_1"


def test_failed_run_decrements_monthly_paid_by_item_amount(store, monkeypatch):
    store.runs["run_1"] = _run(payout_batch_id="batch_1", total_amount_minor=1000)
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 1000,
            "paypal_transaction_status": "FAILED",
            "status": "PENDING",
        }
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1")
    store.recipients["rcp_1"] = {"monthly_paid_minor": 4000}
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "FAILED"}])

    reconcile.reconcile("run_1")

    assert store.recipients["rcp_1"]["monthly_paid_minor"] == 3000


def test_multi_recipient_partial_failure_only_rolls_back_failed_recipients_own_amount(store, monkeypatch):
    """run.total_amount_minor(전체 합계)가 아니라 각 recipient의 sender_item.amount_minor만
    깎아야 한다 — 성공한 rcp_1은 그대로 두고, 실패한 rcp_2는 자기 몫(1500)만 되돌린다."""
    store.runs["run_1"] = _run(payout_batch_id="batch_1", total_amount_minor=3500)
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 2000,
            "currency": "KRW",
            "paypal_transaction_status": "SUCCESS",
            "status": "PENDING",
        },
        {
            "payout_item_id": "itm_2",
            "recipient_id": "rcp_2",
            "amount_minor": 1500,
            "currency": "KRW",
            "paypal_transaction_status": "FAILED",
            "status": "PENDING",
        },
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1", recipient_id="rcp_1")
    store.claims["clm_2"] = _claim("clm_2", "run_1", recipient_id="rcp_2")
    store.recipients["rcp_1"] = {"monthly_paid_minor": 2000}
    store.recipients["rcp_2"] = {"monthly_paid_minor": 1500}
    _wire_payout_batch(
        monkeypatch,
        [
            {"payout_item_id": "itm_1", "transaction_status": "SUCCESS"},
            {"payout_item_id": "itm_2", "transaction_status": "FAILED"},
        ],
    )

    reconcile.reconcile("run_1")

    assert store.recipients["rcp_1"]["monthly_paid_minor"] == 2000  # 성공 — 그대로
    assert store.recipients["rcp_2"]["monthly_paid_minor"] == 0  # 실패 — 자기 몫만 롤백
    # 부분 실패에서도 성공한 recipient만 통보 대상이다 — 실패한 rcp_2는 아직
    # 결과가 안 났으니(재발송 대기) 여기서 안 낀다.
    assert store.enqueued == [
        (
            "/tasks/notify-settlement-complete",
            {
                "settlement_run_id": "run_1",
                "recipients": [{"recipient_id": "rcp_1", "amount_minor": 2000, "currency": "KRW"}],
            },
        )
    ]


def test_monthly_paid_rollback_never_goes_negative(store, monkeypatch):
    """already_paid(300)가 item.amount_minor(1000)보다 작으면 min()으로 clamp —
    다른 run이 이미 예약해둔 금액까지 깎아먹으면 안 된다."""
    store.runs["run_1"] = _run(payout_batch_id="batch_1", total_amount_minor=1000)
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 1000,
            "paypal_transaction_status": "FAILED",
            "status": "PENDING",
        }
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1")
    store.recipients["rcp_1"] = {"monthly_paid_minor": 300}
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "FAILED"}])

    reconcile.reconcile("run_1")

    assert store.recipients["rcp_1"]["monthly_paid_minor"] == 0


def test_deleted_recipient_does_not_crash_rollback(store, monkeypatch):
    store.runs["run_1"] = _run(payout_batch_id="batch_1")
    store.sender_items["run_1"] = [
        {
            "payout_item_id": "itm_1",
            "recipient_id": "rcp_ghost",
            "amount_minor": 1000,
            "paypal_transaction_status": "FAILED",
            "status": "PENDING",
        }
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1", recipient_id="rcp_ghost")
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "FAILED"}])

    result = reconcile.reconcile("run_1")

    assert result["status"] == "FAILED"  # 예외 없이 종결됨


def test_unknown_transaction_status_maps_to_other(store, monkeypatch):
    """PayPal이 알려지지 않은 문자열을 돌려줘도 _KNOWN 밖이면 OTHER로 흡수해야
    한다 — 예상 못 한 상태값이 그대로 새면 이후 로직이 깨진다."""
    store.runs["run_1"] = _run(payout_batch_id="batch_1")
    store.sender_items["run_1"] = [
        {"payout_item_id": "itm_1", "recipient_id": "rcp_1", "paypal_transaction_status": "ONHOLD", "status": "PENDING"}
    ]
    store.claims["clm_1"] = _claim("clm_1", "run_1")
    _wire_payout_batch(monkeypatch, [{"payout_item_id": "itm_1", "transaction_status": "ONHOLD"}])

    reconcile.reconcile("run_1")

    assert store.sender_items["run_1"][0]["status"] == "OTHER"


def test_item_missing_from_paypal_detail_keeps_previous_status(store, monkeypatch):
    """PayPal 조회 응답에 해당 payout_item_id가 없으면(드묾) 기존 상태를 그대로
    둔다 — 임의로 실패 처리하지 않는다."""
    store.runs["run_1"] = _run(payout_batch_id="batch_1", reconcile_attempts=0)
    store.sender_items["run_1"] = [
        {"payout_item_id": "itm_1", "paypal_transaction_status": "PENDING", "status": "PENDING"}
    ]
    _wire_payout_batch(monkeypatch, [])  # 상세 응답에 itm_1이 없음
    monkeypatch.setenv("PAYOUT_MAX_RECONCILE_ATTEMPTS", "5")

    result = reconcile.reconcile("run_1")

    assert result["pending_items"] == 1
    assert store.sender_items["run_1"][0]["status"] == "PENDING"
