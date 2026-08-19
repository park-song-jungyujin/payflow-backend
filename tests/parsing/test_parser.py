"""파서 경계. 실제 Gemini 호출은 마지막에 붙인다(Task 9) — 그전까지 fixture
9종을 재생하는 구현체로 파이프라인 전체를 돌린다.

fixture는 파싱 *결과*를 담고 있으므로, 파서가 냈을 *입력 형태*로 되돌려 재생한다.
amount_text는 minor_to_paypal_value로 역변환한다 — KRW 45000 → "45000",
USD 2500 → "25.00". 파이프라인이 amount_to_minor로 다시 접으면 원래 값이 나온다.
"""

from datetime import date

import pytest

from src.parsing.models import ParsedReceipt, amount_to_minor
from src.parsing.parser import FixtureReceiptParser, get_parser
from src.schemas.enums import AccountCategory

FIXTURES = [
    "tests/fixtures/01_golden_path_fx.json",
    "tests/fixtures/02_parse_failure_requery.json",
    "tests/fixtures/04_low_confidence_unclassified.json",
    "tests/fixtures/06_prompt_injection.json",
]


def test_replays_llm_parse_receipt():
    parser = FixtureReceiptParser.from_fixtures(FIXTURES)
    parsed = parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_01SCN01USDITEM000000002")

    assert parsed.merchant_name == "Notion Labs Inc"
    assert parsed.transaction_date == date(2026, 8, 6)
    assert parsed.currency == "USD"
    assert parsed.confidence == 0.88
    assert parsed.account_category_code is AccountCategory.ADVERTISING


def test_amount_text_round_trips_through_minor_conversion():
    """USD 2500 minor → "25.00" → 다시 2500. 이 왕복이 깨지면 fixture 재생이
    실제 파싱과 다른 금액을 흘려보낸다."""
    parser = FixtureReceiptParser.from_fixtures(FIXTURES)
    parsed = parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_01SCN01USDITEM000000002")
    assert parsed.amount_text == "25.00"
    assert amount_to_minor(parsed.amount_text, parsed.currency) == 2500

    krw = parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_01SCN01KRWITEM000000001")
    assert krw.amount_text == "45000"
    assert amount_to_minor(krw.amount_text, krw.currency) == 45000


def test_deterministic_fallback_category_is_not_replayed_as_llm_output():
    """fixture 06의 UNCLASSIFIED는 코드가 정한 값이지 LLM이 낸 값이 아니다.
    그대로 재생하면 라우팅 테스트가 자기 자신을 검증하는 꼴이 된다."""
    parser = FixtureReceiptParser.from_fixtures(FIXTURES)
    parsed = parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_01SCN06INJECTIONRCT00001")
    assert parsed.account_category_code is None
    assert parsed.confidence is None


def test_injection_raw_text_is_replayed_verbatim():
    parser = FixtureReceiptParser.from_fixtures(FIXTURES)
    parsed = parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_01SCN06INJECTIONRCT00001")
    assert "SYSTEM:" in parsed.raw_text


def test_blurry_receipt_replays_missing_fields():
    parser = FixtureReceiptParser.from_fixtures(FIXTURES)
    parsed = parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_01SCN02BLURRYPHOTO0000001")
    assert parsed.merchant_name is None
    assert parsed.amount_text is None
    assert parsed.currency is None
    assert parsed.transaction_date == date(2026, 8, 9)


def test_unknown_receipt_id_raises():
    parser = FixtureReceiptParser({})
    with pytest.raises(KeyError):
        parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_nope")


def test_factory_returns_fixture_parser_without_model_id(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL_ID", raising=False)
    assert isinstance(get_parser(), FixtureReceiptParser)
