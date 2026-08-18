"""Cloud Tasks enqueue 지점 — schema-contract.md §8/§10 실행 경로.

`CLOUD_TASKS_QUEUE`가 비어 있으면 명시적으로 실패한다 — 호출부가 성공한 척하며
조용히 아무 일도 안 하는 걸 막기 위해서다. 큐가 구성돼 있으면 api 자신의
라우트를 OIDC 토큰(`TASKS_SERVICE_ACCOUNT_EMAIL`)으로 호출하는 태스크를 만든다 —
인증 방식은 `guards/oidc.py`가 검증하는 것과 동일하다.
"""

import json
import os

from google.cloud import tasks_v2


class QueueNotConfigured(RuntimeError):
    pass


def enqueue_task(path: str, payload: dict) -> None:
    queue = os.environ.get("CLOUD_TASKS_QUEUE")
    if not queue:
        raise QueueNotConfigured(
            "CLOUD_TASKS_QUEUE not configured — Cloud Tasks 인프라 미완성. "
            f"POST {path}을 직접 호출해 시뮬레이션한다."
        )

    project = os.environ["GCP_PROJECT"]
    location = os.environ["CLOUD_TASKS_LOCATION"]
    audience = os.environ["OIDC_AUDIENCE"]
    service_account_email = os.environ["TASKS_SERVICE_ACCOUNT_EMAIL"]

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(project, location, queue)
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{audience}{path}",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode("utf-8"),
            "oidc_token": {
                "service_account_email": service_account_email,
                "audience": audience,
            },
        }
    }
    client.create_task(parent=parent, task=task)
