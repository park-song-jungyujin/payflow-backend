"""src/settlements/store.py — get_agent_draft(agent_drafts 유일한 읽기) 단위 테스트.

get_receipts/save_verification_result/create_verification_failed_claim_request는
tests/settlements/test_verification.py가 store.get_client를 통째로 monkeypatch해
간접적으로 덮는다 — 여기서는 새로 추가한 get_agent_draft만 직접 검증한다.
"""

from src.settlements import store


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data else None


class _FakeDocRef:
    def __init__(self, data):
        self._data = data

    def get(self):
        return _FakeSnapshot(self._data)


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def document(self, doc_id):
        return _FakeDocRef(self._docs.get(doc_id))


class _FakeClient:
    def __init__(self, agent_drafts):
        self._agent_drafts = agent_drafts

    def collection(self, name):
        assert name == "agent_drafts"
        return _FakeCollection(self._agent_drafts)


def test_returns_none_when_draft_not_written(monkeypatch):
    monkeypatch.setattr(store, "get_client", lambda: _FakeClient({}))

    assert store.get_agent_draft("EXECUTOR:run_1") is None


def test_returns_draft_dict_when_present(monkeypatch):
    draft = {
        "draft_id": "drf_EXECUTOR:run_1",
        "agent": "EXECUTOR",
        "target_type": "SETTLEMENT_RUN",
        "target_id": "run_1",
        "task_id": "EXECUTOR:run_1",
        "payload": {"anomalies": [], "summary_text": "이상 없음"},
        "created_at": "2026-08-21T00:00:00Z",
    }
    monkeypatch.setattr(
        store, "get_client", lambda: _FakeClient({"EXECUTOR:run_1": draft})
    )

    assert store.get_agent_draft("EXECUTOR:run_1") == draft
