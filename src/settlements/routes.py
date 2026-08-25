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
from ..matching.duplicates import find_duplicate_groups, find_exact_duplicate_receipts
from ..payouts.store import (
    create_settlement_run,
    get_claim,
    get_claims_for_run,
    get_recipient,
    get_settlement_run,
    link_claims_to_run,
    list_settled_claims,
    list_settlement_runs,
    update_claim,
)
from ..schemas.models import SettlementFilter
from .enqueue import enqueue_executor_analyze, executor_draft_task_id
from .export import RunNotFound, build_settlement_export
from .store import get_agent_draft, get_receipts, set_executor_analysis_status, update_receipt_items
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


def _recipient_display_name(recipient_id: str, cache: dict[str, str]) -> str:
    """export.py와 같은 패턴 — recipient_id당 한 번만 조회한다. web 전용 필드라
    _claim_summary(에이전트 enqueue와 공유)에는 넣지 않는다."""
    if recipient_id not in cache:
        recipient = get_recipient(recipient_id)
        cache[recipient_id] = recipient["display_name"] if recipient else recipient_id
    return cache[recipient_id]


def _executor_analysis(run_id: str) -> dict | None:
    """agent_drafts.EXECUTOR를 읽는 유일한 지점. None이면 "정산 실행 생성 자체가
    실패했거나 아주 옛날 run"이지 진행 상태가 아니다 — 진행 상태는 status 필드로
    구분한다: PROCESSING(집행자 에이전트 enqueue 성공, 분석 대기/진행 중),
    FAILED(enqueue 자체가 실패, store.set_executor_analysis_status), DONE(에이전트가
    submit_settlement_analysis로 최종 결과를 씀). status가 없으면 DONE으로
    기본값 처리한다 — 이 필드를 추가하기 전에 이미 쓰인 draft는 전부 에이전트가
    완료한 분석이었기 때문(schema-contract.md 필드 추가는 기존 문서 백필 없이
    nullable/기본값으로).

    TODO: safety_report 필드도 여기 같이 추가한다 — C가 /agents/safety/report
    호출 배선을 만들고 task_id 컨벤션을 정하면(집행자와 같은 충돌 문제가 있어
    executor_draft_task_id처럼 agent_drafts.py, "EXECUTOR:" 로 짐작하지 않는다)."""
    draft = get_agent_draft(executor_draft_task_id(run_id))
    if draft is None:
        return None
    payload = draft["payload"]
    return {
        "status": payload.get("status", "DONE"),
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
def list_settlements(authorization: str = Header(default="")):
    session = _session_from_header(authorization)
    name_cache: dict[str, str] = {}
    runs = []
    for r in list_settlement_runs(session["org_id"]):
        public = _public_run(r)
        recipient_ids = {c["recipient_id"] for c in get_claims_for_run(r["settlement_run_id"])}
        public["recipient_names"] = sorted(
            _recipient_display_name(rid, name_cache) for rid in recipient_ids
        )
        runs.append(public)
    return {"settlement_runs": runs}


@router.get("/settlements/unsettled-claims")
def list_unsettled_claims(authorization: str = Header(default="")):
    """web 대시보드 왼쪽 파트 — 아직 어떤 정산 실행에도 안 들어간 확정 청구
    목록. select_claims_for_run(필터 없음)이 이미 "정산 대상 CONFIRMED claims
    전체"를 준다 — 여기서 검증(verify_candidates)은 돌리지 않는다. 검증은
    Gemini 단발 호출이라 비용·지연이 있고, 이건 실행을 만드는 게 아니라
    조회만 하는 화면이라 필요 없다 — 실제 검증은 정산 실행을 만들 때 한다."""
    session = _session_from_header(authorization)
    candidates = select_claims_for_run(session["org_id"], SettlementFilter())
    receipts = get_receipts({c["receipt_id"] for c in candidates})
    name_cache: dict[str, str] = {}
    claims = []
    for c in candidates:
        summary = _claim_summary(c, receipts)
        summary["recipient_name"] = _recipient_display_name(c["recipient_id"], name_cache)
        # _run_claims와 같은 이유로 web 전용 필드다 — _claim_summary(에이전트
        # enqueue와 공유)에는 안 넣는다.
        summary["items"] = (receipts.get(c["receipt_id"]) or {}).get("items", [])
        claims.append(summary)
    return {"claims": claims}


@router.post("/settlements/runs")
def create_settlement_run_route(body: dict | None = None, authorization: str = Header(default="")):
    session = _session_from_header(authorization)
    filter = SettlementFilter(**(body or {}).get("filter", {}))
    candidates = select_claims_for_run(session["org_id"], filter)
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
    # 이번 배치 후보끼리뿐 아니라 이미 IN_RUN·SETTLED로 넘어간 과거 claim과도
    # 대조한다 — 안 그러면 이미 송금 끝난 영수증이 다른 달 배치에 다시 청구돼도
    # 아무 것도 못 잡는다(list_confirmed_claims는 미배치 claim만 보므로).
    settled_claims = list_settled_claims(session["org_id"])
    settled_receipts = get_receipts({c["receipt_id"] for c in settled_claims})
    exact_duplicate_groups = find_exact_duplicate_receipts(
        claims, receipts, settled_claims=settled_claims, settled_receipts=settled_receipts
    )
    try:
        enqueue_executor_analyze(
            run_id, claim_summaries, duplicate_groups, exact_duplicate_groups, session["org_id"]
        )
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
        try:
            set_executor_analysis_status(run_id, "FAILED", reason=str(e))
        except Exception:
            pass
    else:
        # enqueue 성공 = 집행자 에이전트가 언젠가 분석을 시작한다는 뜻일 뿐,
        # 지금 당장 시작했다는 뜻은 아니다 — 그래도 web이 "아직 분석 안 됨"과
        # "진행 중"을 구분해 보여줄 수 있도록 여기서 먼저 표시해둔다. 에이전트가
        # submit_settlement_analysis로 최종 결과를 쓰면 이 문서를 통째로 덮어써
        # status가 사라지고(_executor_analysis가 DONE으로 기본값 처리), 실패로
        # 끝나면(agent 쪽 500 등) 이 PROCESSING이 그대로 남는다 — 재시도 없이는
        # FAILED로 못 바꾼다는 뜻이지만, 그 이상의 실패 감지 배선은 이번 범위 밖이다.
        try:
            set_executor_analysis_status(run_id, "PROCESSING")
        except Exception:
            pass

    return _public_run(doc)


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
        # items도 recipient_name과 같은 이유로 web 전용이다 — _claim_summary(에이전트
        # enqueue와 공유)에는 넣지 않는다. 이상징후 판단에 품목 단위 정보가
        # 필요하지 않고, §6 "인젝션 표면 축소" 원칙상 안 쓰는 필드는 안 보낸다.
        # (list_unsettled_claims도 같은 이유로 web 전용 — 빠지면 web이
        # claim.items.length를 undefined에서 읽어 500이 난다.)
        summary["items"] = (receipts.get(c["receipt_id"]) or {}).get("items", [])
        summaries.append(summary)
    return summaries


@router.get("/settlements/runs/{run_id}")
def get_settlement_run_route(run_id: str, authorization: str = Header(default="")):
    session = _session_from_header(authorization)
    run = get_settlement_run(run_id)
    if run is None or run.get("org_id") != session["org_id"]:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    public = _public_run(run)
    public["executor_analysis"] = _executor_analysis(run_id)
    public["claims"] = _run_claims(run_id)
    return public


@router.patch("/settlements/runs/{run_id}/claims/{claim_id}/items/{item_index}")
def set_claim_item_excluded_route(
    run_id: str,
    claim_id: str,
    item_index: int,
    body: dict,
    authorization: str = Header(default=""),
):
    """청구 반려 — 집행자가 물품 단위로 체크를 해제하면 그 물품 가격을 claim.amount_minor에서
    뺀다. DRAFT 상태에서만 허용한다 — 승인 이후엔 approval_amount_hash가 이미 금액을
    고정하므로 여기서 바꾸면 승인·집행 사이 금액이 달라진다(money-safety.md)."""
    session = _session_from_header(authorization)
    run = get_settlement_run(run_id)
    if run is None or run.get("org_id") != session["org_id"]:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    if run["status"] != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail=f"settlement_run status is {run['status']}, expected DRAFT",
        )

    excluded = body.get("excluded")
    if not isinstance(excluded, bool):
        raise HTTPException(status_code=400, detail="excluded must be a boolean")

    claim = get_claim(claim_id)
    if claim is None or claim.get("settlement_run_id") != run_id or claim.get("org_id") != session["org_id"]:
        raise HTTPException(status_code=404, detail=f"unknown claim_id: {claim_id}")

    receipt = get_receipts({claim["receipt_id"]}).get(claim["receipt_id"])
    items = list((receipt or {}).get("items") or [])
    if item_index < 0 or item_index >= len(items):
        raise HTTPException(status_code=404, detail=f"unknown item_index: {item_index}")

    items[item_index] = {**items[item_index], "excluded": excluded}
    update_receipt_items(claim["receipt_id"], items)

    # 기준값은 항상 receipt.parsed_amount_minor다(§3 절대 규칙 — 숫자는 코드가
    # 만든다) — claim.amount_minor를 누적 감산하면 토글을 반복할 때 오차가 쌓인다.
    base_amount_minor = receipt.get("parsed_amount_minor") or 0
    excluded_total = sum(item.get("amount_minor") or 0 for item in items if item.get("excluded"))
    new_amount_minor = max(base_amount_minor - excluded_total, 0)
    update_claim(claim_id, {"amount_minor": new_amount_minor, "updated_at": datetime.now(UTC)})

    return {"claim_id": claim_id, "amount_minor": new_amount_minor, "items": items}


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
