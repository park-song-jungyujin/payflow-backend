"""schema-contract.md §2 Firestore 컬렉션 9종 + §6 SettlementFilter.

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
    ReceiptStatus,
    RecipientStatus,
    SenderItemStatus,
    SettlementRunStatus,
)


class Recipient(BaseModel):
    recipient_id: str
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


class Receipt(BaseModel):
    receipt_id: str
    recipient_id: str
    image_gcs_uri: str
    raw_text_gcs_uri: str
    merchant_name: str | None = None
    transaction_date: date | None = None
    parsed_amount_minor: int | None = None
    currency: str
    account_category_code: AccountCategory | None = None
    category_source: CategorySource
    parse_signals: ParseSignals
    llm_confidence: float | None = None
    status: ReceiptStatus
    created_at: datetime
    updated_at: datetime


class ClaimRequest(BaseModel):
    claim_request_id: str
    recipient_id: str
    receipt_id: str
    slack_dm_ts: str
    reminded_at: datetime | None = None
    expires_at: datetime
    status: ClaimRequestStatus
    created_at: datetime
    updated_at: datetime


class Claim(BaseModel):
    claim_id: str
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
    agent: AgentName
    target_type: AgentDraftTargetType
    target_id: str
    task_id: str
    payload: dict
    created_at: datetime


class AuditLog(BaseModel):
    ts: datetime
    actor: str
    actor_type: ActorType
    action: str
    run_id: str | None = None
    before: dict | None = None
    after: dict | None = None
    reason: str | None = None


__all__ = [
    "Money",
    "Recipient",
    "ParseSignals",
    "Receipt",
    "ClaimRequest",
    "Claim",
    "SettlementFilter",
    "SettlementRun",
    "SenderItem",
    "AgentDraft",
    "AuditLog",
]
