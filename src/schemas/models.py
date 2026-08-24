"""schema-contract.md §2 Firestore 컬렉션 12종 + §6 SettlementFilter.

(`receipt_dedup_keys`·`agent_sessions`은 모델링하지 않는다 — 기존 관례.)

필드명·타입·상태 enum은 이 문서가 단일 소스다. 여기 있는 것과 다르게 구현하면
통합 시점에 조용히 실패한다. 변경이 필요하면 schema-contract.md를 먼저 고친다.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from .common import Money
from .enums import (
    AccountCategory,
    ActorType,
    AgentDraftTargetType,
    AgentName,
    CategorySource,
    ClaimRequestStatus,
    ClaimStatus,
    ExecutorStatus,
    ReceiptStatus,
    RecipientStatus,
    ReminderReason,
    SenderItemStatus,
    SettlementRunStatus,
)


class Recipient(BaseModel):
    recipient_id: str
    org_id: str
    slack_user_id: str
    paypal_email: str
    display_name: str
    monthly_paid_minor: int
    monthly_period: str
    verified: bool
    status: RecipientStatus
    created_at: datetime
    updated_at: datetime


class ParseSignals(BaseModel):
    merchant_name_present: bool
    transaction_date_present: bool
    amount_parsed: bool
    currency_detected: bool
    injection_suspected: bool


class VerificationSignals(BaseModel):
    """이미지 ↔ 파싱 결과 검증 판정. 판정만 담는다 — 대체 금액/텍스트는 없다."""

    model_config = ConfigDict(extra="forbid")

    image_legible: bool
    amount_matches: bool
    merchant_matches: bool
    date_matches: bool
    injection_suspected: bool


class Receipt(BaseModel):
    receipt_id: str
    org_id: str
    recipient_id: str
    # image_gcs_uri · raw_text_gcs_uri · currency · category_source · parse_signals는
    # 전부 파싱 파이프라인이 채운다. RECEIVED(Slack 인입 완료, 파싱 전) 상태에서는
    # 없다 — 필수로 두면 그 상태의 문서를 애초에 만들 수 없다.
    image_gcs_uri: str | None = None
    raw_text_gcs_uri: str | None = None
    # slack_file_id가 Slack 재전송 dedup 키다. slack_channel_id + slack_message_ts
    # 조합은 키가 못 된다 — 한 메시지에 이미지를 여러 장 붙이면 ts가 같다.
    slack_file_id: str | None = None
    slack_channel_id: str | None = None
    slack_message_ts: str | None = None
    merchant_name: str | None = None
    transaction_date: date | None = None
    parsed_amount_minor: int | None = None
    currency: str | None = None
    account_category_code: AccountCategory | None = None
    category_source: CategorySource | None = None
    parse_signals: ParseSignals | None = None
    llm_confidence: float | None = None
    verified_at: datetime | None = None
    verification_signals: VerificationSignals | None = None
    status: ReceiptStatus
    created_at: datetime
    updated_at: datetime


class ClaimRequest(BaseModel):
    claim_request_id: str
    org_id: str
    recipient_id: str
    # MISSING_CLAIM · UNPAID_NOTICE는 특정 영수증에서 출발하지 않는다.
    receipt_id: str | None = None
    reason: ReminderReason
    # 값은 chat.postMessage 응답에서 나온다. 문서를 먼저 만들어 멱등키를 확보하고
    # DM을 보낸 뒤 채운다 — 필수면 재시도 때 같은 DM이 두 번 나간다.
    slack_dm_ts: str | None = None
    reminded_at: datetime | None = None
    expires_at: datetime
    status: ClaimRequestStatus
    created_at: datetime
    updated_at: datetime


class Claim(BaseModel):
    claim_id: str
    org_id: str
    recipient_id: str
    receipt_id: str
    amount_minor: int
    currency: str
    account_category_code: AccountCategory
    is_business: bool
    settlement_run_id: str | None = None
    settled_at: datetime | None = None
    status: ClaimStatus
    created_at: datetime
    updated_at: datetime


class SettlementFilter(BaseModel):
    """집행자 에이전트가 자연어에서 만들 수 있는 유일한 객체."""

    model_config = ConfigDict(extra="forbid")

    period_start: date | None = None  # receipts.transaction_date 기준
    period_end: date | None = None
    recipient_ids: list[str] | None = None
    account_categories: list[AccountCategory] | None = None
    exclude_claim_ids: list[str] | None = None


class SettlementRun(BaseModel):
    settlement_run_id: str
    org_id: str
    filter: SettlementFilter
    base_currency: str
    total_amount_minor: int
    fx_rates: dict[str, str]
    fx_locked_at: datetime | None = None
    approval_amount_hash: str | None = None
    approval_token_hash: str | None = None
    approval_token_expires_at: datetime | None = None
    approval_token_used_at: datetime | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    retry_seq: int = 0
    status: SettlementRunStatus
    created_at: datetime
    updated_at: datetime


class SenderItem(BaseModel):
    sender_item_id: str
    org_id: str
    settlement_run_id: str
    recipient_id: str
    receiver_email: str
    amount_minor: int
    currency: str
    paypal_value: str
    payout_item_id: str | None = None
    paypal_transaction_status: str | None = None
    status: SenderItemStatus
    retry_of: str | None = None
    created_at: datetime
    updated_at: datetime


class AgentDraft(BaseModel):
    draft_id: str
    org_id: str
    agent: AgentName
    target_type: AgentDraftTargetType
    target_id: str
    task_id: str
    payload: dict
    created_at: datetime


class AuditLog(BaseModel):
    ts: datetime
    org_id: str | None = None
    actor: str
    actor_type: ActorType
    action: str
    run_id: str | None = None
    before: dict | None = None
    after: dict | None = None
    reason: str | None = None


class Org(BaseModel):
    org_id: str
    name: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class Executor(BaseModel):
    executor_id: str
    org_id: str
    email: str
    google_sub: str
    name: str
    status: ExecutorStatus
    created_at: datetime
    updated_at: datetime


class SlackWorkspace(BaseModel):
    team_id: str
    org_id: str
    bot_token: str
    bot_user_id: str
    scope: str
    installed_at: datetime
    installed_by: str
    updated_at: datetime


class Session(BaseModel):
    session_token_hash: str
    executor_id: str
    org_id: str
    email: str
    expires_at: datetime
    created_at: datetime


__all__ = [
    "Money",
    "Recipient",
    "ParseSignals",
    "VerificationSignals",
    "Receipt",
    "ClaimRequest",
    "Claim",
    "SettlementFilter",
    "SettlementRun",
    "SenderItem",
    "AgentDraft",
    "AuditLog",
    "Org",
    "Executor",
    "SlackWorkspace",
    "Session",
]
