"""정산 배치 XLSX 출력 — 세무사 전달용, 계정과목 컬럼 포함."""

from io import BytesIO

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .export import RunNotFound, build_settlement_export

router = APIRouter()


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
