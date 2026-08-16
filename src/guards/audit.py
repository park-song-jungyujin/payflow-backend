"""money-safety.md — 모든 게이트 결정을 audit_logs에 남긴다.

Firestore가 아직 안 붙어 있어 인메모리 리스트에 append만 한다. 필드 구성은
schema-contract.md §2 `audit_logs`와 동일하게 맞춰, Firestore 연동 시 그대로
옮겨 쓸 수 있게 한다.
"""

from datetime import UTC, datetime

_LOG: list[dict] = []


def record_audit_log(
    *,
    actor: str,
    action: str,
    run_id: str | None = None,
    actor_type: str = "SYSTEM",
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
) -> None:
    _LOG.append(
        {
            "ts": datetime.now(UTC).isoformat(),
            "actor": actor,
            "actor_type": actor_type,
            "action": action,
            "run_id": run_id,
            "before": before,
            "after": after,
            "reason": reason,
        }
    )


def get_audit_log() -> list[dict]:
    return list(_LOG)
