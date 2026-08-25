"""get_or_create_default_org_id — 여러 org가 있을 때 기본 기관을 고르는 규칙."""

from datetime import UTC, datetime

from src.auth import store


class FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class FakeQuery:
    def __init__(self, docs):
        self._docs = docs
        self._order = None
        self._limit = None

    def order_by(self, field):
        self._order = field
        return self

    def limit(self, n):
        self._limit = n
        return self

    def stream(self):
        docs = self._docs
        if self._order:
            docs = sorted(docs, key=lambda d: d[1][self._order])
        if self._limit:
            docs = docs[: self._limit]
        return iter([FakeSnapshot(doc_id, data) for doc_id, data in docs])


class FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def limit(self, n):
        return FakeQuery(self._docs).limit(n)

    def order_by(self, field):
        return FakeQuery(self._docs).order_by(field)


class FakeClient:
    def __init__(self, docs):
        self._docs = docs

    def collection(self, name):
        assert name == "orgs"
        return FakeCollection(self._docs)


def test_returns_earliest_created_org_regardless_of_iteration_order(monkeypatch):
    """orgs가 여러 개 있으면 문서 ID 정렬 우연이 아니라 created_at으로 가장
    이른 org를 기본 기관으로 고른다."""
    docs = [
        ("org_created_second", {"created_at": datetime(2026, 8, 24, 15, 0, tzinfo=UTC)}),
        ("org_created_first", {"created_at": datetime(2026, 8, 24, 10, 0, tzinfo=UTC)}),
        ("org_created_third", {"created_at": datetime(2026, 8, 25, 9, 0, tzinfo=UTC)}),
    ]
    monkeypatch.setattr(store, "get_client", lambda: FakeClient(docs))

    assert store.get_or_create_default_org_id() == "org_created_first"
