"""schema-contract.md §9 — 청구자 에이전트 호출 enqueue (A 소유).

`ingest/enqueue.py`와 같은 형태다: 실제 큐잉은 guards/tasks.py(C 소유, 읽기만
한다)가 하고, 이 모듈은 경로와 페이로드 형태만 고정한다.

호출 시점은 "영수증 인입 직후"(§9)이고, 구체적으로는 파싱이 PARSED로 확정한
직후다. FAILED·NEEDS_REQUERY에서는 부르지 않는다.

**파싱 스냅샷을 태스크 본문에 싣는다.** 청구자 에이전트는 `{receipt_id, task_id}`만
받으면 영수증 내용을 볼 방법이 없다(GCS 접근 없음, Firestore 직접 접근은
agent_sessions 하나만 예외). 안전 확인 에이전트(settlement_run 스냅샷)·집행자
(candidate_claims)와 같은 방식으로, 여기서도 파싱 결과를 본문에 실어 보낸다.
"""

import os

from ..guards.tasks import QueueNotConfigured, enqueue_task

__all__ = ["QueueNotConfigured", "enqueue_claimant_review"]

# §6 최소화 — Firestore 문서를 통째로 보내지 않는다. recipient_id·status·
# slack_*·gcs_*는 에이전트가 알 필요 없는, 판단에 불필요한 식별자다.
_SNAPSHOT_FIELDS = (
    "merchant_name",
    "transaction_date",
    "parsed_amount_minor",
    "currency",
    "account_category_code",
    "parse_confidence",
    "raw_text",
)


def enqueue_claimant_review(receipt_id: str, *, receipt: dict) -> None:
    # 대괄호로 읽지 않는다: AGENT_SERVICE_URL 부재는 배포 설정 누락으로 실제
    # 일어날 수 있는 상황이고, os.environ[...]의 분류 안 된 KeyError로 새 나가면
    # 영수증이 PARSED에서 청구자 에이전트 호출 없이 영영 멈춘다. 여기서
    # QueueNotConfigured로 명시해야 파이프라인이 이미 감사 로그를 남기고
    # PARSED를 유지하도록 잡아주는 경로를 탄다.
    agent_service_url = os.environ.get("AGENT_SERVICE_URL")
    if not agent_service_url:
        raise QueueNotConfigured("AGENT_SERVICE_URL not configured — 청구자 에이전트 서비스 주소가 없다.")
    # task_id는 agent_drafts 문서 ID다(schema-contract.md §9, 925df98 컨벤션) —
    # 에이전트 이름으로 네임스페이스해 다른 에이전트가 같은 id로 같은 문서를
    # 덮어쓰지 않게 한다. receipt_id에서 결정론적으로 나오므로 파싱 재시도로
    # 에이전트가 두 번 불려도 draft 문서는 하나다.
    task_id = f"CLAIMANT:{receipt_id}"
    # 필드가 없으면 키를 빼지 말고 None으로 싣는다 — 에이전트가 "없다"와
    # "안 왔다"를 구분해야 한다.
    payload = {"receipt_id": receipt_id, "task_id": task_id}
    payload.update({field: receipt.get(field) for field in _SNAPSHOT_FIELDS})
    enqueue_task(
        "/agents/claimant/review",
        payload,
        audience=agent_service_url,
    )
