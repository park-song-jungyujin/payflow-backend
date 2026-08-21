"""schema-contract.md §9 — 집행자 에이전트 호출 enqueue (B 소유).

`parsing/enqueue.py`(A)와 같은 형태다: 실제 큐잉은 guards/tasks.py(C 소유, 읽기만
한다)가 하고, 이 모듈은 경로와 페이로드 형태만 고정한다.

호출 시점은 정산 실행(settlement_run) 생성 직후다. `task_id = settlement_run_id`를
쓴다 — 같은 run이 재분석돼도 agent_drafts는 "최신 분석"만 남으면 되고
(agent_sessions가 과거 턴을 따로 보존한다), Cloud Tasks 재시도도 같은 draft를
덮어쓰는 것으로 충분히 멱등하다.
"""

import os

from ..guards.tasks import QueueNotConfigured, enqueue_task

__all__ = ["QueueNotConfigured", "enqueue_executor_analyze"]


def enqueue_executor_analyze(
    settlement_run_id: str, candidate_claims: list[dict], duplicate_groups: list[dict]
) -> None:
    # 대괄호로 읽지 않는다: AGENT_SERVICE_URL 부재는 배포 설정 누락으로 실제
    # 일어날 수 있는 상황이고(parsing/enqueue.py와 같은 이유), 여기서
    # QueueNotConfigured로 명시해야 호출부가 감사 로그를 남기고 정산 실행 자체는
    # 그대로 진행하게 하는 경로를 탄다 — 분석 실패가 배치 생성을 막지 않는다.
    agent_service_url = os.environ.get("AGENT_SERVICE_URL")
    if not agent_service_url:
        raise QueueNotConfigured(
            "AGENT_SERVICE_URL not configured — 집행자 에이전트 서비스 주소가 없다."
        )
    enqueue_task(
        "/agents/executor/analyze",
        {
            "settlement_run_id": settlement_run_id,
            "task_id": settlement_run_id,
            "candidate_claims": candidate_claims,
            "duplicate_groups": duplicate_groups,
        },
        audience=agent_service_url,
    )
