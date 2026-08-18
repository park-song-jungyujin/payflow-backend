"""schema-contract.md §10 — 파싱 태스크 enqueue (A 소유).

실제 큐잉은 `guards/tasks.py`의 `enqueue_task(path, payload)`가 한다(C 소유,
읽기만 한다). 이 모듈은 경로와 페이로드 형태만 고정하는 얇은 층이다 —
`/tasks/parse-receipt`의 본문 계약이 `{"receipt_id": ...}` 하나라는 걸
호출부 여러 곳에 흩어놓지 않기 위해서다.

`QueueNotConfigured`를 그대로 다시 export한다. 라우트가 이 예외를 잡아 200으로
ack하므로(큐가 없어도 receipts 문서는 남아 수동 재개가 가능하다), 호출부가
guards를 직접 import하지 않고도 잡을 수 있어야 한다.
"""

from ..guards.tasks import QueueNotConfigured, enqueue_task

__all__ = ["QueueNotConfigured", "enqueue_parse_receipt"]


def enqueue_parse_receipt(receipt_id: str) -> None:
    enqueue_task("/tasks/parse-receipt", {"receipt_id": receipt_id})
