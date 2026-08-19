"""영수증 파싱 파이프라인 — 이 계획의 유일한 오케스트레이터 (A 소유).

**순서가 계약이다.** PII 마스킹은 Firestore 쓰기 *직전*에 오고, 원문은 객체
저장소에만 남는다 (schema-contract.md §2).

**파싱이 쓸 수 있는 status는 PARSED와 FAILED뿐이다.** NEEDS_REQUERY는 청구자
에이전트가 청구 확정 과정에서 내리는 판단이라(§2), 코드가 대신 쓰면 감사 로그에서
판정 주체를 잃는다. 금액을 못 읽었거나 confidence가 낮은 건 상태가 아니라
account_category_code = UNCLASSIFIED로 표현된다 (§5).
"""

from datetime import UTC, datetime

from ..guards.audit import record_audit_log
from ..guards.tasks import QueueNotConfigured
from .categorize import build_parse_signals, route_category
from .enqueue import enqueue_claimant_review
from .masking import mask_pii
from .models import amount_to_minor
from .parser import get_parser
from .slack_files import PermanentParseError, TransientParseError, download_slack_file
from .storage import get_object_store, image_key, raw_text_key
from .store import ReceiptNotFound, get_receipt, update_receipt

_ACTOR = "api/src/parsing"


def parse_receipt(receipt_id: str) -> str:
    """최종 status를 돌려준다: "PARSED" / "FAILED" / "SKIPPED".

    TransientParseError는 잡지 않고 그대로 올린다 — 호출부(routes.py)가 503으로
    바꿔 Cloud Tasks가 재시도하게 한다. 일시적 실패에 FAILED를 찍으면 멀쩡한
    영수증이 재요청 DM 대상이 된다.
    """
    receipt = get_receipt(receipt_id)
    if receipt is None:
        raise ReceiptNotFound(receipt_id)

    # Cloud Tasks 재시도 멱등성. 두 번째 호출이 파서를 다시 부르면 Gemini 비용이
    # 두 배가 되고 image_gcs_uri가 덮어써진다.
    if receipt.get("status") != "RECEIVED":
        return "SKIPPED"

    try:
        return _parse(receipt_id, receipt)
    except PermanentParseError as e:
        # 다시 불러도 같은 실패다. 여기서 확정하지 않으면 영수증이 RECEIVED로
        # 영원히 남아 아무도 재요청 DM을 보내지 않는다.
        update_receipt(receipt_id, {"status": "FAILED", "updated_at": datetime.now(UTC)})
        record_audit_log(
            actor=_ACTOR,
            action="RECEIPT_PARSE_FAILED",
            reason=mask_pii(str(e)),
            after={"receipt_id": receipt_id, "status": "FAILED"},
        )
        return "FAILED"


def _parse(receipt_id: str, receipt: dict) -> str:
    store = get_object_store()

    slack_file = download_slack_file(receipt["slack_file_id"])
    image_uri = store.put(
        key=image_key(receipt_id, slack_file.ext),
        data=slack_file.data,
        content_type=slack_file.mimetype,
    )

    parsed = get_parser().parse(
        image=slack_file.data, mimetype=slack_file.mimetype, receipt_id=receipt_id
    )

    # 원문은 마스킹하지 않는다 — 원본은 객체 저장소에만 두는 게 §2의 전제이고,
    # 청구자 에이전트가 <untrusted_receipt_text>로 읽어갈 소스가 여기다.
    raw_text_uri = store.put(
        key=raw_text_key(receipt_id),
        data=parsed.raw_text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
    )

    # 숫자는 코드가 만든다 (공통 CLAUDE.md 절대 규칙 3).
    amount_minor = amount_to_minor(parsed.amount_text, parsed.currency)
    signals = build_parse_signals(parsed, amount_minor)
    category, source, confidence = route_category(parsed, signals)

    now = datetime.now(UTC)
    update_receipt(
        receipt_id,
        {
            "image_gcs_uri": image_uri,
            "raw_text_gcs_uri": raw_text_uri,
            # ★ 마스킹은 여기서. Firestore로 나가는 유일한 자유 텍스트다.
            "merchant_name": mask_pii(parsed.merchant_name),
            # §1 시각 예외 — transaction_date는 YYYY-MM-DD 문자열이다.
            # Timestamp로 저장하면 KST/UTC 경계에서 하루가 밀린다.
            "transaction_date": parsed.transaction_date.isoformat() if parsed.transaction_date else None,
            "parsed_amount_minor": amount_minor,
            "currency": parsed.currency,
            "account_category_code": category.value,
            "category_source": source.value,
            "parse_signals": signals.model_dump(),
            "llm_confidence": confidence,
            "status": "PARSED",
            "updated_at": now,
        },
    )
    record_audit_log(
        actor=_ACTOR,
        action="RECEIPT_PARSED",
        after={
            "receipt_id": receipt_id,
            "status": "PARSED",
            "account_category_code": category.value,
            "category_source": source.value,
        },
    )

    # PARSED일 때만 청구자 에이전트를 부른다. FAILED는 재촉 루프 몫이다.
    try:
        enqueue_claimant_review(receipt_id)
    except QueueNotConfigured as e:
        # 파싱 결과는 이미 남았다. 여기서 되돌리면 Gemini 호출을 다시 태우게 된다.
        # ingest/routes.py가 enqueue 실패에 대해 내린 것과 같은 판단이다.
        record_audit_log(
            actor=_ACTOR,
            action="CLAIMANT_ENQUEUE_FAILED",
            reason=mask_pii(str(e)),
            after={"receipt_id": receipt_id},
        )
    return "PARSED"
