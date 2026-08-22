"""schema-contract.md §10 — GET /settlements, POST /settlements/runs,
GET /settlements/runs/{run_id}, GET /settlements/runs/{run_id}/export.

POST /settlements/runs 순서(§7 승인 토큰 흐름): 필터로 후보 조회 → 검증(§2) →
탈락분 제외 → 살아남은 후보만 배치에 링크 → 집행자 에이전트 분석 enqueue(§9).
검증이 claims CONFIRMED → IN_RUN 전이보다 먼저 끝나야 한다 — 순서를 뒤집으면
나중에 검증 탈락한 claim이 어느 run에도 속하지 않으면서 IN_RUN을 들고 있는
상태가 생긴다. enqueue는 배치 커밋 이후에 한다 — 실패해도(QueueNotConfigured
등) 정산 실행 자체를 막지 않는다, 분석은 조언일 뿐이다(agent-tools.md).

TODO: `link_claims_to_run`(payouts/store.py)이 아직 TEMP다 — 진짜 CAS 트랜잭션이
아니라 무조건 덮어쓰는 batch write다. schema-contract.md §2 `claims`는 이 전이를
C(`guards/`) 담당으로 명시한다 — B 소유 파일이 아니라 여기서 고치지 않는다.
"""

import os
from datetime import UTC, datetime
from io import BytesIO

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from ulid import ULID

from ..auth.session import verify_session
from ..guards.audit import record_audit_log
from ..matching.candidates import select_claims_for_run
from ..matching.duplicates import find_duplicate_groups
from ..payouts.store import (
    create_settlement_run,
    get_settlement_run,
    link_claims_to_run,
    list_settlement_runs,
)
from ..schemas.models import SettlementFilter
from .enqueue import enqueue_executor_analyze, executor_draft_task_id
from .export import RunNotFound, build_settlement_export
from .store import get_agent_draft
from .verification import verify_candidates

router = APIRouter()

_ACTOR = "api/src/settlements"


def _session_from_header(authorization: str) -> dict:
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
    return verify_session(token)


def _isoformat_or_none(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _claim_summary(claim: dict, receipts: dict) -> dict:
    """schema-contract.md §6 나가는 필드 최소화 — Firestore 원본 claim 문서를
    통째로 agent에 보내지 않는다. 집행자 에이전트가 판단에 쓸 필드만 추린다."""
    receipt = receipts.get(claim["receipt_id"]) or {}
    return {
        "claim_id": claim["claim_id"],
        "recipient_id": claim["recipient_id"],
        "amount_minor": claim["amount_minor"],
        "currency": claim["currency"],
        "account_category_code": claim["account_category_code"],
        "merchant_name": receipt.get("merchant_name"),
        "transaction_date": _isoformat_or_none(receipt.get("transaction_date")),
    }


def _public_run(run: dict) -> dict:
    """schema-contract.md §6 나가는 필드 최소화 — 토큰 해시는 web으로 내보내지 않는다."""
    return {k: v for k, v in run.items() if k != "approval_token_hash"}


def _executor_analysis(run_id: str) -> dict | None:
    """agent_drafts.EXECUTOR를 읽는 유일한 지점. None이면 "아직 분석 안 됨"이지
    "이상 없음"이 아니다 — web이 두 상태를 구분해 렌더링해야 한다(§9,
    plan.md "요약 카드 — 정산 명세 + 위험 알림 렌더").

    TODO: safety_report 필드도 여기 같이 추가한다 — C가 /agents/safety/report
    호출 배선을 만들고 task_id 컨벤션을 정하면(집행자와 같은 충돌 문제가 있어
    executor_draft_task_id처럼 agent_drafts.py, "EXECUTOR:" 로 짐작하지 않는다)."""
    draft = get_agent_draft(executor_draft_task_id(run_id))
    if draft is None:
        return None
    payload = draft["payload"]
    return {
        "anomalies": payload.get("anomalies", []),
        "summary_text": payload.get("summary_text"),
        "created_at": draft.get("created_at"),
    }


@router.get("/settlements")
def list_settlements(authorization: str = Header(default="")):
    session = _session_from_header(authorization)
    return {"settlement_runs": [_public_run(r) for r in list_settlement_runs(session["org_id"])]}


@router.post("/settlements/runs")
def create_settlement_run_route(body: dict | None = None, authorization: str = Header(default="")):
    session = _session_from_header(authorization)
    filter = SettlementFilter(**(body or {}).get("filter", {}))
    candidates = select_claims_for_run(session["org_id"], filter)
    outcome = verify_candidates(candidates)
    claims = outcome["passed_claims"]
    receipts = outcome["receipts"]

    now = datetime.now(UTC)
    run_id = f"run_{now:%y%m%d}_{str(ULID())[:12]}"
    doc = {
        "settlement_run_id": run_id,
        "org_id": session["org_id"],
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

    claim_summaries = [_claim_summary(c, receipts) for c in claims]
    duplicate_groups = find_duplicate_groups(claims, receipts)
    try:
        enqueue_executor_analyze(run_id, claim_summaries, duplicate_groups)
    except Exception as e:
        # parsing/pipeline.py의 CLAIMANT_ENQUEUE_FAILED와 같은 패턴 — 별도 try로
        # 감싸 감사 로그 실패가 이미 커밋된 배치 생성 응답을 가리지 않게 한다.
        try:
            record_audit_log(
                actor=_ACTOR,
                action="EXECUTOR_ENQUEUE_FAILED",
                run_id=run_id,
                reason=str(e),
                after={"settlement_run_id": run_id},
            )
        except Exception:
            pass

    return _public_run(doc)


@router.get("/settlements/runs/{run_id}")
def get_settlement_run_route(run_id: str, authorization: str = Header(default="")):
    session = _session_from_header(authorization)
    run = get_settlement_run(run_id)
    if run is None or run.get("org_id") != session["org_id"]:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    public = _public_run(run)
    public["executor_analysis"] = _executor_analysis(run_id)
    return public


@router.get("/settlements/runs/{run_id}/export")
def export_settlement_run(run_id: str, authorization: str = Header(default="")):
    session = _session_from_header(authorization)
    run = get_settlement_run(run_id)
    if run is None or run.get("org_id") != session["org_id"]:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    try:
        content = build_settlement_export(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")

    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.xlsx"'},
    )
