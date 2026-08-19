"""schema-contract.md §9 — 청구자 에이전트 호출 enqueue (A 소유).

`ingest/enqueue.py`와 같은 형태다: 실제 큐잉은 guards/tasks.py(C 소유, 읽기만
한다)가 하고, 이 모듈은 경로와 페이로드 형태만 고정한다.

호출 시점은 "영수증 인입 직후"(§9)이고, 구체적으로는 파싱이 PARSED로 확정한
직후다. FAILED·NEEDS_REQUERY에서는 부르지 않는다.
"""

from ..guards.tasks import QueueNotConfigured, enqueue_task

__all__ = ["QueueNotConfigured", "enqueue_claimant_review"]


def enqueue_claimant_review(receipt_id: str) -> None:
    enqueue_task("/agents/claimant/review", {"receipt_id": receipt_id})
