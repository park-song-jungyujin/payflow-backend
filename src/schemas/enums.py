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


class ExecutorStatus(StrEnum):
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
    # 청구자 에이전트(LLM)가 needs_requery=True로 판정한 일반 케이스. 실제 원인
    # (금액 미검출·모순 등)을 에이전트가 구조화해 넘기지 않아 더 세분화하지
    # 않는다 — AMOUNT_MISMATCH(금액 불일치 전용, 위 표 참조)로 뭉뚱그리면 원인이
    # 아닌 값이 찍힌다.
    CLAIMANT_REVIEW_FAILED = "CLAIMANT_REVIEW_FAILED"
    # 거래일자 미검출은 코드가 결정론적으로 판정한다(parsing/pipeline.py) —
    # 청구자 에이전트를 거치지 않으므로 위 CLAIMANT_REVIEW_FAILED와 구분한다.
    DATE_MISSING = "DATE_MISSING"


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
