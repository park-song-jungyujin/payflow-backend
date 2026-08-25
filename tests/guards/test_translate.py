"""Gemma 단발 번역 호출 (ADK 아님). 실제 Vertex에 붙지 않는다.

검증하는 건: 빈 입력은 호출을 생략하는지, 정상 응답을 그대로 돌려주는지,
개수가 안 맞거나 파싱 실패면 None인지, 4xx/5xx/네트워크 실패가 전부
None으로 흡수되는지(번역은 조언성 부가 기능이라 예외를 올리지 않는다)."""

import pytest
from google.genai import errors as genai_errors

from src.guards import translate


class FakeResponse:
    def __init__(self, text=None):
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
    monkeypatch.setenv("GEMMA_MODEL_ID", "gemma-test")
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("VERTEX_LOCATION", "asia-northeast3")


def _mock(monkeypatch, models):
    monkeypatch.setattr(translate, "_build_client", lambda: FakeClient(models))


def test_empty_input_skips_call_entirely(monkeypatch):
    models = FakeModels()
    _mock(monkeypatch, models)

    assert translate.translate_lines([]) == []
    assert models.calls == []


def test_returns_translations_in_order(monkeypatch):
    _mock(monkeypatch, FakeModels(FakeResponse(text='["hello", "world"]')))

    assert translate.translate_lines(["안녕", "세계"]) == ["hello", "world"]


def test_count_mismatch_returns_none(monkeypatch):
    _mock(monkeypatch, FakeModels(FakeResponse(text='["hello"]')))

    assert translate.translate_lines(["안녕", "세계"]) is None


def test_non_string_items_returns_none(monkeypatch):
    _mock(monkeypatch, FakeModels(FakeResponse(text='["hello", 1]')))

    assert translate.translate_lines(["안녕", "세계"]) is None


def test_non_array_json_returns_none(monkeypatch):
    _mock(monkeypatch, FakeModels(FakeResponse(text='{"1": "hello"}')))

    assert translate.translate_lines(["안녕"]) is None


def test_invalid_json_returns_none(monkeypatch):
    _mock(monkeypatch, FakeModels(FakeResponse(text="not json")))

    assert translate.translate_lines(["안녕"]) is None


def test_client_error_returns_none_not_raises(monkeypatch):
    error = genai_errors.ClientError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    _mock(monkeypatch, FakeModels(error=error))

    assert translate.translate_lines(["안녕"]) is None


def test_server_error_returns_none_not_raises(monkeypatch):
    error = genai_errors.ServerError(503, {"error": {"message": "unavailable", "status": "UNAVAILABLE"}})
    _mock(monkeypatch, FakeModels(error=error))

    assert translate.translate_lines(["안녕"]) is None


def test_unexpected_exception_returns_none_not_raises(monkeypatch):
    _mock(monkeypatch, FakeModels(error=RuntimeError("network down")))

    assert translate.translate_lines(["안녕"]) is None
