"""파싱 모델 경계 (A 소유).

실제 Gemini 호출을 붙이기 전에 파이프라인 전체를 돌리기 위한 이음매다.
fixture 9종이 이미 데모 데이터셋이자 계약 예시라(schema-contract.md §12),
같은 파일을 재생 소스로 쓴다 — 따로 만들면 두 번 만든다.
"""

import glob
import json
import os
from datetime import date
from typing import Protocol

from ..payouts.currency import minor_to_paypal_value
from ..schemas.enums import AccountCategory, CategorySource
from .models import ParsedReceipt

DEFAULT_FIXTURE_GLOB = "tests/fixtures/*.json"


class ReceiptParser(Protocol):
    def parse(self, *, image: bytes, mimetype: str, receipt_id: str) -> ParsedReceipt:
        """이미지 1장 → 구조화 결과. 단발 호출이고 세션이 없다 (agent-tools.md).
        실패는 예외로 올린다 — 파이프라인이 Transient/Permanent로 갈라 처리한다."""
        ...


class FixtureReceiptParser:
    """fixture receipt 문서를 파서 *입력 형태*로 되돌려 재생한다.

    되돌리는 게 핵심이다. fixture에 저장된 account_category_code·llm_confidence는
    §5 라우팅을 **거친 뒤** 값이라, 그대로 흘려보내면 라우팅 테스트가 자기가 낸
    답을 자기가 채점하게 된다. category_source가 LLM_PARSE인 것만 LLM이 낸
    값으로 취급하고, DETERMINISTIC_FALLBACK/EXECUTOR_AGENT는 None으로 되돌린다.
    """

    def __init__(self, by_receipt_id: dict[str, ParsedReceipt]):
        self._by_receipt_id = by_receipt_id

    @classmethod
    def from_fixtures(cls, paths: list[str] | None = None) -> "FixtureReceiptParser":
        parsed: dict[str, ParsedReceipt] = {}
        for path in sorted(paths if paths is not None else glob.glob(DEFAULT_FIXTURE_GLOB)):
            with open(path, encoding="utf-8") as f:
                document = json.load(f)
            raw_text_sample = document.get("_fixture_note_raw_text_sample")
            for receipt in document.get("receipts", []):
                parsed[receipt["receipt_id"]] = cls._to_parsed(receipt, raw_text_sample)
        return cls(parsed)

    @staticmethod
    def _to_parsed(receipt: dict, raw_text_sample: str | None) -> ParsedReceipt:
        amount_minor = receipt.get("parsed_amount_minor")
        currency = receipt.get("currency")
        amount_text = (
            minor_to_paypal_value(amount_minor, currency)
            if amount_minor is not None and currency
            else None
        )

        source = receipt.get("category_source")
        llm_category = (
            AccountCategory(receipt["account_category_code"])
            if source == CategorySource.LLM_PARSE and receipt.get("account_category_code")
            else None
        )
        confidence = receipt.get("llm_confidence") if source == CategorySource.LLM_PARSE else None

        transaction_date = receipt.get("transaction_date")
        return ParsedReceipt(
            merchant_name=receipt.get("merchant_name"),
            transaction_date=date.fromisoformat(transaction_date) if transaction_date else None,
            amount_text=amount_text,
            currency=currency,
            account_category_code=llm_category,
            confidence=confidence,
            raw_text=raw_text_sample
            or " ".join(
                str(part)
                for part in (receipt.get("merchant_name"), amount_text, transaction_date)
                if part
            ),
        )

    def parse(self, *, image: bytes, mimetype: str, receipt_id: str) -> ParsedReceipt:
        return self._by_receipt_id[receipt_id]


def get_parser() -> ReceiptParser:
    """GEMINI_MODEL_ID가 비어 있으면 fixture 재생기를 쓴다. §11이 이 변수를 빈 채로
    남겨둔 상태이고(A가 Vertex 콘솔에서 확인해 채운다), 채워지기 전까지 파이프라인이
    돌아가야 한다."""
    if os.environ.get("GEMINI_MODEL_ID"):
        from .gemini import VertexReceiptParser

        return VertexReceiptParser()
    return FixtureReceiptParser.from_fixtures()
