"""schema-contract.md §10 — POST /tasks/parse-receipt (A 소유).

**Cloud Tasks 전용이다. 공개 금지.** payouts/routes.py의 /tasks/* 와 같은
검증 방식을 쓴다 — guards/oidc.py 한 곳에 모아 셋이 갈라지지 않게 한다.

HTTP 코드가 큐의 재시도 손잡이다:
- 200 → 태스크 종료. PARSED든 FAILED든 결말이 났다는 뜻이다.
- 503 → 재시도. 일시적 실패라 다시 부르면 될 때만 쓴다.
- 404 → 재시도해도 없다. 큐가 포기한다.
"""

from fastapi import APIRouter, Header, HTTPException

from ..guards.oidc import verify_oidc
from .pipeline import parse_receipt
from .slack_files import TransientParseError
from .store import ReceiptNotFound

router = APIRouter()


@router.post("/tasks/parse-receipt")
def task_parse_receipt(body: dict, authorization: str = Header(default="")):
    verify_oidc(authorization)

    receipt_id = body.get("receipt_id")
    if not receipt_id:
        raise HTTPException(status_code=400, detail="receipt_id required")

    try:
        status = parse_receipt(receipt_id)
    except ReceiptNotFound:
        raise HTTPException(status_code=404, detail=f"unknown receipt_id: {receipt_id}")
    except TransientParseError as e:
        # 상태를 안 바꾸고 올라온 실패다. 큐가 다시 부르게 503으로 내린다.
        raise HTTPException(status_code=503, detail=str(e))

    return {"status": status, "receipt_id": receipt_id}
