"""schema-contract.md §9 — agent가 draft를 쓰는 유일한 창구.

`agent`는 Firestore SDK를 직접 쓰지 않는다(architecture.md) — 이 라우트가 대리 쓰기를
한다. Cloud Tasks가 아니라 `agent` 서비스 계정이 직접 부르므로 OIDC 검증은 동일하되
호출자가 다르다(infra/iam.tf `agent_invoker_api` 참조).

`/agents/audit`는 before_tool_callback이 툴 호출 시도(거부 포함)를 남기는 경로다 —
money-safety.md "모든 툴 호출을 audit_logs에 남긴다"는 성공한 draft 쓰기뿐 아니라
거부된 시도도 포함한다.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException

from ..payouts.store import get_client
from .audit import record_audit_log
from .oidc import verify_oidc
from .tasks import enqueue_task
from .translate import translate_lines

router = APIRouter()

_VALID_AGENTS = {"CLAIMANT", "EXECUTOR", "SAFETY"}
_VALID_TARGET_TYPES = {"RECEIPT", "SETTLEMENT_RUN"}


def write_agent_draft_document(
    *, agent: str, target_type: str, target_id: str, task_id: str, payload: dict
) -> dict:
    """`agent_drafts` 문서를 실제로 쓴다. `/agents/drafts`(에이전트 전용, OIDC
    필요)와 코드가 결정론적으로 재요청을 확정하는 경로(예: parsing/pipeline.py의
    거래일자 미검출)가 공유한다 — `ingest/routes.py._requery_message`가 DM 문안을
    항상 이 컬렉션에서만 읽으므로, 청구자 에이전트를 거치지 않는 재요청도 여기에
    문서를 남겨야 DM이 나간다.

    감사 로그·`/tasks/apply-claimant-draft` enqueue는 호출부 책임이다 — 코드
    호출부는 actor_type=AGENT가 아니고, apply_claimant_verdict도 직접 부르므로
    같은 draft를 또 apply하는 task를 enqueue할 이유가 없다."""
    payload = _with_translated_fields(agent, payload)
    draft = {
        "draft_id": f"drf_{task_id}",
        "agent": agent,
        "target_type": target_type,
        "target_id": target_id,
        "task_id": task_id,
        "payload": payload,
        "created_at": datetime.now(UTC),
    }
    get_client().collection("agent_drafts").document(task_id).set(draft)
    if agent == "EXECUTOR":
        _enqueue_executor_translation(task_id, payload)
    return draft


def _with_translated_fields(agent: str, payload: dict) -> dict:
    """schema-contract.md §9 — 청구자는 한국어만 쓴다. Slack DM(CLAIMANT)이
    보여줄 영어는 여기서 Gemma로 번역해 채운다. 번역 실패는 조용히 무시한다 —
    payload는 원본 그대로 반환하고, 읽는 쪽(ingest/routes.py._requery_message)이
    이미 en 필드 없음을 정상 상태로 다룬다.

    EXECUTOR는 이 경로를 타지 않는다 — 한국어 번역(anomalies_ko·summary_text_ko)이
    필요하지만 동기로 하면 submit_settlement_analysis 이후 요청이 끝나기 전에
    최대 15초(translate.py _TIMEOUT_MS)가 순차로 붙는다(한때 실제로 이렇게
    구현했다가 지연 때문에 되돌린 적이 있다). 대신 draft를 영어 그대로 먼저
    커밋하고, _enqueue_executor_translation이 번역을 별도 Cloud Task로 미룬다
    — web은 영어를 먼저 보여주고 번역이 끝나면 폴링으로 따라잡는다
    (frontend StatusPoller 참조)."""
    if agent == "CLAIMANT":
        requery_message = payload.get("requery_message")
        if not isinstance(requery_message, str) or not requery_message.strip():
            return payload
        translated = translate_lines([requery_message])
        if translated is None:
            return payload
        return {**payload, "requery_message_en": translated[0]}

    return payload


def _enqueue_executor_translation(task_id: str, payload: dict) -> None:
    """집행자 draft를 영어로 커밋한 직후, 한국어 번역을 비동기 Cloud Task로
    미룬다 — Gemma 호출(최대 15초)을 요청 경로에서 완전히 뺀다.

    summary_text·anomalies를 태스크 본문에 그대로 실어 보낸다(task_id로
    Firestore를 다시 읽지 않는다) — 이 태스크가 늦게 도착했을 때 그 사이
    같은 task_id로 재분석(web "재시도" 버튼)이 새 draft를 덮어썼다면, 지금
    보내는 이 영어 원문은 이미 낡은 내용이다. /tasks/translate-executor-draft가
    되돌아와서 저장하기 직전에 Firestore의 현재 summary_text와 이 값을
    대조해, 다르면(=이미 새 draft로 덮어써졌다) 쓰지 않고 조용히 버린다 —
    낡은 번역이 새 영어 내용에 잘못 붙는 사고를 막는다."""
    summary_text = payload.get("summary_text")
    if not isinstance(summary_text, str) or not summary_text.strip():
        return
    anomalies = payload.get("anomalies") or []
    try:
        enqueue_task(
            "/tasks/translate-executor-draft",
            {"task_id": task_id, "summary_text": summary_text, "anomalies": anomalies},
        )
    except Exception as e:
        record_audit_log(
            actor="api/src/guards",
            action="EXECUTOR_TRANSLATION_ENQUEUE_FAILED",
            reason=str(e),
            after={"task_id": task_id},
        )


@router.post("/agents/drafts")
def write_agent_draft(body: dict, authorization: str = Header(default="")):
    verify_oidc(authorization)

    agent = body.get("agent")
    target_type = body.get("target_type")
    target_id = body.get("target_id")
    task_id = body.get("task_id")
    payload = body.get("payload")
    if agent not in _VALID_AGENTS:
        raise HTTPException(status_code=400, detail=f"invalid agent: {agent}")
    if target_type not in _VALID_TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"invalid target_type: {target_type}")
    if not target_id or not task_id or payload is None:
        raise HTTPException(status_code=400, detail="target_id, task_id, payload required")

    # schema-contract.md §2 — task_id가 멱등키다. 문서 ID로 재사용해 Cloud Tasks
    # 재시도가 새 draft를 쌓지 않고 같은 draft를 덮어쓰게 한다.
    draft = write_agent_draft_document(
        agent=agent, target_type=target_type, target_id=target_id, task_id=task_id, payload=payload
    )

    # schema-contract.md §9 — 안전 확인 에이전트의 risk_report는 audit_logs.reason에
    # 그대로 저장된다. 조언일 뿐이라 게이트 판단에는 쓰지 않는다.
    record_audit_log(
        actor=f"agent/{agent.lower()}",
        actor_type="AGENT",
        action="AGENT_DRAFT_WRITTEN",
        run_id=target_id if target_type == "SETTLEMENT_RUN" else None,
        after={"draft_id": draft["draft_id"]},
        reason=payload.get("risk_report") if agent == "SAFETY" else None,
    )

    # 청구자 draft는 api가 읽어 상태 전이로 반영한다(§9). 실패해도 draft 쓰기를
    # 되돌리지 않는다 — 에이전트에게 500을 주면 Cloud Tasks가 LLM 호출을 다시 태운다.
    # 감사 로그에 task_id를 남기는 게 중요하다: 이 라우트는 task_id로만 draft를
    # 찾으므로, 실패 건을 audit_logs에서 뽑아 그대로 재큐잉할 수 있어야 한다.
    if agent == "CLAIMANT":
        try:
            enqueue_task("/tasks/apply-claimant-draft", {"task_id": task_id})
        except Exception as e:
            record_audit_log(
                actor="api/src/guards",
                action="CLAIMANT_DRAFT_APPLY_ENQUEUE_FAILED",
                reason=str(e),
                after={"draft_id": draft["draft_id"], "task_id": task_id},
            )

    return {"draft_id": draft["draft_id"], "status": "ok"}


@router.post("/agents/audit")
def write_agent_tool_audit(body: dict, authorization: str = Header(default="")):
    """agent-tools.md before_tool_callback — 툴 호출 시도를 남긴다. 거부됐어도 남긴다."""
    verify_oidc(authorization)

    agent = body.get("agent")
    action = body.get("action")
    if agent not in _VALID_AGENTS or not action:
        raise HTTPException(status_code=400, detail="agent, action required")

    record_audit_log(
        actor=f"agent/{agent.lower()}",
        actor_type="AGENT",
        action=action,
        run_id=body.get("run_id"),
        reason=body.get("reason"),
    )
    return {"status": "ok"}


@router.post("/tasks/translate-executor-draft")
def task_translate_executor_draft(body: dict, authorization: str = Header(default="")):
    """_enqueue_executor_translation이 태운 백그라운드 번역 — Cloud Tasks만
    부른다(OIDC). Gemma 호출이 여기서 일어나므로 web 요청 경로엔 이 지연이
    안 보인다.

    번역 실패(Gemma 장애·malformed output)는 translate.py 원칙과 같다 —
    조언성 부가 기능이라 재시도해도 또 실패할 수 있고, 다음에 새로 분석이
    돌면(재시도 버튼) 그때 다시 시도된다. 여기서 500을 던져 Cloud Tasks가
    무한 재시도하게 만들 이유가 없어 항상 200을 돌려준다."""
    verify_oidc(authorization)

    task_id = body.get("task_id")
    summary_text = body.get("summary_text")
    if not task_id or not isinstance(summary_text, str) or not summary_text.strip():
        raise HTTPException(status_code=400, detail="task_id, summary_text required")
    anomalies = body.get("anomalies") or []

    translated = translate_lines([summary_text, *anomalies], target_language="Korean")
    if translated is None:
        return {"status": "ok", "translated": False}

    doc_ref = get_client().collection("agent_drafts").document(task_id)
    snapshot = doc_ref.get()
    if not snapshot.exists:
        return {"status": "ignored", "reason": "draft_not_found"}

    # 이 태스크가 도착하기 전에 같은 task_id로 재분석(web "재시도" 버튼)이
    # 새 draft를 덮어썼으면, 지금 들고 있는 번역은 이미 낡은 영어 원문의
    # 번역이다 — 그 새 draft에는 별도 번역 태스크가 이미 따로 돌고 있으므로
    # 여기서 그냥 버린다. 낡은 한국어가 새 영어 내용에 잘못 붙는 걸 막는
    # 유일한 방어선이다.
    current_payload = snapshot.to_dict().get("payload") or {}
    if current_payload.get("summary_text") != summary_text:
        return {"status": "ignored", "reason": "draft_superseded"}

    doc_ref.update({"payload.summary_text_ko": translated[0], "payload.anomalies_ko": translated[1:]})
    return {"status": "ok", "translated": True}
