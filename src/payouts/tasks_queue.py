"""Cloud Tasks enqueue 지점 — schema-contract.md §8 실행 경로.

큐 프로비저닝(Terraform)은 별도 인프라 작업이라 아직 없다. `CLOUD_TASKS_QUEUE`가
비어 있으면 여기서 명시적으로 실패한다 — `/payouts`가 성공한 척하며 조용히 아무
일도 안 하는 걸 막기 위해서다. 데모/테스트에서는 `/tasks/execute-payout`을 직접
호출해 Cloud Tasks가 하는 일을 시뮬레이션한다.
"""

import os


class QueueNotConfigured(RuntimeError):
    pass


def enqueue_execute_payout(run_id: str) -> None:
    queue = os.environ.get("CLOUD_TASKS_QUEUE")
    if not queue:
        raise QueueNotConfigured(
            "CLOUD_TASKS_QUEUE not configured — Cloud Tasks 인프라 미완성. "
            "POST /tasks/execute-payout을 직접 호출해 시뮬레이션한다."
        )
    raise NotImplementedError("Cloud Tasks enqueue — infra Terraform 작업에서 구현 예정")
