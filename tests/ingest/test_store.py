"""schema-contract.md §2 — receipts 생성과 Slack 재전송 dedup.

**이 스위트는 원자성을 검증하지 않는다.** 호출이 전부 순차이고 `FakeTransaction`은
락·ABORTED·재실행이 없어 즉시 반영된다. 검사와 쓰기를 트랜잭션 밖으로 꺼낸 잘못된
구현도 아래 테스트를 그대로 통과한다.

검증하는 것은 두 가지다.
1. dedup **로직** — 같은 `slack_file_id`는 문서를 하나만 만든다
2. dedup **기법** — 유일성을 문서 ID(`receipt_dedup_keys/{slack_file_id}`)에 대한
   `transaction.get`으로 강제하고, 그 읽기가 모든 쓰기보다 앞에 온다

2번이 구조 테스트다. 쿼리 기반 dedup으로 되돌아가는 회귀를 잡기 위해 있다 — 쿼리는
결과가 0건이면 잠글 문서가 없어 동시 재전송에서 뚫린다(§2 `receipt_dedup_keys`).

진짜 동시성 원자성은 Firestore 에뮬레이터로 스레드 두 개를 돌려야 증명된다. 6일
일정에 맞지 않아 통합 단계로 미뤘다.
"""

import pytest

from src.ingest import store


class FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def get(self, field):
        return self._data[field]

    def to_dict(self):
        return dict(self._data) if self._data else None


class FakeDocRef:
    def __init__(self, store_dict, doc_id, log):
        self._store, self.id, self._log = store_dict, doc_id, log

    def get(self, transaction=None):
        self._log.append(("get", self.id, transaction is not None))
        return FakeSnapshot(self._store.get(self.id))

    def set(self, data):
        self._store[self.id] = data


class FakeQuery:
    """where().limit().stream() 체인. recipients 조회에만 쓰인다."""

    def __init__(self, docs, field=None, value=None, limit=None):
        self._docs, self._field, self._value, self._limit = docs, field, value, limit

    def where(self, filter=None):
        return FakeQuery(self._docs, filter.field_path, filter.value, self._limit)

    def limit(self, n):
        return FakeQuery(self._docs, self._field, self._value, n)

    def stream(self, transaction=None):
        hits = [d for d in self._docs if d.get(self._field) == self._value]
        return iter([FakeSnapshot(d) for d in (hits[: self._limit] if self._limit else hits)])


class FakeCollection:
    def __init__(self, name, store_dict, log):
        self._name, self._store, self._log = name, store_dict, log

    def where(self, filter=None):
        return FakeQuery(list(self._store.values())).where(filter=filter)

    def document(self, doc_id):
        return FakeDocRef(self._store, f"{self._name}/{doc_id}", self._log)


class FakeTransaction:
    """set만 흉내낸다. 커밋은 즉시 반영되고 락·재시도는 없다."""

    def __init__(self, log):
        self._log = log

    def set(self, ref, data):
        self._log.append(("set", ref.id, True))
        ref.set(data)


class FakeClient:
    def __init__(self, log):
        self.data = {"recipients": {}, "receipts": {}, "receipt_dedup_keys": {}}
        self._log = log

    def collection(self, name):
        raw = self.data.setdefault(name, {})
        # FakeDocRef가 "collection/docid"를 키로 쓰므로 접두사를 벗겨 저장한다.
        return FakeCollection(name, _PrefixedDict(name, raw), self._log)


class _PrefixedDict(dict):
    """`{name}/{id}` 키를 받아 `{id}`로 저장하는 얇은 뷰."""

    def __init__(self, name, backing):
        super().__init__()
        self._name, self._backing = name, backing

    def _strip(self, key):
        return key.split("/", 1)[1] if key.startswith(f"{self._name}/") else key

    def get(self, key, default=None):
        return self._backing.get(self._strip(key), default)

    def __setitem__(self, key, value):
        self._backing[self._strip(key)] = value

    def values(self):
        return self._backing.values()


@pytest.fixture
def fake(monkeypatch):
    log = []
    client = FakeClient(log)
    monkeypatch.setattr(store, "get_client", lambda: client)
    # 트랜잭션 래퍼만 갈아끼운다 — 실제 Firestore 없이 콜백 본문을 돌린다.
    monkeypatch.setattr(store, "_run_in_transaction", lambda fn: fn(FakeTransaction(log)))
    client.log = log
    return client


def _create(**overrides):
    kwargs = {
        "recipient_id": "rcp_1",
        "slack_file_id": "F01ABCDEF",
        "slack_channel_id": "C01ABCDEF",
        "slack_message_ts": "1755500000.000100",
    }
    kwargs.update(overrides)
    return store.create_receipt_if_absent(**kwargs)


def test_finds_recipient_by_slack_user(fake):
    fake.data["recipients"]["rcp_1"] = {
        "recipient_id": "rcp_1",
        "slack_user_id": "U01ABCDEF",
    }
    assert store.find_recipient_by_slack_user("U01ABCDEF")["recipient_id"] == "rcp_1"


def test_unknown_slack_user_returns_none(fake):
    assert store.find_recipient_by_slack_user("U_NOBODY") is None


def test_create_recipient_from_slack_writes_unverified_recipient(fake, monkeypatch):
    """셀프 등록은 verified=False로 시작한다 — 관리자 확인 전까지는 사칭·오타
    위험을 감수한다는 뜻이다. 비밀번호 필드는 어디에도 없다."""
    monkeypatch.setattr(store, "record_audit_log", lambda **kw: None)

    doc = store.create_recipient_from_slack(
        slack_user_id="U_NEW", paypal_email="new-user@example.com"
    )

    assert doc["slack_user_id"] == "U_NEW"
    assert doc["paypal_email"] == "new-user@example.com"
    assert doc["verified"] is False
    assert doc["status"] == "ACTIVE"
    assert "password" not in doc
    assert store.find_recipient_by_slack_user("U_NEW")["paypal_email"] == "new-user@example.com"


def test_create_recipient_from_slack_writes_audit_log(fake, monkeypatch):
    audit_calls = []
    monkeypatch.setattr(store, "record_audit_log", lambda **kw: audit_calls.append(kw))

    store.create_recipient_from_slack(slack_user_id="U_NEW", paypal_email="new-user@example.com")

    assert audit_calls == [
        {
            "actor": "api/src/ingest",
            "action": "RECIPIENT_SELF_REGISTERED",
            "after": {"recipient_id": audit_calls[0]["after"]["recipient_id"], "slack_user_id": "U_NEW"},
        }
    ]


def test_creates_receipt_in_received_status(fake):
    receipt_id, created = _create()
    assert created is True
    assert receipt_id.startswith("rct_")
    doc = fake.data["receipts"][receipt_id]
    assert doc["status"] == "RECEIVED"
    assert doc["slack_file_id"] == "F01ABCDEF"
    # 파싱 파이프라인 몫은 자리조차 만들지 않는다 — 만들어 두면 "채워졌는지"를
    # 구분할 수 없어진다.
    assert "image_gcs_uri" not in doc
    assert "parse_signals" not in doc


def test_writes_dedup_key_document(fake):
    """유일성은 receipt_dedup_keys의 문서 ID가 강제한다."""
    receipt_id, _ = _create()
    dedup = fake.data["receipt_dedup_keys"]["F01ABCDEF"]
    assert dedup["receipt_id"] == receipt_id
    assert dedup["slack_file_id"] == "F01ABCDEF"


def test_slack_retry_does_not_create_second_receipt(fake):
    """Slack은 ack가 3초를 넘기면 같은 이벤트를 최대 3회 재전송한다."""
    first_id, first_created = _create()
    second_id, second_created = _create()
    assert first_created is True
    assert second_created is False
    assert second_id == first_id
    assert len(fake.data["receipts"]) == 1
    assert len(fake.data["receipt_dedup_keys"]) == 1


def test_retry_returns_receipt_id_without_querying_receipts(fake):
    """재전송 경로는 dedup 문서만 읽는다 — receipts.slack_file_id 인덱스에
    의존하지 않는다."""
    _create()
    fake.log.clear()
    _create()
    reads = [entry for entry in fake.log if entry[0] == "get"]
    assert reads == [("get", "receipt_dedup_keys/F01ABCDEF", True)]
    assert not [entry for entry in fake.log if entry[0] == "set"]


def test_different_files_in_same_message_create_two_receipts(fake):
    """한 메시지에 이미지 2장이면 message_ts가 같다. file_id로만 갈린다 —
    이게 channel_id + message_ts를 dedup 키로 못 쓰는 이유다."""
    a, _ = _create(slack_file_id="F_AAA")
    b, _ = _create(slack_file_id="F_BBB")
    assert a != b
    assert len(fake.data["receipts"]) == 2
    assert len(fake.data["receipt_dedup_keys"]) == 2


def test_dedup_key_is_read_before_any_write(fake):
    """구조 테스트 — 쿼리 기반 dedup으로 되돌아가는 회귀를 잡는다.

    유일성이 문서 ID로 강제되는지, 그리고 Firestore 제약(모든 읽기가 모든 쓰기보다
    앞)을 지키는지 본다. fake는 원자성 자체를 검증할 수 없으므로 이 순서 단언이
    계약 준수의 유일한 자동 증거다.
    """
    _create()
    kinds = [entry[0] for entry in fake.log]
    assert kinds[0] == "get", f"첫 연산이 읽기가 아니다: {fake.log}"
    assert "get" not in kinds[kinds.index("set") :], f"쓰기 뒤에 읽기가 있다: {fake.log}"

    first_read = fake.log[0]
    assert first_read[1] == "receipt_dedup_keys/F01ABCDEF", (
        f"결정론적 dedup 경로를 읽지 않았다: {first_read}"
    )
    assert first_read[2] is True, "dedup 읽기가 트랜잭션 밖에서 일어났다"


def test_created_receipt_validates_against_contract(fake):
    """schema-contract.md §2 — 인입이 쓴 문서가 Receipt로 검증돼야 한다."""
    from src.schemas.models import Receipt

    receipt_id, _ = _create()
    Receipt.model_validate(fake.data["receipts"][receipt_id])


def test_transaction_failure_raises_domain_error(fake, monkeypatch):
    """SDK가 재시도를 소진하면 ValueError를 던진다. 라우트가 SDK 내부 사정을
    알지 않도록 도메인 예외로 바꿔 올린다."""

    def boom(fn):
        raise ValueError("Failed to commit transaction in 5 attempts.")

    monkeypatch.setattr(store, "_run_in_transaction", boom)
    with pytest.raises(store.ReceiptStoreUnavailable):
        _create()
