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
    yield


def pytest_addoption(parser):
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="OpenAPI 스냅샷을 현재 스키마로 덮어쓴다 (schema: 커밋과 함께 쓴다)",
    )
