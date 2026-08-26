"""schema-contract.md §6 결정론적 매칭 — 후보 claim 중 거래일자가 미래인 건 탐지.

원래는 집행자 에이전트(payflow-agent)가 check_future_dated_claims 툴로 분석
실행 중에 직접 판정했다 — 날짜 산술은 LLM이 하면 안 되는 결정론적 계산이라서다
(agent-tools.md). 그 판정 자체는 여기로 옮겨도 원칙이 그대로다: LLM은 여전히
날짜를 스스로 계산하지 않고 이 함수의 결과만 근거로 서술한다. 달라지는 건
"언제" 계산하느냐뿐이다 — 분석 실행 시점(툴 호출) 대신 정산 실행(배치) 생성
시점(enqueue 직전)에 미리 계산해 candidate_claims와 나란히 태스크 본문에
싣는다. duplicate_groups·exact_duplicate_groups와 완전히 같은 패턴이다.

배치 생성과 에이전트 분석 사이에는 보통 몇 초 안의 지연만 있어 자정 경계에
걸리는 극단적인 경우가 아니면 문제되지 않는다 — 매 분석 호출마다 별도 LLM
tool-call 왕복(및 그 안의 네트워크 지연)을 만드는 비용이 이 근사보다 크다는
판단이다.
"""

from datetime import date


def find_future_dated_claims(
    claims: list[dict], receipts: dict[str, dict], *, today: date
) -> list[dict]:
    """claims 중 receipt의 transaction_date가 today보다 미래인 건을 찾는다.

    claims: candidate claim dict 목록 — 최소 claim_id, receipt_id를 갖는다.
    receipts: receipt_id -> receipt dict. transaction_date가 없는 receipt는
        판정에서 제외한다(근거 없는 필드는 비교하지 않는다 — find_duplicate_groups와
        같은 원칙).

    반환: [{"claim_id": str, "transaction_date": "YYYY-MM-DD"}, ...]. 순서는
    claims 순서를 따른다.
    """
    result = []
    for c in claims:
        receipt = receipts.get(c["receipt_id"])
        if receipt is None:
            continue
        txn_date = receipt.get("transaction_date")
        if txn_date is None:
            continue
        if isinstance(txn_date, str):
            txn_date = date.fromisoformat(txn_date)
        if txn_date > today:
            result.append({"claim_id": c["claim_id"], "transaction_date": txn_date.isoformat()})
    return result
