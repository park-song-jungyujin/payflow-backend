"""영수증 파싱 파이프라인 — 이 계획의 유일한 오케스트레이터 (A 소유).

**순서가 계약이다.** PII 마스킹은 Firestore 쓰기 *직전*에 오고, 원문은 객체
저장소에만 남는다 (schema-contract.md §2).

**파싱 본체(_parse)가 update_receipt/commit_parsed_with_claim으로 직접 쓸 수 있는
status는 PARSED와 FAILED뿐이다.** NEEDS_REQUERY는 원래 청구자 에이전트가 청구
확정 과정에서 내리는 판단이라(§2), 코드가 대신 쓰면 감사 로그에서 판정 주체를
잃는다 — **단, 거래일자 미검출은 예외다.** 이건 LLM 판단이 필요 없는 결정론적
규칙이라 청구자 에이전트를 거치지 않고 apply_claimant_verdict를 직접 호출해
NEEDS_REQUERY로 확정한다(RECEIPT_DATE_MISSING_REQUERY 감사 로그가 판정 주체를
남긴다). 금액을 못 읽었거나 confidence가 낮은 건 상태가 아니라
account_category_code = UNCLASSIFIED로 표현된다 (§5).
"""

from datetime import UTC, datetime

from ..guards.agent_drafts import write_agent_draft_document
from ..guards.audit import record_audit_log
from ..guards.translate import translate_lines
from ..ingest.claims import ClaimNotCreatable, build_claim
from ..ingest.drafts import DraftVerdict
from ..ingest.enqueue import enqueue_remind
from ..ingest.store import apply_claimant_verdict
from ..schemas.enums import ReminderReason
from .categorize import build_parse_signals, route_category
from .enqueue import enqueue_claimant_review
from .masking import hash_receipt_serial_number, mask_pii
from .models import ParsedReceipt, amount_to_minor
from .parser import get_parser
from .slack_files import PermanentParseError, download_slack_file
from .storage import get_object_store, image_key, raw_text_key
from .store import ReceiptNotFound, commit_parsed_with_claim, get_receipt, update_receipt

_ACTOR = "api/src/parsing"


def _build_items(parsed: ParsedReceipt) -> list[dict]:
    """영수증 품목을 Firestore에 저장할 모양으로 만든다 — 마스킹까지만 한다.
    영어 번역(`name_en`)은 _translate_receipt_names가 얹는다.

    숫자는 코드가 만든다(공통 CLAUDE.md 절대 규칙 3). 품목별 금액도 영수증
    전체와 같은 함수·같은 currency로 변환한다 — 품목은 영수증 전체와 다른
    통화를 쓸 수 없다.
    """
    return [
        {
            "name": mask_pii(item.name),
            "amount_minor": amount_to_minor(item.amount_text, parsed.currency),
        }
        for item in parsed.items
    ]


def _translate_receipt_names(
    merchant_name: str | None, items: list[dict]
) -> tuple[str | None, list[dict]]:
    """가맹점명·품목명의 영어 번역을 붙인다. (merchant_name_en, items) 반환.

    web 대시보드는 `en` 로케일에서 이 번역을 보여주고, 그 아래 회색으로 원문을
    같이 적는다(frontend lib/receiptText.ts). 영수증에서 읽은 이름은 원문이
    한국어라, 집행자 서술·Slack 발송 문구를 영어로 통일한 뒤에도 여기만
    "스타벅스 강남점"·"G)콜드브루"처럼 한국어로 남아 있었다.

    **가맹점명과 품목명을 한 번의 Gemma 호출로 묶는다** — 따로 부르면 최대
    15초(translate.py _TIMEOUT_MS)짜리 대기가 파싱 태스크에 두 번 순차로 붙는다.
    가맹점명이 항상 첫 줄이고 품목명이 그 뒤를 원래 순서대로 잇는다.

    **넘기는 건 마스킹 뒤의 이름이다** — 원문을 그대로 보내면 "PII 마스킹은
    Firestore 쓰기 직전"(§2)이 외부 호출 하나로 우회된다. 번역도 마스킹된
    원문의 번역이어야 원문과 짝이 맞는다.

    번역 실패는 조용히 흡수한다(guards/translate.py — 번역은 조언성 부가
    기능이라 파싱을 막지 않는다). `name_en`은 키 자체를 넣지 않아 이 필드가
    생기기 전에 파싱된 영수증과 같은 모양으로 남기고, `merchant_name_en`은
    None이다 — 읽는 쪽은 둘 다 원문으로 폴백한다(schema-contract.md — 필드
    추가는 항상 nullable/기본값, 문서 백필 없이).

    이 Gemma 호출은 Cloud Tasks가 부르는 파싱 태스크 안에서 일어나 사용자
    요청 경로에는 지연이 붙지 않는다.
    """
    # 가맹점명도 품목도 못 읽은 영수증에는 호출 자체를 생략한다 — 빈 호출이
    # 파싱 태스크에 그대로 지연으로 붙는다.
    lines = ([merchant_name] if merchant_name else []) + [item["name"] for item in items]
    if not lines:
        return None, items

    translated = translate_lines(lines)
    # 길이는 translate_lines가 이미 보장하지만(어긋나면 None) 한 번 더 본다 —
    # 어긋난 채 zip하면 가맹점명 번역이 품목으로 한 칸 밀려 조용히 틀린 화면이 된다.
    if translated is None or len(translated) != len(lines):
        return None, items

    if merchant_name:
        merchant_name_en, item_names_en = translated[0], translated[1:]
    else:
        merchant_name_en, item_names_en = None, translated
    return merchant_name_en, [
        {**item, "name_en": name_en} for item, name_en in zip(items, item_names_en)
    ]


def parse_receipt(receipt_id: str) -> str:
    """최종 status를 돌려준다: "PARSED" / "FAILED" / "SKIPPED".

    TransientParseError는 잡지 않고 그대로 올린다 — 호출부(routes.py)가 503으로
    바꿔 Cloud Tasks가 재시도하게 한다. 일시적 실패에 FAILED를 찍으면 멀쩡한
    영수증이 재요청 DM 대상이 된다.

    **예상 밖 예외 정책**: TransientParseError·PermanentParseError가 아닌 예외도
    여기서 잡지 않는다. commit_parsed_with_claim(PARSED로 확정하는 쓰기)보다
    앞에서 터지므로 부분 쓰기가 없고, 영수증은 RECEIVED로 남아 Cloud Tasks
    재시도에 맡기는 게 안전한 기본값이다. 실패가 명백히 영구적이라고
    판단되면(예: slack_file_id 누락) PermanentParseError로 올려
    update_receipt로 FAILED를 직접 확정한다.
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

    slack_file = download_slack_file(slack_file_id, receipt["org_id"])
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
    # ★ 마스킹은 여기서. Firestore로 나가는 자유 텍스트가 전부 여기를 지난다.
    # 번역(_translate_receipt_names)은 마스킹 뒤 값만 본다 — 순서가 계약이다.
    merchant_name = mask_pii(parsed.merchant_name)
    merchant_name_en, items = _translate_receipt_names(merchant_name, _build_items(parsed))
    signals = build_parse_signals(parsed, amount_minor)
    category, source, confidence = route_category(parsed, signals)

    now = datetime.now(UTC)
    updates = {
        "image_gcs_uri": image_uri,
        "raw_text_gcs_uri": raw_text_uri,
        "merchant_name": merchant_name,
        # 가맹점명의 영어 번역. 번역 실패·미검출이면 None이고, web은 원문으로
        # 폴백한다(frontend lib/receiptText.ts merchantDisplay).
        "merchant_name_en": merchant_name_en,
        # §1 시각 예외 — transaction_date는 YYYY-MM-DD 문자열이다.
        # Timestamp로 저장하면 KST/UTC 경계에서 하루가 밀린다.
        "transaction_date": parsed.transaction_date.isoformat() if parsed.transaction_date else None,
        "transaction_time": parsed.transaction_time,
        # ★ mask_pii를 쓰지 않는다 — 이 필드는 거의 항상 순수 숫자라 카드번호
        # 패턴에 걸려 서로 다른 영수증의 고유번호가 전부 "[CARD]"로 뭉개지는
        # 버그가 실제로 있었다(masking.py.hash_receipt_serial_number 참조).
        # find_exact_duplicate_receipts가 유일성을 전제로 완전일치 판정을 하므로
        # 해시로 바꿔 저장해 유일성은 지키고 원문은 남기지 않는다.
        "receipt_serial_number_hash": hash_receipt_serial_number(parsed.receipt_serial_number),
        "items": items,
        "parsed_amount_minor": amount_minor,
        "currency": parsed.currency,
        "account_category_code": category.value,
        "category_source": source.value,
        "parse_signals": signals.model_dump(),
        "llm_confidence": confidence,
        "status": "PARSED",
        "updated_at": now,
    }

    # claim 조립은 receipts 갱신 전이다 — build_claim에는 파싱 전 receipt가
    # 아니라 updates가 반영된 dict를 넘겨야 금액·통화가 보인다.
    claim = None
    try:
        claim = build_claim({**receipt, **updates, "receipt_id": receipt_id}, now=now)
    except ClaimNotCreatable as e:
        # 금액을 못 읽은 영수증이다. 파싱 자체는 성공했으므로 PARSED로 남기고
        # 청구만 만들지 않는다 — 0원 claim을 조용히 만드는 것보다 낫다.
        # 감사 로그로 남겨 대시보드에서 사람이 볼 수 있게 한다. 이 감사 로그
        # 호출만 넓게 잡는다 — 감싸지 않으면 Firestore 장애 하나로 파싱 전체가
        # 중단되고 재시도가 Gemini를 다시 태운다.
        try:
            record_audit_log(
                actor=_ACTOR,
                action="CLAIM_NOT_CREATED",
                reason=mask_pii(str(e)),
                after={"receipt_id": receipt_id},
            )
        except Exception as audit_error:
            try:
                record_audit_log(
                    actor=_ACTOR,
                    action="CLAIM_NOT_CREATED_AUDIT_FAILED",
                    reason=mask_pii(str(audit_error)),
                    after={"receipt_id": receipt_id},
                )
            except Exception:
                pass

    # receipts의 PARSED 확정과 claim 생성을 한 트랜잭션으로 — 갈라지면 갱신은
    # 됐는데 claim 생성이 실패한 경우가 재시도로 복구되지 않는다(status !=
    # RECEIVED라 SKIPPED로 빠진다). 이 호출이 실제 확정 지점이다 — 트랜잭션
    # 안에서 receipt 상태를 다시 읽어 CAS로 확인하므로, 동시 전달로 여기까지
    # 같이 온 다른 시도가 있으면 둘 중 하나만 커밋되고 나머지는 False를 받는다.
    committed = commit_parsed_with_claim(receipt_id, updates, claim)
    if not committed:
        # 이미 다른 시도가 확정했다. 여기서부터 감사 로그·enqueue를 또 돌리면
        # CLAIM_CREATED·RECEIPT_PARSED·enqueue가 두 번 일어난다.
        return "SKIPPED"

    if claim is not None:
        try:
            record_audit_log(
                actor=_ACTOR,
                action="CLAIM_CREATED",
                after={
                    "receipt_id": receipt_id,
                    "claim_id": claim["claim_id"],
                    "status": claim["status"],
                },
            )
        except Exception as e:
            # claim은 이미 커밋됐다 — 여기서 또 던지면 안 되고, 뒤따르는
            # RECEIPT_PARSED·enqueue는 반드시 시도되어야 한다.
            try:
                record_audit_log(
                    actor=_ACTOR,
                    action="CLAIM_CREATED_AUDIT_FAILED",
                    reason=mask_pii(str(e)),
                    after={"receipt_id": receipt_id, "claim_id": claim["claim_id"]},
                )
            except Exception:
                pass

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
    # commit_parsed_with_claim)까지 넓히면 TransientParseError를 삼켜 불변식
    # I3(일시적 실패는 상태를 안 바꾸고 밖으로 나간다)이 깨진다.
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
        # 액션명은 RECEIPT_PARSED_AUDIT_FAILED — enqueue는 바로 아래에서 별개로
        # 시도되어 정상 성공할 수 있으므로 CLAIMANT_ENQUEUE_FAILED를 쓰면 성공한
        # enqueue를 실패로 오기록하게 된다.
        try:
            record_audit_log(
                actor=_ACTOR,
                action="RECEIPT_PARSED_AUDIT_FAILED",
                reason=mask_pii(str(e)),
                after={"receipt_id": receipt_id},
            )
        except Exception:
            pass

    # 거래일자 미검출은 청구자 에이전트(LLM)의 판단을 거칠 이유가 없는
    # 결정론적 규칙이다 — agent/claimant/agent.py의 needs_requery 조건 (b)와
    # 같은 규칙이지만, LLM 호출 성공 여부·판정 확률에 기대지 않고 여기서
    # 코드로 바로 확정한다. apply_claimant_verdict는 청구자 draft 적용 경로
    # (ingest/routes.py task_apply_claimant_draft)와 동일해서, 이 receipt는
    # 청구자 에이전트를 아예 부르지 않는다.
    #
    # TODO: 지금은 파싱(PARSED 확정) 이후에나 걸러진다 — 큐 인입(RECEIVED)
    # 시점엔 아직 이미지를 못 봐 걸러낼 수 없다(3초 ack 제약, ingest/routes.py
    # 참조). 데모 이후: 결정론적 사전 필터가 가능해지면 이 receipt를 애초에
    # claim으로 만들지 않고, 큐에 올리기 전에 재촬영 요청만 보내도록 옮긴다.
    if not signals.transaction_date_present:
        try:
            verdict = DraftVerdict(
                needs_requery=True,
                is_business=None,
                # 이 문안은 Slack DM으로 그대로 나간다 — 청구자 에이전트가 쓰는
                # requery_message와 같은 계약으로 영어다. 한때 한국어로 두고 api가
                # Gemma로 번역했지만, 번역 실패 시 조용히 한국어로 폴백해 같은 문구가
                # 오락가락했다(guards/agent_drafts.py 주석 참조).
                requery_message=(
                    "The transaction date cannot be read from the receipt. "
                    "Please take another photo so that the date is visible and send it again."
                ),
                reason="transaction_date 미검출 — 코드 결정론적 재요청(청구자 에이전트 미호출)",
            )
            # ingest/routes.py._requery_message는 DM 문안을 agent_drafts에서만
            # 읽는다 — 청구자 에이전트를 안 부르는 이 경로도 여기에 문서를 남겨야
            # 재촉 루프가 실제로 DM을 보낸다(청구자 에이전트가 쓰는 것과 같은
            # task_id 컨벤션: parsing/enqueue.py의 f"CLAIMANT:{receipt_id}").
            write_agent_draft_document(
                agent="CLAIMANT",
                target_type="RECEIPT",
                target_id=receipt_id,
                task_id=f"CLAIMANT:{receipt_id}",
                payload=verdict.model_dump(),
            )
            result, claim_request_id = apply_claimant_verdict(
                receipt_id,
                verdict,
                now=now,
                reason=ReminderReason.DATE_MISSING.value,
            )
            record_audit_log(
                actor=_ACTOR,
                action="RECEIPT_DATE_MISSING_REQUERY",
                after={"receipt_id": receipt_id, "result": result},
            )
            if result == "REQUERY" and claim_request_id:
                enqueue_remind(claim_request_id, delay_seconds=0)
        except Exception as e:
            try:
                record_audit_log(
                    actor=_ACTOR,
                    action="RECEIPT_DATE_MISSING_REQUERY_FAILED",
                    reason=mask_pii(str(e)),
                    after={"receipt_id": receipt_id},
                )
            except Exception:
                pass
        return "PARSED"

    # PARSED일 때만 청구자 에이전트를 부른다. FAILED는 재촉 루프 몫이다.
    #
    # 여기서 이미 갖고 있는 updates를 그대로 넘긴다 — Firestore 재조회는 낭비다.
    # 원문은 parsed.raw_text가 아니라 raw_text_gcs_uri로 나간다(§9 입력 계약) —
    # 태스크 본문은 큐에 영속화되고 로그에 남으므로 원문을 실으면 §2가 막아둔
    # 유출 경로가 다시 열린다. 에이전트가 이 URI로 GCS에서 직접 읽는다.
    # llm_confidence는 태스크 본문에서는 parse_confidence로 나간다.
    try:
        enqueue_claimant_review(
            receipt_id,
            receipt={
                "merchant_name": updates["merchant_name"],
                "transaction_date": updates["transaction_date"],
                "parsed_amount_minor": updates["parsed_amount_minor"],
                "currency": updates["currency"],
                "account_category_code": updates["account_category_code"],
                "parse_confidence": updates["llm_confidence"],
                "raw_text_gcs_uri": updates["raw_text_gcs_uri"],
                "org_id": receipt["org_id"],
                "recipient_id": receipt["recipient_id"],
            },
        )
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
