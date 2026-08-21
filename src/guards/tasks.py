"""Cloud Tasks enqueue 지점 — schema-contract.md §8/§10 실행 경로.

`CLOUD_TASKS_QUEUE`가 비어 있으면 명시적으로 실패한다 — 호출부가 성공한 척하며
조용히 아무 일도 안 하는 걸 막기 위해서다. 큐가 구성돼 있으면 api 자신의
라우트를 OIDC 토큰(`TASKS_SERVICE_ACCOUNT_EMAIL`)으로 호출하는 태스크를 만든다 —
인증 방식은 `guards/oidc.py`가 검증하는 것과 동일하다.
"""

import json
import os
from datetime import UTC, datetime, timedelta

from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2


class QueueNotConfigured(RuntimeError):
    pass


def enqueue_task(path: str, payload: dict, delay_seconds: int = 0, *, audience: str | None = None) -> None:
    """`audience`가 None이면 기본값인 api 자신의 `OIDC_AUDIENCE`를 쓴다 — 이 서비스
    안의 라우트(payouts 등)를 태우는 태스크는 그걸로 충분하다. 하지만 태스크가
    쳐야 할 대상이 별도 Cloud Run 서비스(예: payflow-agent)일 때는 그 서비스
    자신의 주소를 `audience`로 넘겨야 한다 — api 자신의 audience로는 그 서비스에
    도달할 수 없고, OIDC 토큰도 대상 서비스가 검증할 수 있는 audience여야 하기
    때문이다. URL과 OIDC 토큰의 audience는 항상 같은 값을 쓴다.
    """
    queue = os.environ.get("CLOUD_TASKS_QUEUE")
    if not queue:
        raise QueueNotConfigured(
            "CLOUD_TASKS_QUEUE not configured — Cloud Tasks 인프라 미완성. "
            f"POST {path}을 직접 호출해 시뮬레이션한다."
        )

    project = os.environ["GCP_PROJECT"]
    location = os.environ["CLOUD_TASKS_LOCATION"]
    if audience is None:
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
    if delay_seconds > 0:
        schedule_time = timestamp_pb2.Timestamp()
        schedule_time.FromDatetime(datetime.now(UTC) + timedelta(seconds=delay_seconds))
        task["schedule_time"] = schedule_time
    client.create_task(parent=parent, task=task)
