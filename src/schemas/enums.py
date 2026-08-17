"""schema-contract.md §5 계정과목, §2 각 컬렉션 상태 enum."""

from enum import StrEnum


class AccountCategory(StrEnum):
    PAYMENT_FEE = "PAYMENT_FEE"
    EMPLOYEE_BENEFIT = "EMPLOYEE_BENEFIT"
    TRAVEL = "TRAVEL"
    SUPPLIES = "SUPPLIES"
    ADVERTISING = "ADVERTISING"
    RENT = "RENT"
    UNCLASSIFIED = "UNCLASSIFIED"


CATEGORY_DISPLAY: dict[AccountCategory, str] = {
    AccountCategory.PAYMENT_FEE: "지급수수료",
    AccountCategory.EMPLOYEE_BENEFIT: "복리후생비",
    AccountCategory.TRAVEL: "여비교통비",
    AccountCategory.SUPPLIES: "소모품비",
    AccountCategory.ADVERTISING: "광고선전비",
    AccountCategory.RENT: "지급임차료",
    AccountCategory.UNCLASSIFIED: "미분류",
}


class CategorySource(StrEnum):
    LLM_PARSE = "LLM_PARSE"
    DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"
    EXECUTOR_AGENT = "EXECUTOR_AGENT"
    HUMAN = "HUMAN"


class RecipientStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ReceiptStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PARSED = "PARSED"
    NEEDS_REQUERY = "NEEDS_REQUERY"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    FAILED = "FAILED"


class ReminderReason(StrEnum):
    PARSE_FAILED = "PARSE_FAILED"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    MISSING_CLAIM = "MISSING_CLAIM"
    UNPAID_NOTICE = "UNPAID_NOTICE"


class ClaimRequestStatus(StrEnum):
    PENDING = "PENDING"
    REMINDED = "REMINDED"
    RESPONDED = "RESPONDED"
    EXPIRED = "EXPIRED"


class ClaimStatus(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    IN_RUN = "IN_RUN"
    SETTLED = "SETTLED"
    VOID = "VOID"


class SettlementRunStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    SETTLED = "SETTLED"
    FAILED = "FAILED"


class SenderItemStatus(StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNCLAIMED = "UNCLAIMED"
    OTHER = "OTHER"


class AgentName(StrEnum):
    CLAIMANT = "CLAIMANT"
    EXECUTOR = "EXECUTOR"
    SAFETY = "SAFETY"


class AgentDraftTargetType(StrEnum):
    RECEIPT = "RECEIPT"
    SETTLEMENT_RUN = "SETTLEMENT_RUN"


class ActorType(StrEnum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"
