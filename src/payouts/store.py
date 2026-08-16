"""schema-contract.md §2 Firestore 접근 — C 소유 컬렉션(settlement_runs, sender_items,
claims, recipients)의 유일한 읽기/쓰기 창구.

읽기 함수는 dict를 반환하지만 그 dict를 in-place mutate해도 Firestore에는
반영되지 않는다 — 쓰기는 반드시 이 모듈의 update_*/increment_* 함수를 통해서만
한다. 데모 fixture 시딩은 `scripts/seed_firestore.py`가 한다(이 모듈은 시딩하지 않는다).
"""

import os

from google.cloud import firestore

_client: firestore.Client | None = None


def get_client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(
            project=os.environ.get("GCP_PROJECT"),
            database=os.environ.get("FIRESTORE_DATABASE", "(default)"),
        )
    return _client


def get_settlement_run(run_id: str) -> dict | None:
    doc = get_client().collection("settlement_runs").document(run_id).get()
    return doc.to_dict() if doc.exists else None


def update_settlement_run(run_id: str, updates: dict) -> None:
    get_client().collection("settlement_runs").document(run_id).update(updates)


def get_sender_items(run_id: str) -> list[dict]:
    docs = get_client().collection("sender_items").where(
        "settlement_run_id", "==", run_id
    ).stream()
    return [d.to_dict() for d in docs]


def set_sender_items(run_id: str, items: list[dict]) -> None:
    """execute-payout/reconcile이 PayPal 대조 결과로 만든 sender_items를 저장한다.
    `sender_item_id`를 문서 ID로 써서 재발송(retry) 항목과 충돌하지 않는다."""
    batch = get_client().batch()
    col = get_client().collection("sender_items")
    for item in items:
        batch.set(col.document(item["sender_item_id"]), item)
    batch.commit()


def get_claims_for_run(run_id: str) -> list[dict]:
    docs = get_client().collection("claims").where(
        "settlement_run_id", "==", run_id
    ).stream()
    return [d.to_dict() for d in docs]


def update_claim(claim_id: str, updates: dict) -> None:
    get_client().collection("claims").document(claim_id).update(updates)


def get_recipient(recipient_id: str) -> dict | None:
    doc = get_client().collection("recipients").document(recipient_id).get()
    return doc.to_dict() if doc.exists else None


def increment_recipient_monthly(recipient_id: str, delta_minor: int) -> None:
    """schema-contract.md §2 monthly_paid_minor 예약/역산. 원자적 증분이라
    read-modify-write 경합이 없다."""
    get_client().collection("recipients").document(recipient_id).update(
        {"monthly_paid_minor": firestore.Increment(delta_minor)}
    )


def get_sole_recipient_id(run_id: str) -> str | None:
    """claims가 전부 같은 recipient면 그 id, 하나도 없거나 둘 이상이면 None.
    여러 recipient의 통화 혼합 청구를 base_currency로 합산하는 로직은
    matching(Track B)의 결과물이 필요한데 아직 없다 — 지금은 단일 recipient run만
    다룰 수 있다."""
    recipient_ids = {c["recipient_id"] for c in get_claims_for_run(run_id)}
    return next(iter(recipient_ids)) if len(recipient_ids) == 1 else None


def find_run_id_by_payout_batch_id(payout_batch_id: str) -> str | None:
    docs = (
        get_client()
        .collection("settlement_runs")
        .where("payout_batch_id", "==", payout_batch_id)
        .limit(1)
        .stream()
    )
    doc = next(iter(docs), None)
    return doc.id if doc else None
