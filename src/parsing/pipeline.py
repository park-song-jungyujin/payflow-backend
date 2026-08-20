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
from .categorize import build_parse_signals, route_category
from .enqueue import enqueue_claimant_review
from .masking import mask_pii
from .models import amount_to_minor
from .parser import get_parser
from .slack_files import PermanentParseError, download_slack_file
from .storage import get_object_store, image_key, raw_text_key
from .store import ReceiptNotFound, get_receipt, update_receipt

_ACTOR = "api/src/parsing"


def parse_receipt(receipt_id: str) -> str:
    """최종 status를 돌려준다: "PARSED" / "FAILED" / "SKIPPED".

    TransientParseError는 잡지 않고 그대로 올린다 — 호출부(routes.py)가 503으로
    바꿔 Cloud Tasks가 재시도하게 한다. 일시적 실패에 FAILED를 찍으면 멀쩡한
    영수증이 재요청 DM 대상이 된다.

    **예상 밖 예외 정책**: TransientParseError·PermanentParseError가 아닌 예외도
    여기서 잡지 않는다. update_receipt(PARSED로 확정하는 쓰기)보다 앞에서 터지므로
    부분 쓰기가 없고, 영수증은 RECEIVED로 남아 Cloud Tasks 재시도에 맡기는 게
    안전한 기본값이다. 실패가 명백히 영구적이라고 판단되면(예: slack_file_id
    누락) PermanentParseError로 올려 FAILED로 직접 확정한다.
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

    slack_file_id = receipt.get("slack_file_id")
    if not slack_file_id:
        # 계약상 nullable이다 — Slack 경로가 아닌 방식(fixture 시딩, 수동 등록)으로
        # 만들어진 영수증에는 값이 없을 수 있다. 대괄호로 읽으면 KeyError가 밖으로
        # 새 나가 영수증이 RECEIVED에 영원히 남는다. 다시 불러도 같은 실패이므로
        # PermanentParseError로 올려 FAILED로 확정한다 — 그래야 재요청 DM이 나간다.
        raise PermanentParseError(f"receipt {receipt_id} has no slack_file_id")

    slack_file = download_slack_file(slack_file_id)
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
    # 여기서부터는 PARSED가 이미 Firestore에 남은 뒤의 부수 효과다. 되돌리지
    # 않는다 — 되돌리면 Gemini 호출을 다시 태우고, 재시도해도 status != RECEIVED라
    # SKIPPED로 빠져 같은 부수 효과가 영영 안 돈다(ingest/routes.py와 같은 판단).
    #
    # 감사 로그와 enqueue는 **각각 별개의 try**로 감싼다. 같은 try에 넣으면
    # 감사 로그가 던지는 순간 제어가 except로 점프해 enqueue_claimant_review가
    # 영영 호출되지 않는다 — 재시도해도 status != RECEIVED라 SKIPPED로 빠지므로
    # "감사 로그 실패 시 enqueue를 지킨다"는 목적이 오히려 정반대로 깨진다.
    # 두 예외 모두 넓게 잡는다 — 감사 로그는 Firestore 장애, enqueue는
    # QueueNotConfigured뿐 아니라 os.environ 대괄호 접근(KeyError)이나
    # Cloud Tasks 네트워크 호출(Google API 예외)로도 던질 수 있다.
    #
    # 이 범위는 여기서 끝난다 — _parse 앞부분(download_slack_file·parser 호출·
    # update_receipt)까지 넓히면 TransientParseError를 삼켜 불변식 I3(일시적
    # 실패는 상태를 안 바꾸고 밖으로 나간다)이 깨진다.
    try:
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
    except Exception as e:
        # 이 기록 자체가 실패해도 최선을 다한다 — PARSED 자체는 이미 남아 있으니
        # 여기서 또 던지면 안 되고, 뒤따르는 enqueue는 반드시 시도되어야 한다.
        try:
            record_audit_log(
                actor=_ACTOR,
                action="CLAIMANT_ENQUEUE_FAILED",
                reason=mask_pii(str(e)),
                after={"receipt_id": receipt_id},
            )
        except Exception:
            pass

    # PARSED일 때만 청구자 에이전트를 부른다. FAILED는 재촉 루프 몫이다.
    try:
        enqueue_claimant_review(receipt_id)
    except Exception as e:
        try:
            record_audit_log(
                actor=_ACTOR,
                action="CLAIMANT_ENQUEUE_FAILED",
                reason=mask_pii(str(e)),
                after={"receipt_id": receipt_id},
            )
        except Exception:
            pass
    return "PARSED"
