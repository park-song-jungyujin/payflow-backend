"""schema-contract.md §9 — 청구자 에이전트 review enqueue 경로와 본문 계약."""

import json

import pytest

from src.guards import tasks as guards_tasks
from src.parsing.enqueue import QueueNotConfigured, enqueue_claimant_review


_SNAPSHOT_FIELDS = (
    "merchant_name",
    "transaction_date",
    "parsed_amount_minor",
    "currency",
    "account_category_code",
    "parse_confidence",
    "raw_text_gcs_uri",
)

_FULL_RECEIPT_SNAPSHOT = {
    "merchant_name": "딤섬관",
    "transaction_date": "2026-08-21",
    "parsed_amount_minor": 15000,
    "currency": "KRW",
    "account_category_code": "MEALS",
    "parse_confidence": 0.92,
    "raw_text_gcs_uri": "gs://payflow-receipts/raw_text/rct_01K3M9XQ7B2F4G6H8J0K2M4N6P.txt",
    # 나가면 안 되는 필드들 — 판단에 불필요한 식별자·상태다(§6).
    # raw_text(원문 자체)도 여기 있다 — URI만 나가고 원문은 태스크 본문에 실리지 않는다.
    "raw_text": "딤섬관 영수증 원문 010-1234-5678",
    "recipient_id": "usr_should_not_leak",
    "status": "PARSED",
    "slack_channel_id": "C0123",
    "gcs_uri": "gs://should-not-leak",
}


def test_missing_queue_raises_explicitly(monkeypatch):
    """큐가 없으면 성공한 척하지 않는다 — 청구자 에이전트 호출이 조용히 사라지는 걸 막는다."""
    monkeypatch.delenv("CLOUD_TASKS_QUEUE", raising=False)
    with pytest.raises(QueueNotConfigured):
        enqueue_claimant_review("rct_01K3M9XQ7B2F4G6H8J0K2M4N6P", receipt=_FULL_RECEIPT_SNAPSHOT)


def test_builds_oidc_task_for_claimant_review_route(monkeypatch):
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.setenv("TASKS_SERVICE_ACCOUNT_EMAIL", "tasks@payflow-test.iam.gserviceaccount.com")
    monkeypatch.setenv("AGENT_SERVICE_URL", "https://payflow-agent.test.invalid")
    captured = {}

    class FakeClient:
        def queue_path(self, project, location, queue):
            return f"projects/{project}/locations/{location}/queues/{queue}"

        def create_task(self, parent, task):
            captured["parent"] = parent
            captured["task"] = task

    monkeypatch.setattr(guards_tasks.tasks_v2, "CloudTasksClient", FakeClient)
    enqueue_claimant_review("rct_01K3M9XQ7B2F4G6H8J0K2M4N6P", receipt=_FULL_RECEIPT_SNAPSHOT)

    request = captured["task"]["http_request"]
    # payflow-agent는 별도 Cloud Run 서비스라 api 자신의 OIDC_AUDIENCE로는
    # 도달할 수 없다 — URL과 OIDC 토큰 audience 둘 다 AGENT_SERVICE_URL이어야 한다.
    assert request["url"] == "https://payflow-agent.test.invalid/agents/claimant/review"
    body = json.loads(request["body"])
    assert body == {
        "receipt_id": "rct_01K3M9XQ7B2F4G6H8J0K2M4N6P",
        "task_id": "CLAIMANT:rct_01K3M9XQ7B2F4G6H8J0K2M4N6P",
        "merchant_name": "딤섬관",
        "transaction_date": "2026-08-21",
        "parsed_amount_minor": 15000,
        "currency": "KRW",
        "account_category_code": "MEALS",
        "parse_confidence": 0.92,
        "raw_text_gcs_uri": "gs://payflow-receipts/raw_text/rct_01K3M9XQ7B2F4G6H8J0K2M4N6P.txt",
    }
    assert request["oidc_token"]["audience"] == "https://payflow-agent.test.invalid"
    assert captured["parent"].endswith("/queues/payflow-queue")


def test_snapshot_fields_are_exactly_seven_and_exclude_recipient_id(monkeypatch):
    """§6 최소화 — 판단에 불필요한 식별자(recipient_id)·status·slack_*·gcs_*는
    나가지 않는다. 나가는 스냅샷 필드는 정확히 7개뿐이다.

    원문(raw_text)도 나가지 않는다 — 태스크 본문은 큐에 영속화되고 로그에 남는데,
    §2가 금지하는 건 "마스킹 안 된 원문이 Firestore·감사 로그로 흘러가는 것"이다.
    에이전트는 raw_text_gcs_uri로 GCS에서 직접 읽는다(§9 입력 계약)."""
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.setenv("TASKS_SERVICE_ACCOUNT_EMAIL", "tasks@payflow-test.iam.gserviceaccount.com")
    monkeypatch.setenv("AGENT_SERVICE_URL", "https://payflow-agent.test.invalid")
    captured = {}

    class FakeClient:
        def queue_path(self, project, location, queue):
            return f"projects/{project}/locations/{location}/queues/{queue}"

        def create_task(self, parent, task):
            captured["task"] = task

    monkeypatch.setattr(guards_tasks.tasks_v2, "CloudTasksClient", FakeClient)
    enqueue_claimant_review("rct_01K3M9XQ7B2F4G6H8J0K2M4N6P", receipt=_FULL_RECEIPT_SNAPSHOT)

    body = json.loads(captured["task"]["http_request"]["body"])
    snapshot_keys = set(body.keys()) - {"receipt_id", "task_id"}
    assert snapshot_keys == set(_SNAPSHOT_FIELDS)
    assert "raw_text" not in body
    assert "recipient_id" not in body
    assert "status" not in body
    assert "slack_channel_id" not in body
    assert "gcs_uri" not in body
    # 본문 전체를 훑는다 — 키 이름이 바뀌어도 원문이 실려 나가면 잡힌다.
    assert "010-1234-5678" not in json.dumps(body, ensure_ascii=False)


def test_missing_snapshot_fields_become_none_not_absent(monkeypatch):
    """필드가 없으면 키를 빼지 말고 None으로 넣는다 — 에이전트가 "없다"와
    "안 왔다"를 구분해야 한다."""
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.setenv("TASKS_SERVICE_ACCOUNT_EMAIL", "tasks@payflow-test.iam.gserviceaccount.com")
    monkeypatch.setenv("AGENT_SERVICE_URL", "https://payflow-agent.test.invalid")
    captured = {}

    class FakeClient:
        def queue_path(self, project, location, queue):
            return f"projects/{project}/locations/{location}/queues/{queue}"

        def create_task(self, parent, task):
            captured["task"] = task

    monkeypatch.setattr(guards_tasks.tasks_v2, "CloudTasksClient", FakeClient)
    enqueue_claimant_review("rct_01K3M9XQ7B2F4G6H8J0K2M4N6P", receipt={})

    body = json.loads(captured["task"]["http_request"]["body"])
    for field in _SNAPSHOT_FIELDS:
        assert field in body
        assert body[field] is None


def test_missing_agent_service_url_raises_queue_not_configured(monkeypatch):
    """AGENT_SERVICE_URL이 없으면 os.environ[...] KeyError로 새 나가지 않고
    QueueNotConfigured로 명시돼야 파이프라인이 PARSED를 유지하는 경로를 탄다."""
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "payflow-queue")
    monkeypatch.delenv("AGENT_SERVICE_URL", raising=False)
    with pytest.raises(QueueNotConfigured):
        enqueue_claimant_review("rct_01K3M9XQ7B2F4G6H8J0K2M4N6P", receipt=_FULL_RECEIPT_SNAPSHOT)
