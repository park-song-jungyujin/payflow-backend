"""schema-contract.md §2 — receipts 생성과 Slack 재전송 dedup."""

import pytest

from src.ingest import store


class FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = True

    def to_dict(self):
        return dict(self._data)


class FakeQuery:
    """where().limit().stream() 체인만 흉내낸다."""

    def __init__(self, docs, field=None, value=None, limit=None):
        self._docs, self._field, self._value, self._limit = docs, field, value, limit

    def where(self, filter=None):
        return FakeQuery(self._docs, filter.field_path, filter.value, self._limit)

    def limit(self, n):
        return FakeQuery(self._docs, self._field, self._value, n)

    def stream(self, transaction=None):
        hits = [d for d in self._docs if d.to_dict().get(self._field) == self._value]
        return iter(hits[: self._limit] if self._limit else hits)


class FakeDocRef:
    def __init__(self, store_dict, doc_id):
        self._store, self.id = store_dict, doc_id

    def set(self, data):
        self._store[self.id] = data


class FakeCollection:
    def __init__(self, store_dict):
        self._store = store_dict

    def where(self, filter=None):
        docs = [FakeDoc(k, v) for k, v in self._store.items()]
        return FakeQuery(docs).where(filter=filter)

    def document(self, doc_id):
        return FakeDocRef(self._store, doc_id)


class FakeTransaction:
    """Firestore 트랜잭션의 set만 흉내낸다. 커밋은 즉시 반영된다."""

    def set(self, ref, data):
        ref.set(data)


class FakeClient:
    def __init__(self):
        self.data = {"recipients": {}, "receipts": {}}

    def collection(self, name):
        return FakeCollection(self.data.setdefault(name, {}))


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(store, "get_client", lambda: client)
    # 트랜잭션 래퍼만 갈아끼운다 — 실제 Firestore 없이 콜백 본문을 돌린다.
    monkeypatch.setattr(
        store, "_run_in_transaction", lambda fn: fn(FakeTransaction())
    )
    return client


def test_finds_recipient_by_slack_user(fake_client):
    fake_client.data["recipients"]["rcp_1"] = {
        "recipient_id": "rcp_1",
        "slack_user_id": "U01ABCDEF",
    }
    assert store.find_recipient_by_slack_user("U01ABCDEF")["recipient_id"] == "rcp_1"


def test_unknown_slack_user_returns_none(fake_client):
    assert store.find_recipient_by_slack_user("U_NOBODY") is None


def test_creates_receipt_in_received_status(fake_client):
    receipt_id, created = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F01ABCDEF",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    assert created is True
    assert receipt_id.startswith("rct_")
    doc = fake_client.data["receipts"][receipt_id]
    assert doc["status"] == "RECEIVED"
    assert doc["slack_file_id"] == "F01ABCDEF"
    # 파싱 파이프라인 몫은 자리조차 만들지 않는다 — 만들어 두면 "채워졌는지"를
    # 구분할 수 없어진다.
    assert "image_gcs_uri" not in doc
    assert "parse_signals" not in doc


def test_slack_retry_does_not_create_second_receipt(fake_client):
    """Slack은 ack가 3초를 넘기면 같은 이벤트를 최대 3회 재전송한다."""
    first_id, first_created = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F01ABCDEF",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    second_id, second_created = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F01ABCDEF",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    assert first_created is True
    assert second_created is False
    assert second_id == first_id
    assert len(fake_client.data["receipts"]) == 1


def test_different_files_in_same_message_create_two_receipts(fake_client):
    """한 메시지에 이미지 2장이면 message_ts가 같다. file_id로만 갈린다 —
    이게 channel_id + message_ts를 dedup 키로 못 쓰는 이유다."""
    a, _ = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F_AAA",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    b, _ = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F_BBB",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    assert a != b
    assert len(fake_client.data["receipts"]) == 2


def test_created_receipt_validates_against_contract(fake_client):
    """schema-contract.md §2 — 인입이 쓴 문서가 Receipt로 검증돼야 한다."""
    from src.schemas.models import Receipt

    receipt_id, _ = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F01ABCDEF",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    Receipt.model_validate(fake_client.data["receipts"][receipt_id])
