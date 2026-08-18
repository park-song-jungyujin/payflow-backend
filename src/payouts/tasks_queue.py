"""payout 실행 태스크 enqueue. 공용 로직은 `guards/tasks.py` 참조."""

from ..guards.tasks import QueueNotConfigured, enqueue_task

__all__ = ["QueueNotConfigured", "enqueue_execute_payout"]


def enqueue_execute_payout(run_id: str) -> None:
    enqueue_task("/tasks/execute-payout", {"settlement_run_id": run_id})
