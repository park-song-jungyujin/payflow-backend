"""schema-contract.md §7 / money-safety.md — 승인 토큰 소각과 CAS 전이.

절대 규칙 2 (docs/CLAUDE.md): 승인 토큰 없이 송금 엔드포인트는 실행되지 않는다.
이 스위트가 지키는 것: 토큰 누락/위조/재사용/만료를 전부 막고, 통과했을 때만
APPROVED → EXECUTING 전이가 트랜잭션 안에서 일어난다.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from src.guards import tokens
from src.guards.errors import GuardRejection

RAW_TOKEN = "raw-approval-token"
TOKEN_HASH = hashlib.sha256(RAW_TOKEN.encode("utf-8")).hexdigest()


class FakeSnapshot:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data) if self._data else None


class FakeRunRef:
    def __init__(self, data):
        self._data = data

    def get(self, transaction=None):
        return FakeSnapshot(self._data)


class FakeCollection:
    def __init__(self, data):
        self._data = data

    def document(self, doc_id):
        return FakeRunRef(self._data)


class FakeTransaction:
    def update(self, ref, data):
        pass  # verify_and_burn_token은 반환된 updates dict만 보고, 재조회하지 않는다


class FakeClient:
    def __init__(self, run_doc):
        self._run_doc = run_doc

    def collection(self, name):
        assert name == "settlement_runs"
        return FakeCollection(self._run_doc)

    def transaction(self):
        return FakeTransaction()


def _base_run(**overrides):
    now = datetime.now(UTC)
    run = {
        "settlement_run_id": "run_1",
        "status": "APPROVED",
        "approval_token_hash": TOKEN_HASH,
        "approval_token_used_at": None,
        "approval_token_expires_at": now + timedelta(minutes=10),
        "approval_amount_hash": "matching-hash",
        "fx_rates": {},
    }
    run.update(overrides)
    return run


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    """firestore.transactional을 항등 데코레이터로 바꿔 실제 Firestore 없이
    트랜잭션 콜백 본문을 그대로 돌린다 — tests/ingest/test_store.py와 같은 전략."""
    monkeypatch.setattr(tokens.firestore, "transactional", lambda fn: fn)
    monkeypatch.setattr(tokens, "approval_amount_hash", lambda run: "matching-hash")
    monkeypatch.setattr(tokens, "check_caps", lambda run: None)


def _run_with_client(monkeypatch, run_doc):
    client = FakeClient(run_doc)
    monkeypatch.setattr(tokens, "get_client", lambda: client)
    return client


def test_missing_token_rejected(monkeypatch):
    run = _base_run()
    _run_with_client(monkeypatch, run)
    with pytest.raises(GuardRejection) as exc:
        tokens.verify_and_burn_token("run_1", run, None)
    assert exc.value.status_code == 403
    assert "missing" in exc.value.detail


def test_wrong_status_rejected(monkeypatch):
    run = _base_run(status="DRAFT")
    _run_with_client(monkeypatch, run)
    with pytest.raises(GuardRejection) as exc:
        tokens.verify_and_burn_token("run_1", run, RAW_TOKEN)
    assert exc.value.status_code == 409


def test_already_used_token_rejected(monkeypatch):
    run = _base_run(approval_token_used_at=datetime.now(UTC))
    _run_with_client(monkeypatch, run)
    with pytest.raises(GuardRejection) as exc:
        tokens.verify_and_burn_token("run_1", run, RAW_TOKEN)
    assert "already used" in exc.value.detail


def test_expired_token_rejected(monkeypatch):
    run = _base_run(approval_token_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    _run_with_client(monkeypatch, run)
    with pytest.raises(GuardRejection) as exc:
        tokens.verify_and_burn_token("run_1", run, RAW_TOKEN)
    assert "expired" in exc.value.detail


def test_wrong_token_value_rejected(monkeypatch):
    run = _base_run()
    _run_with_client(monkeypatch, run)
    with pytest.raises(GuardRejection) as exc:
        tokens.verify_and_burn_token("run_1", run, "not-the-real-token")
    assert "invalid approval token" in exc.value.detail


def test_amount_changed_after_approval_rejected(monkeypatch):
    run = _base_run()
    _run_with_client(monkeypatch, run)
    monkeypatch.setattr(tokens, "approval_amount_hash", lambda r: "different-hash")
    with pytest.raises(GuardRejection) as exc:
        tokens.verify_and_burn_token("run_1", run, RAW_TOKEN)
    assert "amount_hash mismatch" in exc.value.detail


def test_cap_violation_rejected(monkeypatch):
    run = _base_run()
    _run_with_client(monkeypatch, run)
    monkeypatch.setattr(tokens, "check_caps", lambda r: "MAX_AMOUNT_PER_BATCH_MINOR exceeded")
    with pytest.raises(GuardRejection) as exc:
        tokens.verify_and_burn_token("run_1", run, RAW_TOKEN)
    assert exc.value.status_code == 403
    assert "exceeded" in exc.value.detail


def test_valid_token_transitions_to_executing_and_burns_token(monkeypatch):
    run = _base_run()
    _run_with_client(monkeypatch, run)
    updates = tokens.verify_and_burn_token("run_1", run, RAW_TOKEN)
    assert updates["status"] == "EXECUTING"
    assert updates["approval_token_used_at"] is not None


def test_reused_token_rejected_even_if_caller_passed_stale_run_dict(monkeypatch):
    """CAS의 핵심 — 트랜잭션 안에서 최신 스냅샷을 다시 읽어 재확인한다. 호출자가
    넘긴 run dict가 이미 소각된 걸 놓쳐도(경합 상황 흉내) 트랜잭션이 잡아야 한다."""
    stale_run = _base_run()  # 호출자 관점: 아직 안 쓰였다고 믿음
    current_in_store = _base_run(approval_token_used_at=datetime.now(UTC))  # 실제로는 이미 씀
    _run_with_client(monkeypatch, current_in_store)
    with pytest.raises(GuardRejection) as exc:
        tokens.verify_and_burn_token("run_1", stale_run, RAW_TOKEN)
    assert "already used" in exc.value.detail
