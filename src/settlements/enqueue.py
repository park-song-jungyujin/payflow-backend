"""schema-contract.md §9 — 집행자 에이전트 호출 enqueue (B 소유).

`parsing/enqueue.py`(A)와 같은 형태다: 실제 큐잉은 guards/tasks.py(C 소유, 읽기만
한다)가 하고, 이 모듈은 경로와 페이로드 형태만 고정한다.

호출 시점은 정산 실행(settlement_run) 생성 직후다. `task_id`는 순수
`settlement_run_id`가 아니라 `executor_draft_task_id(run_id)`("EXECUTOR:{run_id}")
를 쓴다 — `guards/agent_drafts.py`는 Firestore 문서 ID로 `task_id`만 쓰고 `agent`는
섞지 않는다. 순수 run_id를 쓰면, 나중에 안전 확인 에이전트(C)가 같은 run_id를
task_id로 써서 `/agents/safety/report`를 부르는 순간 같은 문서를 서로 덮어쓴다.
agent별로 네임스페이스해 이 충돌을 원천 차단한다 — agent 쪽(payflow-agent)은
받은 task_id를 그대로 되돌려 쓰기만 해서 이 변경에 영향받지 않는다.

같은 run이 재분석돼도 agent_drafts는 "최신 분석"만 남으면 되고(agent_sessions가
과거 턴을 따로 보존한다), Cloud Tasks 재시도도 같은 draft를 덮어쓰는 것으로
충분히 멱등하다 — 네임스페이스를 붙여도 이 멱등성은 그대로 유지된다.
"""

import os

from ..guards.tasks import QueueNotConfigured, enqueue_task

__all__ = ["QueueNotConfigured", "enqueue_executor_analyze", "executor_draft_task_id"]


def executor_draft_task_id(settlement_run_id: str) -> str:
    """agent_drafts 문서 ID. 쓰기(enqueue_executor_analyze)와 읽기
    (store.get_agent_draft 호출부)가 반드시 이 함수를 통해 같은 값을 써야 한다."""
    return f"EXECUTOR:{settlement_run_id}"


def enqueue_executor_analyze(
    settlement_run_id: str,
    candidate_claims: list[dict],
    duplicate_groups: list[dict],
    exact_duplicate_groups: list[dict],
    org_id: str,
    force_reanalyze: bool = False,
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
            "task_id": executor_draft_task_id(settlement_run_id),
            "candidate_claims": candidate_claims,
            "duplicate_groups": duplicate_groups,
            "exact_duplicate_groups": exact_duplicate_groups,
            # agent_sessions 스코핑 키 — tiered-memory-review.html §8 Phase 2.
            "org_id": org_id,
            # web "재시도" 버튼 전용 — 이미 CLOSED된 세션도 강제로 재분석하게 한다
            # (agent/main.py executor_analyze의 CLOSED 가드가 이 값을 본다).
            "force_reanalyze": force_reanalyze,
        },
        audience=agent_service_url,
    )
