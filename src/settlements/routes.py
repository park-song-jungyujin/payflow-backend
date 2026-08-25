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
from ..guards.oidc import verify_oidc
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
from .safety_enqueue import enqueue_safety_report, safety_draft_task_id
from .store import (
    get_agent_draft,
    get_receipts,
    set_executor_analysis_status,
    set_safety_report_status,
    update_receipt_items,
)
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
    nullable/기본값으로)."""
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


def _safety_report(run_id: str) -> dict | None:
    """agent_drafts.SAFETY를 읽는 유일한 지점. None이면 정산 실행 생성 자체가
    실패했거나(enqueue 실패로 status 쓰기까지 죽은 경우) 아주 옛날 run이지
    진행 상태가 아니다 — 진행 상태는 status 필드로 구분한다: PROCESSING(enqueue
    성공, 리포트 대기/작성 중), FAILED(enqueue 자체가 실패, set_safety_report_status),
    DONE(에이전트가 submit_risk_report로 최종 결과를 씀). _executor_analysis와
    같은 이유로 status 없으면 DONE 기본값 처리한다."""
    draft = get_agent_draft(safety_draft_task_id(run_id))
    if draft is None:
        return None
    payload = draft["payload"]
    return {
        "status": payload.get("status", "DONE"),
        "risk_report": payload.get("risk_report"),
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

    # items는 _claim_summary(§6 최소화 대상)에는 없다 — 청구 반려 자동화(집행자가
    # 개인적 사용 의심 물품을 골라내는 것)에 필요해 여기서만 얹는다. list_unsettled_claims·
    # _run_claims가 web 전용으로 따로 얹는 것과 같은 패턴이다.
    claim_summaries = [
        {**_claim_summary(c, receipts), "items": (receipts.get(c["receipt_id"]) or {}).get("items", [])}
        for c in claims
    ]
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

    try:
        enqueue_safety_report(run_id, _public_run(doc))
    except Exception as e:
        # 집행자 enqueue와 같은 이유로 별도 try — 안전 확인은 조언자일 뿐이라
        # 실패해도 정산 실행 생성 자체는 막지 않는다(agent-tools.md).
        try:
            record_audit_log(
                actor=_ACTOR,
                action="SAFETY_ENQUEUE_FAILED",
                run_id=run_id,
                reason=str(e),
                after={"settlement_run_id": run_id},
            )
        except Exception:
            pass
        try:
            set_safety_report_status(run_id, "FAILED", reason=str(e))
        except Exception:
            pass
    else:
        # executor와 같은 이유 — enqueue 성공 = 언젠가 에이전트가 시작한다는
        # 뜻일 뿐이라 web이 "아직 없음"과 "진행 중"을 구분하도록 먼저 표시해둔다.
        try:
            set_safety_report_status(run_id, "PROCESSING")
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
    public["safety_report"] = _safety_report(run_id)
    public["claims"] = _run_claims(run_id)
    return public


def _apply_item_exclusion(
    run: dict, claim_id: str, item_index: int, excluded: bool, reason: str | None = None
) -> dict:
    """청구 반려 핵심 로직 — 사람이 web 체크박스로 직접 하는 것(PATCH ...items/{i})과
    집행자 에이전트가 자동으로 하는 것(POST /agents/executor/reject-items)이 공유한다.
    claim이 run에 실제로 링크돼 있고 같은 org 소속인지는 여기서 확인한다 — 세션이
    아니라 run["org_id"] 기준이다(claim.org_id는 link_claims_to_run이 보장하는 대로
    항상 run과 같아야 한다 — 세션 컨텍스트가 없는 OIDC 호출도 이 함수를 그대로 쓸 수
    있게 하려는 것).

    reason은 excluded=True일 때만 물품에 남는다(rejected_reason/rejected_by) —
    나중에 청구 반려 내역·사유를 Slack으로 청구자에게 보낼 때 쓸 근거다. 체크를
    다시 되돌리면(excluded=False) 지운다 — 더 이상 반려 상태가 아니므로.
    """
    claim = get_claim(claim_id)
    if claim is None or claim.get("settlement_run_id") != run["settlement_run_id"] or claim.get(
        "org_id"
    ) != run.get("org_id"):
        raise HTTPException(status_code=404, detail=f"unknown claim_id: {claim_id}")

    receipt = get_receipts({claim["receipt_id"]}).get(claim["receipt_id"])
    items = list((receipt or {}).get("items") or [])
    if item_index < 0 or item_index >= len(items):
        raise HTTPException(status_code=404, detail=f"unknown item_index: {item_index}")

    updated_item = dict(items[item_index])
    updated_item["excluded"] = excluded
    if excluded and reason:
        updated_item["rejected_reason"] = reason
        updated_item["rejected_by"] = "EXECUTOR"
    else:
        updated_item.pop("rejected_reason", None)
        updated_item.pop("rejected_by", None)
    items[item_index] = updated_item
    update_receipt_items(claim["receipt_id"], items)

    # 기준값은 항상 receipt.parsed_amount_minor다(§3 절대 규칙 — 숫자는 코드가
    # 만든다) — claim.amount_minor를 누적 감산하면 토글을 반복할 때 오차가 쌓인다.
    base_amount_minor = receipt.get("parsed_amount_minor") or 0
    excluded_total = sum(item.get("amount_minor") or 0 for item in items if item.get("excluded"))
    new_amount_minor = max(base_amount_minor - excluded_total, 0)
    update_claim(claim_id, {"amount_minor": new_amount_minor, "updated_at": datetime.now(UTC)})

    return {"claim_id": claim_id, "amount_minor": new_amount_minor, "items": items}


@router.patch("/settlements/runs/{run_id}/claims/{claim_id}/items/{item_index}")
def set_claim_item_excluded_route(
    run_id: str,
    claim_id: str,
    item_index: int,
    body: dict,
    authorization: str = Header(default=""),
):
    """청구 반려 — 집행자(사람)가 물품 단위로 체크를 해제하면 그 물품 가격을
    claim.amount_minor에서 뺀다. DRAFT 상태에서만 허용한다 — 승인 이후엔
    approval_amount_hash가 이미 금액을 고정하므로 여기서 바꾸면 승인·집행 사이
    금액이 달라진다(money-safety.md)."""
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

    return _apply_item_exclusion(run, claim_id, item_index, excluded)


@router.post("/agents/executor/reject-items")
def reject_claim_items_route(body: dict, authorization: str = Header(default="")):
    """청구 반려 자동화 — 집행자 에이전트가 이상징후 분석을 마친 뒤 개인적 사용이
    의심되는 물품을 한 번에 제외한다(executor/tools.py flag_personal_use_items).
    사람이 web에서 체크박스로 직접 하는 것과 최종 효과(_apply_item_exclusion)는
    같다 — 승인 전까지는 사람이 web에서 언제든 다시 체크해 되돌릴 수 있으므로,
    최종 결정권은 여전히 사람에게 있다(절대 규칙 3 — 금액은 코드가 계산하고,
    LLM은 "어떤 물품이 의심스러운가"만 판단한다).

    Cloud Tasks가 아니라 agent 서비스 계정이 직접 부른다 — /agents/drafts와 같은
    인증 방식(agent_invoker_api)."""
    verify_oidc(authorization)

    run_id = body.get("settlement_run_id")
    rejections = body.get("rejections")
    if not run_id or not rejections:
        raise HTTPException(status_code=400, detail="settlement_run_id, rejections required")

    run = get_settlement_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown settlement_run_id: {run_id}")
    if run["status"] != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail=f"settlement_run status is {run['status']}, expected DRAFT",
        )

    results = []
    for r in rejections:
        claim_id = r.get("claim_id")
        item_index = r.get("item_index")
        reason = r.get("reason")
        if not claim_id or not isinstance(item_index, int) or not reason:
            results.append(
                {
                    "claim_id": claim_id,
                    "item_index": item_index,
                    "status": "error",
                    "detail": "claim_id, item_index, reason required",
                }
            )
            continue
        try:
            outcome = _apply_item_exclusion(run, claim_id, item_index, excluded=True, reason=reason)
        except HTTPException as e:
            results.append(
                {"claim_id": claim_id, "item_index": item_index, "status": "error", "detail": e.detail}
            )
            continue

        record_audit_log(
            org_id=run.get("org_id"),
            actor="agent/executor",
            actor_type="AGENT",
            action="CLAIM_ITEM_REJECTED",
            run_id=run_id,
            reason=reason,
            after={"claim_id": claim_id, "item_index": item_index, "amount_minor": outcome["amount_minor"]},
        )
        results.append(
            {
                "claim_id": claim_id,
                "item_index": item_index,
                "status": "ok",
                "amount_minor": outcome["amount_minor"],
            }
        )

    return {"results": results}


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
