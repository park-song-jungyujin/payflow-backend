"""schema-contract.md §3 PayPal 연동 ID — 멱등성의 근거.

동일 (run_id, recipient_id, retry_seq) 입력이면 항상 동일한 sender_batch_id /
sender_item_id를 낸다. 매번 새 UUID를 만들면 재시도 한 번에 이중 지급이 난다
(money-safety.md).
"""


def build_payout_ids(run_id: str, recipient_id: str, retry_seq: int) -> tuple[str, str]:
    if retry_seq == 0:
        return run_id, f"{run_id}:{recipient_id}"
    suffix = f":r{retry_seq}"
    return f"{run_id}{suffix}", f"{run_id}{suffix}:{recipient_id}"
