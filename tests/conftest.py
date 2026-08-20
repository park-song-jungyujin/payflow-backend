"""테스트 전역 환경변수 스텁. 실제 GCP·Slack에 붙지 않는다."""

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("GCP_PROJECT", "payflow-test")
    monkeypatch.setenv("FIRESTORE_DATABASE", "development")
    monkeypatch.setenv("CLOUD_TASKS_LOCATION", "asia-northeast3")
    monkeypatch.setenv("OIDC_AUDIENCE", "https://api.test.invalid")
    monkeypatch.delenv("CLOUD_TASKS_QUEUE", raising=False)
    monkeypatch.delenv("GCS_RECEIPTS_BUCKET", raising=False)  # 테스트는 실제 GCS에 붙지 않는다
    monkeypatch.delenv("GEMINI_MODEL_ID", raising=False)  # 개발자 .env에 값이 있어도 테스트는 fixture 파서를 써야 한다
    monkeypatch.delenv("VERTEX_LOCATION", raising=False)  # 같은 이유 — 실 SDK 클라이언트가 만들어지면 안 된다
    yield


def pytest_addoption(parser):
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="OpenAPI 스냅샷을 현재 스키마로 덮어쓴다 (schema: 커밋과 함께 쓴다)",
    )
