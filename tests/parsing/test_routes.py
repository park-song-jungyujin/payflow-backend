"""schema-contract.md §10 — POST /tasks/parse-receipt (Cloud Tasks 전용, OIDC 필수).

payouts/routes.py의 /tasks/* 와 같은 형태다. 공개 금지 라우트라 인증이 첫 관문이고,
그게 이 스위트의 첫 테스트다.
"""

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.parsing import routes
from src.parsing.slack_files import TransientParseError
from src.parsing.store import ReceiptNotFound


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(routes, "verify_oidc", lambda authorization: {"sub": "tasks-sa"})
    return TestClient(app)


def test_rejects_request_without_oidc_token():
    """verify_oidc를 스텁하지 않은 채로 부른다 — 공개 노출 회귀를 잡는다."""
    response = TestClient(app).post("/tasks/parse-receipt", json={"receipt_id": "rct_1"})
    assert response.status_code == 401


def test_parses_and_returns_status(client, monkeypatch):
    monkeypatch.setattr(routes, "parse_receipt", lambda receipt_id: "PARSED")
    response = client.post(
        "/tasks/parse-receipt", json={"receipt_id": "rct_1"}, headers={"Authorization": "Bearer t"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "PARSED", "receipt_id": "rct_1"}


def test_passes_receipt_id_through(client, monkeypatch):
    seen = []
    monkeypatch.setattr(routes, "parse_receipt", lambda receipt_id: seen.append(receipt_id) or "PARSED")
    client.post(
        "/tasks/parse-receipt", json={"receipt_id": "rct_abc"}, headers={"Authorization": "Bearer t"}
    )
    assert seen == ["rct_abc"]


def test_missing_receipt_id_is_400(client):
    response = client.post("/tasks/parse-receipt", json={}, headers={"Authorization": "Bearer t"})
    assert response.status_code == 400


def test_unknown_receipt_is_404(client, monkeypatch):
    def boom(receipt_id):
        raise ReceiptNotFound(receipt_id)

    monkeypatch.setattr(routes, "parse_receipt", boom)
    response = client.post(
        "/tasks/parse-receipt", json={"receipt_id": "rct_nope"}, headers={"Authorization": "Bearer t"}
    )
    assert response.status_code == 404


def test_transient_failure_is_503_so_cloud_tasks_retries(client, monkeypatch):
    """200을 돌려주면 큐가 태스크를 지우고 영수증이 RECEIVED로 영원히 남는다."""
    def boom(receipt_id):
        raise TransientParseError("vertex 503")

    monkeypatch.setattr(routes, "parse_receipt", boom)
    response = client.post(
        "/tasks/parse-receipt", json={"receipt_id": "rct_1"}, headers={"Authorization": "Bearer t"}
    )
    assert response.status_code == 503


def test_permanent_failure_is_200_so_cloud_tasks_stops(client, monkeypatch):
    """FAILED는 확정된 결말이다. 5xx를 던지면 큐가 같은 영수증을 계속 재시도한다."""
    monkeypatch.setattr(routes, "parse_receipt", lambda receipt_id: "FAILED")
    response = client.post(
        "/tasks/parse-receipt", json={"receipt_id": "rct_1"}, headers={"Authorization": "Bearer t"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
