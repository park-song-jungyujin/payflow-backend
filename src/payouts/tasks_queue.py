"""payout 실행 태스크 enqueue. 공용 로직은 `guards/tasks.py` 참조."""

import os

from ..guards.tasks import QueueNotConfigured, enqueue_task

__all__ = ["QueueNotConfigured", "enqueue_execute_payout", "enqueue_reconcile"]


def enqueue_execute_payout(run_id: str) -> None:
    enqueue_task("/tasks/execute-payout", {"settlement_run_id": run_id})


def enqueue_reconcile(run_id: str) -> None:
    """schema-contract.md §8 — RECONCILE_DELAY_SECONDS 뒤에 /tasks/reconcile을 예약한다.
    task_reconcile()이 미종결이면 이 함수를 다시 불러 스스로 재예약한다."""
    delay_seconds = int(os.environ.get("RECONCILE_DELAY_SECONDS", "30"))
    enqueue_task("/tasks/reconcile", {"settlement_run_id": run_id}, delay_seconds=delay_seconds)
