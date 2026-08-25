"""schema-contract.md §9 — 안전 확인 에이전트 호출 enqueue (C 소유).

enqueue.py(B, 집행자)와 같은 형태다: 실제 큐잉은 guards/tasks.py가 하고, 이 모듈은
경로와 페이로드 형태만 고정한다.

호출 시점은 정산 실행(settlement_run) 생성 직후, 집행자 enqueue와 같은 자리다.
`task_id`는 `safety_draft_task_id(run_id)`("SAFETY:{run_id}")를 쓴다 —
executor_draft_task_id와 같은 이유로, 같은 run_id를 문서 ID로 쓰면 EXECUTOR draft와
서로 덮어쓴다.
"""

import os

from ..guards.tasks import QueueNotConfigured, enqueue_task

__all__ = ["QueueNotConfigured", "enqueue_safety_report", "safety_draft_task_id"]


def safety_draft_task_id(settlement_run_id: str) -> str:
    """agent_drafts 문서 ID. 쓰기(enqueue_safety_report)와 읽기(store.get_agent_draft
    호출부)가 반드시 이 함수를 통해 같은 값을 써야 한다."""
    return f"SAFETY:{settlement_run_id}"


def enqueue_safety_report(settlement_run_id: str, settlement_run_snapshot: dict) -> None:
    agent_service_url = os.environ.get("AGENT_SERVICE_URL")
    if not agent_service_url:
        raise QueueNotConfigured(
            "AGENT_SERVICE_URL not configured — 안전 확인 에이전트 서비스 주소가 없다."
        )
    enqueue_task(
        "/agents/safety/report",
        {
            "settlement_run_id": settlement_run_id,
            "task_id": safety_draft_task_id(settlement_run_id),
            "settlement_run_snapshot": settlement_run_snapshot,
        },
        audience=agent_service_url,
    )
