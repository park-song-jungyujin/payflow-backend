"""schema-contract.md §6 결정론적 매칭 — 후보 claim들 사이의 중복 청구 탐지.

같은 recipient의 claim 두 건이 금액·날짜윈도우·가맹점명 세 축 모두 일치하면
중복 의심으로 묶는다. LLM을 쓰지 않는다(agent-tools.md) — 흔들리면 데모가
무너지는 유일한 지점이다.

이 함수는 후보를 배치에서 빼지 않는다. fixture 03의 주석대로("코드 게이트[claim
CAS 점유, exclude_claim_ids]는 여기서 중복을 막지 않는다 — 차단은 사람이
risk_report를 보고 판단해야 한다") 판정 결과는 사람에게 보여줄 근거일 뿐이다.

이 결과를 누가 서술하는지(집행자 vs 안전 확인 에이전트)는 agent-tools.md 담당
표와 fixture 03 실제 데이터가 서로 다르게 가리키고 있어 아직 미정이다 — 그
배선은 이 함수의 소관이 아니다. 호출자가 정한다.

세 축 중 하나라도 어긋나는 부분 일치 쌍은 여기서 판정하지 않는다. "하나라도
어긋나면 집행자 에이전트가 서술한다"(§6)는 확정 판정 바깥의 얘기라 포함하지
않는다 — 필요해지면 별도 함수로 추가한다.
"""

import os
from datetime import date, timedelta


def _normalize_merchant(name: str) -> str:
    """공백 제거 후 casefold. 그 이상의 정규화 규칙(법인 접미사 제거 등)은
    실제 파싱 데이터를 보고 필요할 때 추가한다 — 지금은 근거가 없다."""
    return "".join(name.split()).casefold()


def find_duplicate_groups(claims: list[dict], receipts: dict[str, dict]) -> list[dict]:
    """같은 recipient_id의 후보 claim들 중 금액·날짜윈도우·가맹점명이 모두
    일치하는 클러스터를 찾는다.

    claims: candidate claim dict 목록. 각 항목은 최소 claim_id, recipient_id,
        receipt_id, amount_minor, currency를 갖는다. select_claims_for_run의
        반환값을 그대로 넣을 수 있다.
    receipts: receipt_id -> receipt dict. merchant_name 또는 transaction_date가
        None인 receipt는 판정에서 제외한다 — 근거 없는 필드는 비교하지 않는다
        (§2 검증 절의 verify_passed와 동일한 원칙).

    반환: [{"claim_ids": [...], "amount_minor": int, "currency": str,
            "merchant_name": str, "transaction_date": "YYYY-MM-DD"}, ...]
    claim_id가 2개 미만인 그룹은 만들지 않는다. 순서는 보장하지 않는다.
    """
    window = timedelta(days=int(os.environ.get("MATCHING_DATE_WINDOW_DAYS", "3")))

    comparable = []
    for c in claims:
        receipt = receipts.get(c["receipt_id"])
        if receipt is None:
            continue
        merchant = receipt.get("merchant_name")
        txn_date = receipt.get("transaction_date")
        if merchant is None or txn_date is None:
            continue
        if isinstance(txn_date, str):
            txn_date = date.fromisoformat(txn_date)
        comparable.append(
            {
                "claim_id": c["claim_id"],
                "recipient_id": c["recipient_id"],
                "amount_minor": c["amount_minor"],
                "currency": c["currency"],
                "merchant_norm": _normalize_merchant(merchant),
                "merchant_name": merchant,
                "transaction_date": txn_date,
            }
        )

    # union-find로 서로 일치하는 claim들을 클러스터로 묶는다.
    parent = {c["claim_id"]: c["claim_id"] for c in comparable}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, a in enumerate(comparable):
        for b in comparable[i + 1 :]:
            if a["recipient_id"] != b["recipient_id"]:
                continue
            if a["amount_minor"] != b["amount_minor"] or a["currency"] != b["currency"]:
                continue
            if abs(a["transaction_date"] - b["transaction_date"]) > window:
                continue
            if a["merchant_norm"] != b["merchant_norm"]:
                continue
            union(a["claim_id"], b["claim_id"])

    groups: dict[str, list[dict]] = {}
    for c in comparable:
        groups.setdefault(find(c["claim_id"]), []).append(c)

    return [
        {
            "claim_ids": [m["claim_id"] for m in members],
            "amount_minor": members[0]["amount_minor"],
            "currency": members[0]["currency"],
            "merchant_name": members[0]["merchant_name"],
            "transaction_date": members[0]["transaction_date"].isoformat(),
        }
        for members in groups.values()
        if len(members) >= 2
    ]


def find_exact_duplicate_receipts(
    claims: list[dict],
    receipts: dict[str, dict],
    *,
    settled_claims: list[dict] | None = None,
    settled_receipts: dict[str, dict] | None = None,
) -> list[dict]:
    """같은 recipient_id의 후보 claim들 중 receipt_serial_number_hash(카드
    승인번호 · 영수증 고유번호를 해시한 값, parsing/masking.py.
    hash_receipt_serial_number)가 완전히 일치하는 클러스터를 찾는다.

    반드시 해시로 비교해야 한다 — 원문(마스킹 전 값)을 그대로 비교하면 원문을
    Firestore에 들고 있어야 해 §2를 어기고, mask_pii로 마스킹한 값을 비교하면
    순수 숫자 승인번호가 카드번호 패턴에 걸려 전부 같은 "[CARD]"로 뭉개져
    무관한 영수증들이 완전일치로 오판된다(실제로 있었던 버그).

    find_duplicate_groups(금액·날짜윈도우·가맹점명 퍼지 매칭)보다 훨씬 강한 신호다
    — 승인번호는 카드사/POS가 거래 건마다 고유하게 발급하므로, OCR이 정확히
    읽었다면 서로 다른 실제 거래가 우연히 같은 값을 낼 확률이 사실상 없다. 같은
    물리적 영수증을 서로 다른 사진으로 두 번 올린 경우(예: slack_file_id가 달라
    receipt_dedup_keys가 못 잡는 경우)를 잡기 위한 것이다.

    receipt_serial_number_hash가 None이거나 빈 문자열인 receipt는 판정에서
    제외한다(근거 없는 필드는 비교하지 않는다 — find_duplicate_groups와 같은
    원칙). amount_minor도 함께 일치해야 클러스터로 묶는다 — OCR 오독으로 서로
    다른 영수증이 우연히 같은 문자열을 내는 극단적인 경우를 막는 이중 확인이다.

    claims, receipts: find_duplicate_groups와 같은 형태 — **이번 배치 후보만**.

    settled_claims, settled_receipts: 이미 IN_RUN·SETTLED로 전이된 과거 claim들
    (payouts.store.list_settled_claims). 생략하면(None) 이번 배치 후보끼리만
    비교한다 — 과거 배치를 함께 대조하지 않으면, 이미 송금까지 끝난 영수증이
    나중 배치에 다시 청구돼도 아무 것도 못 잡는다(같은 배치 안에서만 비교하는
    한계). 과거 claim은 지금 와서 빼거나 막을 대상이 아니므로 반환값의
    "claim_ids"에는 넣지 않고, "already_settled_claim_ids"에 참고용으로만 담는다.

    반환: [{"claim_ids": [...이번 배치 candidate만...],
            "already_settled_claim_ids": [...과거 claim, 없으면 빈 리스트...],
            "receipt_serial_number_hash": str, "amount_minor": int, "currency": str},
           ...].
    이번 배치 candidate가 하나도 없는 그룹(과거 claim끼리만 겹침)은 만들지 않는다
    — 지금 새로 보여줄 게 없다.
    """

    def _comparable(claim_list: list[dict], receipt_map: dict[str, dict], *, is_settled: bool):
        out = []
        for c in claim_list:
            receipt = receipt_map.get(c["receipt_id"])
            if receipt is None:
                continue
            serial_hash = receipt.get("receipt_serial_number_hash")
            if not serial_hash:
                continue
            out.append(
                {
                    "claim_id": c["claim_id"],
                    "recipient_id": c["recipient_id"],
                    "amount_minor": c["amount_minor"],
                    "currency": c["currency"],
                    "receipt_serial_number_hash": serial_hash,
                    "is_settled": is_settled,
                }
            )
        return out

    comparable = _comparable(claims, receipts, is_settled=False) + _comparable(
        settled_claims or [], settled_receipts or {}, is_settled=True
    )

    parent = {c["claim_id"]: c["claim_id"] for c in comparable}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, a in enumerate(comparable):
        for b in comparable[i + 1 :]:
            if a["recipient_id"] != b["recipient_id"]:
                continue
            if a["amount_minor"] != b["amount_minor"] or a["currency"] != b["currency"]:
                continue
            if a["receipt_serial_number_hash"] != b["receipt_serial_number_hash"]:
                continue
            union(a["claim_id"], b["claim_id"])

    groups: dict[str, list[dict]] = {}
    for c in comparable:
        groups.setdefault(find(c["claim_id"]), []).append(c)

    result = []
    for members in groups.values():
        candidate_ids = [m["claim_id"] for m in members if not m["is_settled"]]
        settled_ids = [m["claim_id"] for m in members if m["is_settled"]]
        if len(candidate_ids) >= 2 or (candidate_ids and settled_ids):
            result.append(
                {
                    "claim_ids": candidate_ids,
                    "already_settled_claim_ids": settled_ids,
                    "receipt_serial_number_hash": members[0]["receipt_serial_number_hash"],
                    "amount_minor": members[0]["amount_minor"],
                    "currency": members[0]["currency"],
                }
            )
    return result
