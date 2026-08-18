"""schema-contract.md §10 — 파싱 태스크 enqueue 경계 (A 소유).

**아직 큐에 넣지 않는다.** payouts/tasks_queue.py는 /tasks/execute-payout URL이
박혀 있고 C 소유라 고칠 수 없어서, C에게 enqueue_task(path, payload) 형태로
일반화해 src/shared/로 빼달라고 요청해 둔 상태다. 도착하면 이 파일의 함수 본문만
그 호출로 바꾼다 — 라우트와 테스트는 손대지 않는다.

그때까지는 QueueNotConfigured를 던진다. 라우트가 이 예외를 잡아 200으로 ack하고
감사 로그를 남기므로, 큐가 없다고 Slack 재전송을 유발하지는 않는다. receipts
문서는 이미 남아 있어 수동 재개가 가능하다.
"""


class QueueNotConfigured(RuntimeError):
    pass


def enqueue_parse_receipt(receipt_id: str) -> None:
    raise QueueNotConfigured(
        "공용 enqueue 모듈(src/shared/) 미도착 — POST /tasks/parse-receipt를 "
        f"직접 호출해 시뮬레이션한다. receipt_id={receipt_id}"
    )
