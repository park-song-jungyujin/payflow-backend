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

from ..guards.audit import record_audit_log
from ..guards.oidc import verify_oidc
from ..matching.candidates import select_claims_for_run
from ..matching.duplicates import find_duplicate_groups, find_exact_duplicate_receipts
from ..payouts.store import (
    create_settlement_run,
    get_claims_for_run,
    get_recipient,
    get_settlement_run,
    link_claims_to_run,
    list_settlement_runs,
)
from ..schemas.models import SettlementFilter
from .enqueue import enqueue_executor_analyze, enqueue_executor_retry_check, executor_draft_task_id
from .export import RunNotFound, build_settlement_export
from .store import get_agent_draft, get_receipts
from .verification import verify_candidates

router = APIRouter()

_ACTOR = "api/src/settlements"

# EXECUTOR_ENQUEUE_FAILED 감사 로그만 있고 재시도가 없으면, 정산 실행 생성
# 도중(예: 이 서비스 재배포로 리비전이 바뀌는 순간) enqueue 호출 자체가 죽은
# 경우 executor_analysis가 영원히 null로 남는다 — 사람이 로그를 뒤져 수동으로
# 재개하기 전까지. ingest/routes.py의 재촉 루프("자기 재예약이 유일한
# 생명선")와 같은 패턴으로, 생성 직후 스스로를 깨우는 확인 태스크를 건다.
_EXECUTOR_RETRY_DELAY_SECONDS = int(os.environ.get("EXECUTOR_RETRY_DELAY_SECONDS", "120"))
_EXECUTOR_RETRY_MAX_ATTEMPTS = int(os.environ.get("EXECUTOR_RETRY_MAX_ATTEMPTS", "3"))


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


def _recipient_display_name(recipient_id: str, cache: dict[str, str]) -> str:
    """export.py와 같은 패턴 — recipient_id당 한 번만 조회한다. web 전용 필드라
    _claim_summary(에이전트 enqueue와 공유)에는 넣지 않는다."""
    if recipient_id not in cache:
        recipient = get_recipient(recipient_id)
        cache[recipient_id] = recipient["display_name"] if recipient else recipient_id
    return cache[recipient_id]


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
        # anomalies_en/summary_text_en은 executor-agent가 새로 채우는 필드다 —
        # 그 전에 쓰인 draft에는 없을 수 있어 기본값을 둔다(schema-contract.md
        # 필드 추가는 항상 nullable/기본값으로, 문서 백필 없이).
        "anomalies_en": payload.get("anomalies_en", []),
        "summary_text_en": payload.get("summary_text_en"),
        "created_at": draft.get("created_at"),
    }


@router.get("/settlements")
def list_settlements():
    name_cache: dict[str, str] = {}
    runs = []
    for r in list_settlement_runs():
        public = _public_run(r)
        recipient_ids = {c["recipient_id"] for c in get_claims_for_run(r["settlement_run_id"])}
        public["recipient_names"] = sorted(
            _recipient_display_name(rid, name_cache) for rid in recipient_ids
        )
        runs.append(public)
    return {"settlement_runs": runs}


@router.post("/settlements/runs")
def create_settlement_run_route(body: dict | None = None):
    filter = SettlementFilter(**(body or {}).get("filter", {}))
    candidates = select_claims_for_run(filter)
    outcome = verify_candidates(candidates)
    claims = outcome["passed_claims"]
    receipts = outcome["receipts"]

    if not claims:
        # 청구 항목 없는 빈 run을 만들지 않는다 — 승인 화면에서 "연결된 청구
        # 항목이 없어 승인할 수 없습니다"로만 끝나는 죽은 run이 계속 쌓이는 것을
        # 막는다. select_claims_for_run이 후보를 걸렀거나 verify_candidates가
        # 전부 탈락시킨 두 경우 모두 여기서 걸린다.
        raise HTTPException(
            status_code=400,
            detail="필터에 해당하는 청구 항목이 없어 정산 실행을 생성할 수 없습니다.",
        )

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

    claim_summaries = [_claim_summary(c, receipts) for c in claims]
    duplicate_groups = find_duplicate_groups(claims, receipts)
    exact_duplicate_groups = find_exact_duplicate_receipts(claims, receipts)
    try:
        enqueue_executor_analyze(run_id, claim_summaries, duplicate_groups, exact_duplicate_groups)
    except Exception as e:
        # parsing/pipeline.py의 CLAIMANT_ENQUEUE_FAILED와 같은 패턴 — 별도 try로
        # 감싸 감사 로그 실패가 이미 커밋된 배치 생성 응답을 가리지 않게 한다.
        _audit_best_effort(
            action="EXECUTOR_ENQUEUE_FAILED",
            run_id=run_id,
            reason=str(e),
            after={"settlement_run_id": run_id},
        )

    # enqueue 호출 자체가 성공해도(Cloud Tasks에 태스크가 만들어져도) agent 쪽
    # 디스패치가 조용히 실패할 수 있다 — 이 시도 하나에만 기대지 않는다. 생성
    # 직후 스스로를 깨우는 확인 태스크를 건다(성공/실패 무관하게 항상).
    _try_enqueue_executor_retry_check(
        run_id, claim_summaries, duplicate_groups, exact_duplicate_groups, attempt=1
    )

    return _public_run(doc)


def _audit_best_effort(**kwargs) -> None:
    """감사 로그 자체가 죽어도(Firestore 장애 등) 호출부 흐름을 막지 않는다."""
    try:
        record_audit_log(actor=_ACTOR, **kwargs)
    except Exception:
        pass


def _try_enqueue_executor_retry_check(
    run_id: str,
    claim_summaries: list[dict],
    duplicate_groups: list[dict],
    exact_duplicate_groups: list[dict],
    *,
    attempt: int,
) -> None:
    """enqueue 실패는 삼킨다 — ingest/routes.py._try_enqueue_remind와 같은 이유다.
    여기서 예외를 올리면 이미 끝난 배치 생성 응답(또는 이전 재시도 호출)이
    가려진다. 여기서 삼킨 실패는 그 자체로 재시도 루프가 끊긴다는 뜻이다 —
    다음 깨어남이 없으므로 EXECUTOR_ANALYSIS_STALLED로 종결되지 않고 그냥
    조용히 멈춘다. 감사 로그의 EXECUTOR_RETRY_ENQUEUE_FAILED가 유일한 단서다."""
    try:
        enqueue_executor_retry_check(
            run_id,
            claim_summaries,
            duplicate_groups,
            exact_duplicate_groups,
            attempt=attempt,
            delay_seconds=_EXECUTOR_RETRY_DELAY_SECONDS,
        )
    except Exception as e:
        _audit_best_effort(
            action="EXECUTOR_RETRY_ENQUEUE_FAILED",
            run_id=run_id,
            reason=str(e),
            after={"settlement_run_id": run_id, "attempt": attempt},
        )


def _run_claims(run_id: str) -> list[dict]:
    """web의 "정산 명세"(plan.md 요약 카드 요건) — 이 run에 링크된 claim별 상세.
    _claim_summary는 이미 집행자 에이전트 enqueue용으로 있던 함수를 그대로
    재사용한다(payflow-frontend/plans/2026-08-21-web-dashboard.md "필요한
    백엔드 변경 (a)")."""
    claims = get_claims_for_run(run_id)
    receipts = get_receipts({c["receipt_id"] for c in claims})
    name_cache: dict[str, str] = {}
    summaries = []
    for c in claims:
        summary = _claim_summary(c, receipts)
        summary["recipient_name"] = _recipient_display_name(c["recipient_id"], name_cache)
        summaries.append(summary)
    return summaries


@router.get("/settlements/runs/{run_id}")
def get_settlement_run_route(run_id: str):
    run = get_settlement_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    public = _public_run(run)
    public["executor_analysis"] = _executor_analysis(run_id)
    public["claims"] = _run_claims(run_id)
    return public


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


@router.post("/tasks/retry-executor-analysis")
def task_retry_executor_analysis(body: dict, authorization: str = Header(default="")):
    """집행자 분석 확인 태스크의 한 번의 깨어남. Cloud Tasks가 부른다.

    ingest/routes.py의 /tasks/remind와 같은 철학 — "자기 재예약이 유일한
    생명선"이다. agent_drafts.EXECUTOR가 이미 있으면(정상적으로 끝났으면)
    아무것도 안 하고 끝낸다. 없으면 재-enqueue하고 스스로를 다시 깨운다.
    시도 상한(_EXECUTOR_RETRY_MAX_ATTEMPTS)에 도달하면 재예약을 멈추고
    EXECUTOR_ANALYSIS_STALLED를 남긴다 — 이게 사람이 볼 수 있는 유일한 종착점이다.

    항상 200이다. 이 태스크 자체의 실패를 Cloud Tasks 재시도에 맡기지 않는다
    — 재시도 손잡이는 이미 이 루프(재예약)가 쥐고 있고, Cloud Tasks 자체
    재시도까지 겹치면 같은 run에 대해 재시도 태스크가 두 갈래로 늘어난다.
    """
    verify_oidc(authorization)

    run_id = body.get("settlement_run_id")
    if not run_id:
        raise HTTPException(status_code=400, detail="settlement_run_id required")
    attempt = body.get("attempt", 1)
    claim_summaries = body.get("candidate_claims", [])
    duplicate_groups = body.get("duplicate_groups", [])
    exact_duplicate_groups = body.get("exact_duplicate_groups", [])

    if get_agent_draft(executor_draft_task_id(run_id)) is not None:
        return {"status": "ok", "reason": "already_analyzed"}

    if attempt >= _EXECUTOR_RETRY_MAX_ATTEMPTS:
        _audit_best_effort(
            action="EXECUTOR_ANALYSIS_STALLED",
            run_id=run_id,
            reason=f"gave up after {attempt} attempts — executor_analysis still missing",
            after={"settlement_run_id": run_id},
        )
        return {"status": "ok", "reason": "gave_up"}

    try:
        enqueue_executor_analyze(run_id, claim_summaries, duplicate_groups, exact_duplicate_groups)
    except Exception as e:
        _audit_best_effort(
            action="EXECUTOR_ENQUEUE_FAILED",
            run_id=run_id,
            reason=str(e),
            after={"settlement_run_id": run_id, "attempt": attempt},
        )

    _try_enqueue_executor_retry_check(
        run_id, claim_summaries, duplicate_groups, exact_duplicate_groups, attempt=attempt + 1
    )
    return {"status": "ok", "reason": "retried"}
