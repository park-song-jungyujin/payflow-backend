"""schema-contract.md §2 `claims` — "claim 점유는 CAS 트랜잭션이다. 한 claim이
두 배치에 들어가면 이중 지급이다." 이 스위트가 지키는 것: CONFIRMED가 아닌 claim은
조용히 빠지고, CONFIRMED인 것만 IN_RUN + settlement_run_id로 전이한다.
"""

from datetime import UTC, datetime

import pytest

from src.guards import claims


class FakeSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data else None


class FakeDocRef:
    def __init__(self, doc_id, data):
        self.doc_id = doc_id
        self._data = data

    def get(self, transaction=None):
        return FakeSnapshot(self._data)


class FakeCollection:
    def __init__(self, docs_by_id):
        self._docs_by_id = docs_by_id

    def document(self, doc_id):
        return FakeDocRef(doc_id, self._docs_by_id.get(doc_id))


class FakeTransaction:
    def __init__(self):
        self.updates = []

    def update(self, ref, data):
        self.updates.append((ref.doc_id, data))


class FakeClient:
    def __init__(self, docs_by_id):
        self._docs_by_id = docs_by_id
        self.last_transaction = None

    def collection(self, name):
        assert name == "claims"
        return FakeCollection(self._docs_by_id)

    def transaction(self):
        self.last_transaction = FakeTransaction()
        return self.last_transaction


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    """firestore.transactional을 항등 데코레이터로 바꿔 실제 Firestore 없이
    트랜잭션 콜백 본문을 그대로 돌린다 — tests/guards/test_tokens.py와 같은 전략."""
    monkeypatch.setattr(claims.firestore, "transactional", lambda fn: fn)


def _claim(claim_id, status="CONFIRMED"):
    return {"claim_id": claim_id, "status": status}


def _run_with_client(monkeypatch, docs_by_id):
    client = FakeClient(docs_by_id)
    monkeypatch.setattr(claims, "get_client", lambda: client)
    return client


def test_confirmed_claims_all_link(monkeypatch):
    docs = {"clm_1": _claim("clm_1"), "clm_2": _claim("clm_2")}
    client = _run_with_client(monkeypatch, docs)

    linked = claims.link_claims_to_run_cas("run_1", ["clm_1", "clm_2"])

    assert set(linked) == {"clm_1", "clm_2"}
    updated_ids = {doc_id for doc_id, _ in client.last_transaction.updates}
    assert updated_ids == {"clm_1", "clm_2"}
    for _, update in client.last_transaction.updates:
        assert update["settlement_run_id"] == "run_1"
        assert update["status"] == "IN_RUN"


def test_already_in_run_claim_excluded_without_failing_batch(monkeypatch):
    """동시에 다른 배치가 먼저 채간 claim(CONFIRMED가 아님)은 조용히 빠진다 —
    나머지가 전이에 실패하지 않는다."""
    docs = {"clm_1": _claim("clm_1", status="CONFIRMED"), "clm_2": _claim("clm_2", status="IN_RUN")}
    client = _run_with_client(monkeypatch, docs)

    linked = claims.link_claims_to_run_cas("run_1", ["clm_1", "clm_2"])

    assert linked == ["clm_1"]
    updated_ids = {doc_id for doc_id, _ in client.last_transaction.updates}
    assert updated_ids == {"clm_1"}


def test_missing_claim_excluded(monkeypatch):
    docs = {"clm_1": _claim("clm_1")}
    client = _run_with_client(monkeypatch, docs)

    linked = claims.link_claims_to_run_cas("run_1", ["clm_1", "clm_missing"])

    assert linked == ["clm_1"]
    assert len(client.last_transaction.updates) == 1


def test_no_confirmed_claims_returns_empty_list(monkeypatch):
    docs = {"clm_1": _claim("clm_1", status="SETTLED")}
    client = _run_with_client(monkeypatch, docs)

    linked = claims.link_claims_to_run_cas("run_1", ["clm_1"])

    assert linked == []
    assert client.last_transaction.updates == []


def test_updated_at_is_a_datetime(monkeypatch):
    docs = {"clm_1": _claim("clm_1")}
    client = _run_with_client(monkeypatch, docs)

    claims.link_claims_to_run_cas("run_1", ["clm_1"])

    _, update = client.last_transaction.updates[0]
    assert isinstance(update["updated_at"], datetime)
    assert update["updated_at"].tzinfo is UTC
