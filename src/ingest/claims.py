"""schema-contract.md §2 `claims` — 파싱 결과에서 청구 항목을 만든다 (A 소유).

**왜 ingest/인가:** 계약 §10이 `/claims/{claim_id}/confirm`의 핸들러 위치를
`api/src/ingest/`로 지정한다. claim 관련 코드를 여기 두는 게 팀이 정한 배치다.
`src/claims/`를 새로 만들면 소유권 표에 없는 디렉터리가 생겨 계약 변경이 필요하다.

**금액을 만들지 않는다.** 영수증의 `parsed_amount_minor`를 그대로 옮긴다 —
공통 CLAUDE.md 절대 규칙 3. 이 파일에 산술이 있으면 안 된다.
"""

from datetime import datetime

from ulid import ULID

from ..schemas.enums import AccountCategory, ClaimStatus

# 청구자 에이전트(§9)가 업무용/개인용을 판단하는 필드인데 아직 501 스텁이라
# 코드가 기본값을 넣는다. True인 이유: 경비 청구를 개인 지출로 올릴 이유가 없고,
# 반대 방향 오판이 더 나쁘다 — 업무 경비가 개인용으로 분류되면 청구에서
# 조용히 빠지고 아무도 모른다.
#
# ⚠️ 에이전트가 붙으면 이 값을 draft로 덮어써야 한다. 덮어쓰기는 claim이
# DRAFT일 때만 허용한다 — CONFIRMED 이상은 이미 정산 후보이거나 점유된
# 상태라, 금액·분류가 바뀌면 approval_amount_hash(§7)와 어긋난다.
DEFAULT_IS_BUSINESS = True

# 지금은 청구자 에이전트가 없어 검토 단계 없이 바로 정산 후보가 된다.
# 에이전트가 붙으면 이 값을 ClaimStatus.DRAFT로 바꾸고, 에이전트 draft의
# needs_requery=false를 확인한 뒤 CONFIRMED로 승격한다. 그 전환이 상수
# 하나로 끝나도록 여기 뽑아 둔다.
CLAIM_STATUS_ON_CREATE = ClaimStatus.CONFIRMED


class ClaimNotCreatable(RuntimeError):
    """이 영수증으로는 청구를 만들 수 없다. 금액이나 수취인이 없다는 뜻이다."""


def build_claim(receipt: dict, *, now: datetime) -> dict:
    """claim 문서(dict)를 만든다. Firestore에 쓰지는 않는다.

    쓰기를 분리한 이유: 호출부가 이 문서를 receipts 갱신과 **한 트랜잭션**으로
    묶어야 한다. 여기서 직접 쓰면 그렇게 못 한다.
    """
    amount_minor = receipt.get("parsed_amount_minor")
    currency = receipt.get("currency")
    recipient_id = receipt.get("recipient_id")

    # 금액 없는 청구를 0원으로 만들지 않는다. 못 만들면 안 만드는 게 맞다 —
    # 영수증은 PARSED로 남고 사람이 대시보드에서 볼 수 있다.
    if amount_minor is None or not currency or not recipient_id:
        raise ClaimNotCreatable(
            f"receipt {receipt.get('receipt_id')} lacks amount/currency/recipient"
        )

    # account_category_code는 claims에서 nullable이 아니다(§2). 영수증에 없으면
    # UNCLASSIFIED로 채운다 — 분류를 못 한 것이 청구를 막을 이유는 아니고,
    # 집행자 에이전트가 나중에 재분류한다(§9).
    category = receipt.get("account_category_code") or AccountCategory.UNCLASSIFIED.value

    return {
        "claim_id": f"clm_{ULID()}",
        "recipient_id": recipient_id,
        "receipt_id": receipt["receipt_id"],
        "amount_minor": amount_minor,
        "currency": currency,
        "account_category_code": category,
        "is_business": DEFAULT_IS_BUSINESS,
        # ★ 명시적 None이어야 한다. B의 list_confirmed_claims가
        # `settlement_run_id == None`으로 필터하는데, Firestore는 필드가 없는
        # 문서를 그 쿼리로 잡지 못한다 — 생략하면 정산 후보에 조용히 안 잡힌다.
        "settlement_run_id": None,
        "settled_at": None,
        "status": CLAIM_STATUS_ON_CREATE.value,
        "created_at": now,
        "updated_at": now,
    }
