"""schema-contract.md §2 — orgs · executors · slack_workspaces · sessions (C 소유).

`payouts/store.py`의 `get_client`를 재사용한다 — Firestore 클라이언트를 두 번
만들 이유가 없다.
"""

from datetime import UTC, datetime

from google.cloud.firestore_v1.base_query import FieldFilter
from ulid import ULID

from ..payouts.store import get_client


def get_or_create_default_org_id() -> str:
    """ingest/routes.py — Slack 워크스페이스가 아직 /auth/slack/install로 등록되기
    전에 영수증이 먼저 들어올 때 쓰는 폴백 기관. 대시보드 로그인 게이트와는
    무관하다(그쪽은 세션에서 org_id를 받는다) — Firestore에 org가 하나도 없으면
    새로 만들고, 있으면 그대로 쓴다."""
    docs = get_client().collection("orgs").limit(1).stream()
    existing = next(iter(docs), None)
    if existing is not None:
        return existing.id

    org_id = f"org_{ULID()}"
    now = datetime.now(UTC)
    create_org(
        org_id,
        {
            "org_id": org_id,
            "name": "Default Org",
            "created_by": "system",
            "created_at": now,
            "updated_at": now,
        },
    )
    return org_id


def get_executor_by_google_sub(google_sub: str) -> dict | None:
    docs = (
        get_client()
        .collection("executors")
        .where(filter=FieldFilter("google_sub", "==", google_sub))
        .limit(1)
        .stream()
    )
    doc = next(iter(docs), None)
    return doc.to_dict() if doc else None


def create_executor(executor_id: str, doc: dict) -> None:
    get_client().collection("executors").document(executor_id).set(doc)


def create_org(org_id: str, doc: dict) -> None:
    get_client().collection("orgs").document(org_id).set(doc)


def create_session(session_token_hash: str, doc: dict) -> None:
    get_client().collection("sessions").document(session_token_hash).set(doc)


def get_session(session_token_hash: str) -> dict | None:
    doc = get_client().collection("sessions").document(session_token_hash).get()
    return doc.to_dict() if doc.exists else None


def delete_session(session_token_hash: str) -> None:
    get_client().collection("sessions").document(session_token_hash).delete()


def get_slack_workspace_by_org(org_id: str) -> dict | None:
    """`slack_files.py`가 영수증 다운로드용 bot token을 찾을 때 쓴다.
    문서 ID는 `team_id`라 org_id로는 조회가 아니라 쿼리다 — 단일 동등 필터라
    복합 색인이 필요 없다. 기관당 워크스페이스는 하나(1:1)라 `limit(1)`."""
    docs = (
        get_client()
        .collection("slack_workspaces")
        .where(filter=FieldFilter("org_id", "==", org_id))
        .limit(1)
        .stream()
    )
    doc = next(iter(docs), None)
    return doc.to_dict() if doc else None


def get_slack_workspace_by_team(team_id: str) -> dict | None:
    doc = get_client().collection("slack_workspaces").document(team_id).get()
    return doc.to_dict() if doc.exists else None


def create_slack_workspace(team_id: str, doc: dict) -> None:
    get_client().collection("slack_workspaces").document(team_id).set(doc)
