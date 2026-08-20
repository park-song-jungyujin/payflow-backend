"""schema-contract.md §3 / money-safety.md 멱등성 — sender_batch_id/sender_item_id
결정론 검증. scripts/test_payout_idempotency.py(라이브 PayPal sandbox 호출)를 CI에서
못 돌리는 대신, 그 스크립트가 실증하려던 규칙 — 동일 입력이면 항상 동일 ID —
을 순수 함수 단위로 고정한다."""

from src.payouts.idempotency import build_payout_ids


def test_first_attempt_uses_run_id_as_sender_batch_id():
    sender_batch_id, sender_item_id = build_payout_ids("run_1", "rcp_1", retry_seq=0)
    assert sender_batch_id == "run_1"
    assert sender_item_id == "run_1:rcp_1"


def test_retry_gets_distinct_ids_from_first_attempt():
    first = build_payout_ids("run_1", "rcp_1", retry_seq=0)
    retry = build_payout_ids("run_1", "rcp_1", retry_seq=1)
    assert first != retry


def test_same_inputs_always_produce_same_ids():
    """PayPal-Request-Id 멱등성의 전제 — 재시도가 새 ID를 만들면 이중 지급이 난다."""
    a = build_payout_ids("run_1", "rcp_1", retry_seq=0)
    b = build_payout_ids("run_1", "rcp_1", retry_seq=0)
    assert a == b


def test_different_recipients_get_different_sender_item_ids_same_batch():
    a = build_payout_ids("run_1", "rcp_1", retry_seq=0)
    b = build_payout_ids("run_1", "rcp_2", retry_seq=0)
    assert a[0] == b[0] == "run_1"  # 같은 배치
    assert a[1] != b[1]  # 항목은 갈린다


def test_retry_sequence_is_embedded_in_both_ids():
    sender_batch_id, sender_item_id = build_payout_ids("run_1", "rcp_1", retry_seq=2)
    assert sender_batch_id == "run_1:r2"
    assert sender_item_id == "run_1:r2:rcp_1"
