"""schema-contract.md §10 — GET /settlements, POST /settlements/runs,
GET /settlements/runs/{run_id}, GET /settlements/runs/{run_id}/export.

GET /settlements, POST /settlements/runs, GET /settlements/runs/{run_id}는 B 담당
로직(결정론적 매칭, 검증 호출, 자연어 필터 → SettlementFilter)이 아직 없어
src/matching/select_claims_for_run의 TEMP 버전(필터링만, PayPal 원장 대조·이미지
검증 없음)을 쓴다 — A/C의 E2E 테스트가 실제 claims로 배치를 만들 수 있게 최소한만
채웠다. TODO(B): src/matching/ 안쪽을 결정론적 매칭·검증 결과 반영으로 교체한다.
"""

import os
from datetime import UTC, datetime
from io import BytesIO

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from ulid import ULID

from ..matching import select_claims_for_run
from ..payouts.store import (
    create_settlement_run,
    get_settlement_run,
    link_claims_to_run,
    list_settlement_runs,
)
from ..schemas.models import SettlementFilter
from .export import RunNotFound, build_settlement_export

router = APIRouter()


def _public_run(run: dict) -> dict:
    """schema-contract.md §6 나가는 필드 최소화 — 토큰 해시는 web으로 내보내지 않는다."""
    return {k: v for k, v in run.items() if k != "approval_token_hash"}


@router.get("/settlements")
def list_settlements():
    return {"settlement_runs": [_public_run(r) for r in list_settlement_runs()]}


@router.post("/settlements/runs")
def create_settlement_run_route(body: dict | None = None):
    filter = SettlementFilter(**(body or {}).get("filter", {}))
    claims = select_claims_for_run(filter)

    now = datetime.now(UTC)
    run_id = f"run_{now:%y%m%d}_{str(ULID())[:12]}"
    doc = {
        "settlement_run_id": run_id,
        "filter": filter.model_dump(mode="json"),
        "base_currency": os.environ.get("PAYOUT_CURRENCY", "KRW"),
        # TEMP(B): 여기선 0으로 둔다 — guards/routes.py._lock_fx_and_total이
        # approve 시점에 get_claims_for_run으로 다시 계산한다.
        "total_amount_minor": 0,
        "fx_rates": {},
        "fx_locked_at": None,
        "approval_amount_hash": None,
        "approval_token_hash": None,
        "approval_token_expires_at": None,
        "approval_token_used_at": None,
        "approved_by": None,
        "approved_at": None,
        "retry_seq": 0,
        "status": "DRAFT",
        "created_at": now,
        "updated_at": now,
    }
    create_settlement_run(run_id, doc)
    link_claims_to_run(run_id, [c["claim_id"] for c in claims])
    return _public_run(doc)


@router.get("/settlements/runs/{run_id}")
def get_settlement_run_route(run_id: str):
    run = get_settlement_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    return _public_run(run)


@router.get("/settlements/runs/{run_id}/export")
def export_settlement_run(run_id: str):
    try:
        content = build_settlement_export(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")

    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.xlsx"'},
    )
