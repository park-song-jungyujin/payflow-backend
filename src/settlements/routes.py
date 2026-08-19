"""schema-contract.md §10 — GET /settlements, POST /settlements/runs,
GET /settlements/runs/{run_id}, GET /settlements/runs/{run_id}/export.

GET /settlements, POST /settlements/runs, GET /settlements/runs/{run_id}는 B 담당
로직(결정론적 매칭, 검증 호출, 자연어 필터 → SettlementFilter)이 아직 없어 하드코딩
스텁이다 — A/C의 E2E 테스트가 스키마만 맞는 응답을 받을 수 있게 최소한만 채웠다.
TODO(B): src/matching/으로 실제 매칭 붙이고, 후보 claim 필터링·검증 결과 반영으로
교체한다.
"""

import os
from datetime import UTC, datetime
from io import BytesIO

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from ulid import ULID

from ..payouts.store import create_settlement_run, get_settlement_run, list_settlement_runs
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
    now = datetime.now(UTC)
    run_id = f"run_{now:%y%m%d}_{str(ULID())[:12]}"
    doc = {
        "settlement_run_id": run_id,
        "filter": (body or {}).get("filter", {}),
        "base_currency": os.environ.get("PAYOUT_CURRENCY", "KRW"),
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
