"""schema-contract.md §9 — 청구자 draft 판정을 상태 전이로 반영하는 트랜잭션.

`apply_claimant_verdict`는 receipts.status · claims.status · claim_requests 생성을
**한 트랜잭션**으로 묶는다. 갈라지면 "영수증은 NEEDS_REQUERY인데 claim은 CONFIRMED"인
창이 생기고, 그 사이 정산 배치가 돌면 분쟁 중인 영수증이 송금된다. 재시도로도 안
메워진다 — 두 번째 시도는 이미 NEEDS_REQUERY라 멱등 가드에 걸려 건너뛴다.

이 스위트도 test_store.py와 같은 한계를 갖는다: FakeTransaction은 락·ABORTED·재실행이
없어 즉시 반영된다. 검증하는 건 (1) 전이 로직 자체 (2) 구조 — 모든 읽기가 모든 쓰기보다
앞에 오고, 그 읽기가 `transaction=`을 받는지. 진짜 동시성 원자성은 에뮬레이터 몫으로
미룬다(store.py와 동일 판단).
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.ingest import store
from src.ingest.drafts import DraftVerdict


class FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def get(self, field):
        return self._data[field]

    def to_dict(self):
        return dict(self._data) if self._data else None


class FakeDocRef:
    def __init__(self, collection, store_dict, doc_id, log):
        self.collection_name, self._store, self.id, self._log = collection, store_dict, doc_id, log

    def get(self, transaction=None):
        self._log.append(("get", f"{self.collection_name}/{self.id}", transaction is not None))
        return FakeSnapshot(self.id, self._store.get(self.id))


class FakeQuery:
    def __init__(self, collection, store_dict, log, field=None, value=None, limit=None):
        self.collection_name, self._store, self._log = collection, store_dict, log
        self._field, self._value, self._limit = field, value, limit

    def where(self, filter=None):
        return FakeQuery(
            self.collection_name, self._store, self._log, filter.field_path, filter.value, self._limit
        )

    def limit(self, n):
        return FakeQuery(self.collection_name, self._store, self._log, self._field, self._value, n)

    def stream(self, transaction=None):
        self._log.append(("query", self.collection_name, transaction is not None))
        hits = [(doc_id, d) for doc_id, d in self._store.items() if d.get(self._field) == self._value]
        if self._limit:
            hits = hits[: self._limit]
        return iter([FakeSnapshot(doc_id, d) for doc_id, d in hits])


class FakeCollection:
    def __init__(self, name, store_dict, log):
        self._name, self._store, self._log = name, store_dict, log

    def document(self, doc_id):
        return FakeDocRef(self._name, self._store, doc_id, self._log)

    def where(self, filter=None):
        return FakeQuery(self._name, self._store, self._log).where(filter=filter)


class FakeTransaction:
    """set·update만 흉내낸다. 커밋은 즉시 반영되고 락·재시도는 없다."""

    def __init__(self, log):
        self._log = log

    def set(self, ref, data):
        self._log.append(("set", f"{ref.collection_name}/{ref.id}", True))
        ref._store[ref.id] = data

    def update(self, ref, data):
        self._log.append(("update", f"{ref.collection_name}/{ref.id}", True))
        current = dict(ref._store.get(ref.id) or {})
        current.update(data)
        ref._store[ref.id] = current


class FakeClient:
    def __init__(self, log):
        self.data = {"receipts": {}, "claims": {}, "claim_requests": {}}
        self._log = log

    def collection(self, name):
        return FakeCollection(name, self.data.setdefault(name, {}), self._log)


@pytest.fixture
def fake(monkeypatch):
    log = []
    client = FakeClient(log)
    monkeypatch.setattr(store, "get_client", lambda: client)
    monkeypatch.setattr(store, "_run_in_transaction", lambda fn: fn(FakeTransaction(log)))
    audit_calls = []
    monkeypatch.setattr(store, "record_audit_log", lambda **kwargs: audit_calls.append(kwargs))
    client.log = log
    client.audit_calls = audit_calls
    return client


NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _seed_receipt(fake, receipt_id="rct_1", status="PARSED", recipient_id="rcp_1", org_id="org_1"):
    fake.data["receipts"][receipt_id] = {
        "receipt_id": receipt_id,
        "org_id": org_id,
        "recipient_id": recipient_id,
        "status": status,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _seed_claim(fake, claim_id="clm_1", receipt_id="rct_1", status="CONFIRMED"):
    fake.data["claims"][claim_id] = {
        "claim_id": claim_id,
        "recipient_id": "rcp_1",
        "receipt_id": receipt_id,
        "amount_minor": 1000,
        "currency": "KRW",
        "account_category_code": "UNCLASSIFIED",
        "is_business": True,
        "settlement_run_id": None,
        "settled_at": None,
        "status": status,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _verdict(**overrides):
    kwargs = {"needs_requery": False}
    kwargs.update(overrides)
    return DraftVerdict(**kwargs)


def test_needs_requery_transitions_receipt_and_claim_and_creates_request(fake):
    _seed_receipt(fake)
    _seed_claim(fake, status="CONFIRMED")

    result, claim_request_id = store.apply_claimant_verdict(
        "rct_1", _verdict(needs_requery=True), now=NOW
    )

    assert result == "REQUERY"
    assert fake.data["receipts"]["rct_1"]["status"] == "NEEDS_REQUERY"
    assert fake.data["claims"]["clm_1"]["status"] == "DRAFT"
    assert len(fake.data["claim_requests"]) == 1
    request = next(iter(fake.data["claim_requests"].values()))
    # 호출부가 이 id로 재촉 루프를 깨운다 — 방금 만든 문서를 가리켜야 한다.
    assert claim_request_id == request["claim_request_id"]
    assert request["status"] == "PENDING"
    assert request["reason"] == "AMOUNT_MISMATCH"
    assert request["receipt_id"] == "rct_1"
    assert request["expires_at"] == NOW + timedelta(seconds=86400)


def test_needs_requery_leaves_in_run_claim_untouched_but_logs_audit(fake):
    _seed_receipt(fake)
    _seed_claim(fake, status="IN_RUN")

    result, _ = store.apply_claimant_verdict("rct_1", _verdict(needs_requery=True), now=NOW)

    assert result == "REQUERY"
    assert fake.data["claims"]["clm_1"]["status"] == "IN_RUN"
    assert fake.data["receipts"]["rct_1"]["status"] == "NEEDS_REQUERY"
    assert len(fake.data["claim_requests"]) == 1
    assert len(fake.audit_calls) == 1


def test_needs_requery_leaves_settled_claim_untouched_but_logs_audit(fake):
    _seed_receipt(fake)
    _seed_claim(fake, status="SETTLED")

    result, _ = store.apply_claimant_verdict("rct_1", _verdict(needs_requery=True), now=NOW)

    assert result == "REQUERY"
    assert fake.data["claims"]["clm_1"]["status"] == "SETTLED"
    assert len(fake.data["claim_requests"]) == 1
    assert len(fake.audit_calls) == 1


def test_no_requery_with_is_business_updates_claim_only(fake):
    _seed_receipt(fake)
    _seed_claim(fake, status="CONFIRMED")

    result, _ = store.apply_claimant_verdict(
        "rct_1", _verdict(needs_requery=False, is_business=True), now=NOW
    )

    assert result == "APPLIED"
    assert fake.data["claims"]["clm_1"]["is_business"] is True
    assert fake.data["receipts"]["rct_1"]["status"] == "PARSED"
    assert len(fake.data["claim_requests"]) == 0


def test_no_requery_without_is_business_writes_nothing(fake):
    _seed_receipt(fake)
    _seed_claim(fake, status="CONFIRMED")

    result, claim_request_id = store.apply_claimant_verdict(
        "rct_1", _verdict(needs_requery=False), now=NOW
    )

    assert result == "APPLIED"
    # claim_request를 안 만들었으면 돌려줄 id도 없다 — 호출부가 엉뚱한 문서를
    # 재촉하지 않게 한다.
    assert claim_request_id is None
    writes = [entry for entry in fake.log if entry[0] in ("set", "update")]
    assert writes == []


def test_receipt_already_needs_requery_is_skipped(fake):
    _seed_receipt(fake, status="NEEDS_REQUERY")
    _seed_claim(fake, status="DRAFT")

    result, _ = store.apply_claimant_verdict("rct_1", _verdict(needs_requery=True), now=NOW)

    assert result == "SKIPPED"
    writes = [entry for entry in fake.log if entry[0] in ("set", "update")]
    assert writes == []
    assert len(fake.data["claim_requests"]) == 0


def test_missing_receipt_is_skipped(fake):
    result, _ = store.apply_claimant_verdict("rct_missing", _verdict(needs_requery=True), now=NOW)

    assert result == "SKIPPED"
    writes = [entry for entry in fake.log if entry[0] in ("set", "update")]
    assert writes == []


def test_receipt_missing_status_field_is_skipped_not_500(fake):
    """F1 리뷰 지적 — receipts 문서에 status 필드가 아예 없으면(과거 스키마 등)
    snapshot.get("status")가 KeyError를 던져 500이 됐다. to_dict() 기반이면
    None이 되고, None != "PARSED"라 기존 "PARSED 아님 → SKIPPED" 분기로 자연히
    빠져야 한다."""
    fake.data["receipts"]["rct_1"] = {
        "receipt_id": "rct_1",
        "recipient_id": "rcp_1",
        "created_at": NOW,
        "updated_at": NOW,
        # status 필드 없음
    }
    _seed_claim(fake, status="CONFIRMED")

    result, _ = store.apply_claimant_verdict("rct_1", _verdict(needs_requery=True), now=NOW)

    assert result == "SKIPPED"
    writes = [entry for entry in fake.log if entry[0] in ("set", "update")]
    assert writes == []
    assert len(fake.data["claim_requests"]) == 0


def test_receipt_missing_recipient_id_field_is_skipped_not_500(fake):
    """F1 리뷰 지적 — receipts 문서에 recipient_id가 없으면
    snapshot.get("recipient_id")가 KeyError → 500이었다. status는 PARSED라
    "PARSED 아님" 분기로는 안 빠지므로 recipient_id 누락을 명시적으로 막아야
    한다 — 안 그러면 recipient_id=None인 claim_request가 나가 ClaimRequest
    스키마(non-nullable recipient_id)를 어긴다."""
    fake.data["receipts"]["rct_1"] = {
        "receipt_id": "rct_1",
        "status": "PARSED",
        "created_at": NOW,
        "updated_at": NOW,
        # recipient_id 필드 없음
    }
    _seed_claim(fake, status="CONFIRMED")

    result, _ = store.apply_claimant_verdict("rct_1", _verdict(needs_requery=True), now=NOW)

    assert result == "SKIPPED"
    writes = [entry for entry in fake.log if entry[0] in ("set", "update")]
    assert writes == []
    assert len(fake.data["claim_requests"]) == 0


def test_claim_missing_status_field_is_skipped_not_500(fake):
    """F1 리뷰 지적 — claims 문서에 status가 없으면 claim_snapshot.get("status")가
    KeyError → 500이었다. status를 알 수 없는 claim은 안전하게 판단할 수 없으므로
    receipt·claim_request 어느 쪽도 건드리지 않고 SKIPPED로 멈춰야 한다."""
    _seed_receipt(fake)
    fake.data["claims"]["clm_1"] = {
        "claim_id": "clm_1",
        "recipient_id": "rcp_1",
        "receipt_id": "rct_1",
        "amount_minor": 1000,
        "currency": "KRW",
        "account_category_code": "UNCLASSIFIED",
        "is_business": True,
        "settlement_run_id": None,
        "settled_at": None,
        "created_at": NOW,
        "updated_at": NOW,
        # status 필드 없음
    }

    result, _ = store.apply_claimant_verdict("rct_1", _verdict(needs_requery=True), now=NOW)

    assert result == "SKIPPED"
    writes = [entry for entry in fake.log if entry[0] in ("set", "update")]
    assert writes == []
    assert len(fake.data["claim_requests"]) == 0
    assert fake.data["receipts"]["rct_1"]["status"] == "PARSED"


def test_idempotency_same_draft_applied_twice(fake):
    """같은 draft를 연속 두 번 반영해도 claim_requests는 1건이어야 한다."""
    _seed_receipt(fake)
    _seed_claim(fake, status="CONFIRMED")
    verdict = _verdict(needs_requery=True)

    first, _ = store.apply_claimant_verdict("rct_1", verdict, now=NOW)
    second, _ = store.apply_claimant_verdict("rct_1", verdict, now=NOW)

    assert first == "REQUERY"
    assert second == "SKIPPED"
    assert len(fake.data["claim_requests"]) == 1


def test_idempotency_two_different_drafts_both_needs_requery(fake):
    """파싱 재시도로 draft가 두 건(task_id가 다름) 생겨도 claim_requests는 1건."""
    _seed_receipt(fake)
    _seed_claim(fake, status="CONFIRMED")
    verdict_a = DraftVerdict(needs_requery=True, requery_message="첫 번째 에이전트 호출")
    verdict_b = DraftVerdict(needs_requery=True, requery_message="재시도된 두 번째 호출")

    first, _ = store.apply_claimant_verdict("rct_1", verdict_a, now=NOW)
    second, _ = store.apply_claimant_verdict("rct_1", verdict_b, now=NOW)

    assert first == "REQUERY"
    assert second == "SKIPPED"
    assert len(fake.data["claim_requests"]) == 1


def test_idempotency_needs_requery_false_rerun_creates_no_new_document(fake):
    """needs_requery=false 경로는 영수증 상태를 안 바꿔 가드에 안 걸린다.
    같은 값 재적용은 무해하되, 새 문서가 생기면 안 된다."""
    _seed_receipt(fake)
    _seed_claim(fake, status="CONFIRMED")
    verdict = _verdict(needs_requery=False, is_business=True)

    store.apply_claimant_verdict("rct_1", verdict, now=NOW)
    store.apply_claimant_verdict("rct_1", verdict, now=NOW)

    assert len(fake.data["claims"]) == 1
    assert len(fake.data["claim_requests"]) == 0
    assert len(fake.data["receipts"]) == 1


def test_reads_before_any_write_structural(fake):
    """구조 테스트 — 모든 읽기가 모든 쓰기보다 앞서야 한다(Firestore 제약).
    첫 연산은 receipts 문서에 대한 `transaction=` 읽기여야 한다."""
    _seed_receipt(fake)
    _seed_claim(fake, status="CONFIRMED")

    store.apply_claimant_verdict("rct_1", _verdict(needs_requery=True), now=NOW)

    kinds = [entry[0] for entry in fake.log]
    first_write_idx = min(
        (i for i, k in enumerate(kinds) if k in ("set", "update")), default=len(kinds)
    )
    reads_after_write = [k for k in kinds[first_write_idx:] if k in ("get", "query")]
    assert reads_after_write == [], f"쓰기 뒤에 읽기가 있다: {fake.log}"

    assert kinds[0] == "get", f"첫 연산이 읽기가 아니다: {fake.log}"
    first_read = fake.log[0]
    assert first_read[1] == "receipts/rct_1"
    assert first_read[2] is True, "receipt 읽기가 트랜잭션 밖에서 일어났다"

    # 첫 읽기만 확인하면 그 뒤의 claims 쿼리가 transaction= 없이 실행돼도 이
    # 테스트는 안 죽는다 — 모든 읽기 엔트리가 트랜잭션 소속인지 봐야 한다.
    reads = [entry for entry in fake.log[:first_write_idx] if entry[0] in ("get", "query")]
    assert reads, f"읽기 로그가 비어 있다: {fake.log}"
    for entry in reads:
        assert entry[2] is True, f"트랜잭션 밖에서 일어난 읽기: {entry}"


def test_created_claim_request_validates_against_contract(fake):
    from src.schemas.models import ClaimRequest

    _seed_receipt(fake)
    _seed_claim(fake, status="CONFIRMED")

    store.apply_claimant_verdict("rct_1", _verdict(needs_requery=True), now=NOW)

    request = next(iter(fake.data["claim_requests"].values()))
    ClaimRequest.model_validate(request)


def test_claim_request_id_uses_crq_prefix_and_matches_doc_id(fake):
    """schema-contract.md §3 — claim_request_id는 crq_{ULID()}. 문서 ID와
    claim_request_id 필드가 어긋나면 조회 경로가 갈린다."""
    _seed_receipt(fake)
    _seed_claim(fake, status="CONFIRMED")

    store.apply_claimant_verdict("rct_1", _verdict(needs_requery=True), now=NOW)

    doc_id, request = next(iter(fake.data["claim_requests"].items()))
    assert doc_id.startswith("crq_"), f"문서 ID가 crq_ 접두사가 아니다: {doc_id}"
    assert request["claim_request_id"].startswith("crq_"), (
        f"claim_request_id 필드가 crq_ 접두사가 아니다: {request['claim_request_id']}"
    )
    assert request["claim_request_id"] == doc_id
