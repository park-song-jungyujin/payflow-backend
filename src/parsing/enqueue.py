"""schema-contract.md §9 — 청구자 에이전트 호출 enqueue (A 소유).

`ingest/enqueue.py`와 같은 형태다: 실제 큐잉은 guards/tasks.py(C 소유, 읽기만
한다)가 하고, 이 모듈은 경로와 페이로드 형태만 고정한다.

호출 시점은 "영수증 인입 직후"(§9)이고, 구체적으로는 파싱이 PARSED로 확정한
직후다. FAILED·NEEDS_REQUERY에서는 부르지 않는다.
"""

import os

from ..guards.tasks import QueueNotConfigured, enqueue_task

__all__ = ["QueueNotConfigured", "enqueue_claimant_review"]


def enqueue_claimant_review(receipt_id: str) -> None:
    # 대괄호로 읽지 않는다: AGENT_SERVICE_URL 부재는 배포 설정 누락으로 실제
    # 일어날 수 있는 상황이고, os.environ[...]의 분류 안 된 KeyError로 새 나가면
    # 영수증이 PARSED에서 청구자 에이전트 호출 없이 영영 멈춘다. 여기서
    # QueueNotConfigured로 명시해야 파이프라인이 이미 감사 로그를 남기고
    # PARSED를 유지하도록 잡아주는 경로를 탄다.
    agent_service_url = os.environ.get("AGENT_SERVICE_URL")
    if not agent_service_url:
        raise QueueNotConfigured("AGENT_SERVICE_URL not configured — 청구자 에이전트 서비스 주소가 없다.")
    enqueue_task("/agents/claimant/review", {"receipt_id": receipt_id}, audience=agent_service_url)
