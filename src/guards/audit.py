"""money-safety.md — 모든 게이트 결정을 audit_logs에 남긴다. append-only,
schema-contract.md §2 필드 구성을 그대로 따른다.
"""

from datetime import UTC, datetime

from ..payouts.store import get_client


def record_audit_log(
    *,
    actor: str,
    action: str,
    org_id: str | None = None,
    run_id: str | None = None,
    actor_type: str = "SYSTEM",
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
) -> None:
    get_client().collection("audit_logs").add(
        {
            "ts": datetime.now(UTC),
            "org_id": org_id,
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
    docs = get_client().collection("audit_logs").order_by("ts").stream()
    return [d.to_dict() for d in docs]
