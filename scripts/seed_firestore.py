"""schema-contract.md §12 — tests/fixtures/*.json 8종을 Firestore에 적재한다.

재실행 가능하다: 문서 ID로 덮어쓰므로 데모 리셋은 다시 돌리기만 하면 된다.
fixtures는 읽기만 하고 수정하지 않는다(schema-contract.md §0 소유권).

사용법:
    cd backend && python scripts/seed_firestore.py
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.payouts.store import get_client  # noqa: E402  (load_dotenv 이후 import)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
_SUB_CASE_KEYS = ("sub_case_a_cap_exceeded_at_approve", "sub_case_b_payouts_without_token")

# schema-contract.md §1 — 시각 필드는 Firestore Timestamp. transaction_date만 예외(날짜 문자열).
_TIMESTAMP_KEYS = {
    "created_at",
    "updated_at",
    "settled_at",
    "reminded_at",
    "expires_at",
    "fx_locked_at",
    "approval_token_expires_at",
    "approval_token_used_at",
    "approved_at",
    "ts",
}

_COLLECTIONS = (
    "recipients",
    "receipts",
    "claim_requests",
    "claims",
    "settlement_runs",
    "sender_items",
    "agent_drafts",
)

_ID_FIELDS = {
    "recipients": "recipient_id",
    "receipts": "receipt_id",
    "claim_requests": "claim_request_id",
    "claims": "claim_id",
    "settlement_runs": "settlement_run_id",
    "sender_items": "sender_item_id",
    "agent_drafts": "draft_id",
}


def _convert_timestamps(value):
    if isinstance(value, dict):
        return {
            k: (
                datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC)
                if k in _TIMESTAMP_KEYS and isinstance(v, str)
                else _convert_timestamps(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_convert_timestamps(v) for v in value]
    return value


def _blocks(data: dict) -> list[dict]:
    return [data] + [data[key] for key in _SUB_CASE_KEYS if key in data]


# schema-contract.md "검증" — fixture는 수정 금지(§0)이고 verified_at을 모른다.
# 정산까지 가는 시나리오(status=PARSED)가 검증 미통과 취급으로 후보에서 빠지지
# 않도록, 시딩 시점에 통과 판정을 채워 넣는다. fixture 9(검증 실패)가 생기면
# 그쪽은 status가 VERIFICATION_FAILED라 이 보정을 타지 않는다.
# fixture 06(프롬프트 인젝션)은 parse_signals.injection_suspected=true라 제외한다 —
# 그 시나리오의 목적이 인젝션 탐지 시연인데 검증 통과로 덮어쓰면 무의미해진다.
# claim/settlement_run이 없는 receipt라 제외해도 다른 데모가 막히지 않는다.
_PASSED_VERIFICATION_SIGNALS = {
    "image_legible": True,
    "amount_matches": True,
    "merchant_matches": True,
    "date_matches": True,
    "injection_suspected": False,
}


def _backfill_verification(doc: dict) -> dict:
    injection_suspected = doc.get("parse_signals", {}).get("injection_suspected", False)
    if doc.get("status") == "PARSED" and doc.get("verified_at") is None and not injection_suspected:
        doc["verified_at"] = datetime.now(UTC)
        doc["verification_signals"] = dict(_PASSED_VERIFICATION_SIGNALS)
    return doc


def seed() -> None:
    client = get_client()
    counts: dict[str, int] = {c: 0 for c in _COLLECTIONS}

    for path in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        for block in _blocks(data):
            for collection in _COLLECTIONS:
                for doc in block.get(collection, []):
                    doc = _convert_timestamps(doc)
                    if collection == "receipts":
                        doc = _backfill_verification(doc)
                    doc_id = doc.get(_ID_FIELDS[collection]) if collection in _ID_FIELDS else None
                    ref = client.collection(collection).document(doc_id) if doc_id else client.collection(
                        collection
                    ).document()
                    ref.set(doc)
                    counts[collection] += 1

    for collection, count in counts.items():
        if count:
            print(f"{collection}: {count} docs seeded")


if __name__ == "__main__":
    seed()
