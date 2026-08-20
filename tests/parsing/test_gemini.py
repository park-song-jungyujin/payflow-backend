"""Vertex Gemini 단발 호출 (ADK 아님 — agent-tools.md).

실제 Vertex에 붙지 않는다. 검증하는 건 세 가지다:
1. 응답 스키마로 ParsedReceipt를 넘긴다 — 모델이 amount_minor를 만들 자리가 없다
2. 프롬프트가 영수증 텍스트를 비신뢰 입력으로 다룬다
3. 실패가 Transient/Permanent로 갈린다
"""

import pytest
from google.genai import errors as genai_errors

from src.parsing import gemini
from src.parsing.models import ParsedReceipt
from src.parsing.slack_files import PermanentParseError, TransientParseError


class FakeResponse:
    def __init__(self, parsed=None, text=None):
        self.parsed = parsed
        self.text = text


class FakeModels:
    def __init__(self, response=None, error=None):
        self._response, self._error = response, error
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


class FakeClient:
    def __init__(self, models):
        self.models = models


@pytest.fixture(autouse=True)
def _model_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_ID", "gemini-test")
    monkeypatch.setenv("VERTEX_LOCATION", "asia-northeast3")


def _parser(monkeypatch, models):
    monkeypatch.setattr(gemini, "_build_client", lambda: FakeClient(models))
    return gemini.VertexReceiptParser()


def test_returns_parsed_receipt(monkeypatch):
    expected = ParsedReceipt(merchant_name="다이소 강남점", amount_text="32,000", currency="KRW")
    parser = _parser(monkeypatch, FakeModels(FakeResponse(parsed=expected)))

    result = parser.parse(image=b"\xff\xd8img", mimetype="image/jpeg", receipt_id="rct_1")
    assert result.merchant_name == "다이소 강남점"
    assert result.amount_text == "32,000"


def test_response_schema_has_no_minor_unit_field(monkeypatch):
    """모델에게 ×100을 시킬 자리가 없어야 한다 (절대 규칙 3)."""
    models = FakeModels(FakeResponse(parsed=ParsedReceipt()))
    _parser(monkeypatch, models).parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_1")

    schema = models.calls[0]["config"]["response_schema"]
    assert schema is ParsedReceipt
    assert "amount_minor" not in ParsedReceipt.model_fields
    assert "amount_text" in ParsedReceipt.model_fields


def test_prompt_marks_receipt_text_as_untrusted(monkeypatch):
    """agent-tools.md §입력 신뢰도 — 영수증 텍스트는 비신뢰 입력이다."""
    models = FakeModels(FakeResponse(parsed=ParsedReceipt()))
    _parser(monkeypatch, models).parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_1")

    prompt = str(models.calls[0]["contents"])
    assert "untrusted" in prompt.lower()


def test_uses_model_id_from_env(monkeypatch):
    models = FakeModels(FakeResponse(parsed=ParsedReceipt()))
    _parser(monkeypatch, models).parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_1")
    assert models.calls[0]["model"] == "gemini-test"


def test_unparseable_response_is_permanent(monkeypatch):
    """구조화 출력이 안 나왔다. 다시 불러도 같은 이미지다."""
    parser = _parser(monkeypatch, FakeModels(FakeResponse(parsed=None, text="죄송합니다")))
    with pytest.raises(PermanentParseError):
        parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_1")


def test_api_error_is_transient(monkeypatch):
    parser = _parser(monkeypatch, FakeModels(error=RuntimeError("503 Service Unavailable")))
    with pytest.raises(TransientParseError):
        parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_1")


def test_client_error_404_is_permanent(monkeypatch):
    """모델 ID 오타 같은 4xx(429 제외)는 재시도해도 똑같이 실패한다 — Permanent."""
    error = genai_errors.ClientError(404, {"error": {"message": "model not found", "status": "NOT_FOUND"}})
    parser = _parser(monkeypatch, FakeModels(error=error))
    with pytest.raises(PermanentParseError):
        parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_1")


def test_client_error_400_is_permanent(monkeypatch):
    """미지원 mimetype(HEIC 등)이 부르는 400 INVALID_ARGUMENT도 Permanent."""
    error = genai_errors.ClientError(400, {"error": {"message": "invalid mimetype", "status": "INVALID_ARGUMENT"}})
    parser = _parser(monkeypatch, FakeModels(error=error))
    with pytest.raises(PermanentParseError):
        parser.parse(image=b"x", mimetype="image/heic", receipt_id="rct_1")


def test_client_error_429_is_transient(monkeypatch):
    """쿼터 초과는 4xx이지만 재시도하면 풀린다 — Transient로 남아야 한다."""
    error = genai_errors.ClientError(429, {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
    parser = _parser(monkeypatch, FakeModels(error=error))
    with pytest.raises(TransientParseError):
        parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_1")


def test_server_error_503_is_transient(monkeypatch):
    error = genai_errors.ServerError(503, {"error": {"message": "unavailable", "status": "UNAVAILABLE"}})
    parser = _parser(monkeypatch, FakeModels(error=error))
    with pytest.raises(TransientParseError):
        parser.parse(image=b"x", mimetype="image/jpeg", receipt_id="rct_1")
