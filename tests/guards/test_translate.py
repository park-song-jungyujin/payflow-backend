"""Gemma 단발 번역 호출 (ADK 아님). 실제 Vertex에 붙지 않는다.

검증하는 건: 빈 입력은 호출을 생략하는지, 정상 응답을 그대로 돌려주는지,
개수가 안 맞거나 파싱 실패면 None인지, 4xx/5xx/네트워크 실패가 전부
None으로 흡수되는지(번역은 조언성 부가 기능이라 예외를 올리지 않는다),
그리고 실패했을 때 원인을 특정할 수 있는 진단 정보를 로그에 남기는지."""

import logging

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


class ScriptedModels:
    """호출 순서대로 준비된 응답을 돌려준다 — 청크 분할·줄 단위 재시도처럼
    Gemma를 여러 번 부르는 경로를 검증할 때 쓴다."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(text=self._texts[len(self.calls) - 1])


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


def test_default_target_language_is_english_in_prompt(monkeypatch):
    """target_language 인자 없이 부르는 기존 호출부(CLAIMANT)와 하위호환된다."""
    models = FakeModels(FakeResponse(text='["hello"]'))
    _mock(monkeypatch, models)

    translate.translate_lines(["안녕"])

    assert "English" in models.calls[0]["contents"]


def test_target_language_is_embedded_in_prompt(monkeypatch):
    """EXECUTOR가 영어 → 한국어로 뒤집어 부르는 경로 — target_language가
    프롬프트에 그대로 들어가는지 확인한다."""
    models = FakeModels(FakeResponse(text='["안녕"]'))
    _mock(monkeypatch, models)

    result = translate.translate_lines(["hello"], target_language="Korean")

    assert result == ["안녕"]
    assert "Korean" in models.calls[0]["contents"]
    assert "English" not in models.calls[0]["contents"]


def test_count_mismatch_returns_none_when_line_retry_also_fails(monkeypatch):
    """줄 수가 어긋나면 줄 단위로 다시 부르고, 거기서도 실패하면 None이다."""
    models = ScriptedModels(['["hello"]', "nope", "nope"])
    _mock(monkeypatch, models)

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


# --- 실패 진단 로그 ---
#
# 실패를 조용히 흡수하는 설계라(번역은 조언성 부가 기능) 로그가 유일한 단서다.
# 응답 원문을 안 남기면 "malformed output"까지만 알 수 있고 실제 원인(코드펜스·
# 객체 반환·프롤로그 등)은 영영 특정 못 한다 — 실제로 그래서 한 번 막혔다
# (payflow-docs journal 2026-08-26 "Gemma 번역 기능 동작 여부 확인").


def test_malformed_output_logs_the_raw_response(monkeypatch, caplog):
    _mock(monkeypatch, FakeModels(FakeResponse(text="I cannot translate that.")))

    with caplog.at_level(logging.WARNING, logger="src.guards.translate"):
        assert translate.translate_lines(["안녕"]) is None

    assert "I cannot translate that." in caplog.text


def test_count_mismatch_logs_the_raw_response(monkeypatch, caplog):
    models = ScriptedModels(['["hello"]', "nope", "nope"])
    _mock(monkeypatch, models)

    with caplog.at_level(logging.WARNING, logger="src.guards.translate"):
        assert translate.translate_lines(["안녕", "세계"]) is None

    assert '["hello"]' in caplog.text


def test_empty_response_text_is_logged_as_such(monkeypatch, caplog):
    """차단·빈 candidates면 response.text가 None이다 — 파싱 실패와 원인이
    달라서 로그에서 구분돼야 한다."""
    _mock(monkeypatch, FakeModels(FakeResponse(text=None)))

    with caplog.at_level(logging.WARNING, logger="src.guards.translate"):
        assert translate.translate_lines(["안녕"]) is None

    assert "empty response" in caplog.text


def test_raw_response_is_truncated_in_the_log(monkeypatch, caplog):
    """응답이 통째로 길면 로그가 넘친다 — 원인 특정에 필요한 앞부분만 남긴다."""
    _mock(monkeypatch, FakeModels(FakeResponse(text="x" * 5000)))

    with caplog.at_level(logging.WARNING, logger="src.guards.translate"):
        assert translate.translate_lines(["안녕"]) is None

    assert len(caplog.text) < 2000
    assert "..." in caplog.text


# --- Gemma 출력 형식 흡수 ---
#
# Gemma는 Vertex의 구조화 출력(response_schema)을 지키지 않아 프롬프트로만
# 형식을 지시하는데, 그 지시도 호출마다 흔들린다 — 같은 문구가 어떤 호출에선
# 영어로, 어떤 호출에선 한국어로 도착하던(=번역이 간헐 실패하던) 원인이다.
# 배열만 뽑아낼 수 있으면 뽑아 쓴다. 뽑을 수 없을 때만 None이다.


def test_json_code_fence_is_stripped(monkeypatch):
    """가장 흔한 형태 — 프롬프트로 코드펜스를 금지해도 자주 감싸서 준다."""
    _mock(monkeypatch, FakeModels(FakeResponse(text='```json\n["hello", "world"]\n```')))

    assert translate.translate_lines(["안녕", "세계"]) == ["hello", "world"]


def test_bare_code_fence_is_stripped(monkeypatch):
    _mock(monkeypatch, FakeModels(FakeResponse(text='```\n["hello"]\n```')))

    assert translate.translate_lines(["안녕"]) == ["hello"]


def test_prose_around_the_array_is_ignored(monkeypatch):
    """"Here is the translation:" 같은 프롤로그·에필로그를 붙여 주는 경우."""
    _mock(
        monkeypatch,
        FakeModels(FakeResponse(text='Here is the translation:\n["hello", "world"]\nHope this helps!')),
    )

    assert translate.translate_lines(["안녕", "세계"]) == ["hello", "world"]


def test_array_wrapped_in_an_object_is_unwrapped(monkeypatch):
    """배열 대신 객체로 감싸 주는 경우 — 키 이름은 호출마다 다르다."""
    _mock(monkeypatch, FakeModels(FakeResponse(text='{"translations": ["hello", "world"]}')))

    assert translate.translate_lines(["안녕", "세계"]) == ["hello", "world"]


def test_object_with_two_lists_is_rejected(monkeypatch):
    """어느 쪽이 번역인지 고를 근거가 없다 — 찍지 않고 실패로 둔다."""
    _mock(
        monkeypatch,
        FakeModels(FakeResponse(text='{"source": ["안녕"], "translations": ["hello"]}')),
    )

    assert translate.translate_lines(["안녕"]) is None


def test_bare_string_is_accepted_when_exactly_one_line_was_requested(monkeypatch):
    """한 줄만 보내면 배열로 감싸지 않고 문자열만 주는 경우가 잦다 — 재요청
    DM(requery_message)이 정확히 이 경로라 실사용에서 가장 자주 물린다."""
    _mock(monkeypatch, FakeModels(FakeResponse(text='"hello"')))

    assert translate.translate_lines(["안녕"]) == ["hello"]


def test_bare_string_answering_a_multi_line_request_triggers_line_retry(monkeypatch):
    """여러 줄을 보냈는데 문자열 하나가 오면 어느 줄인지 알 수 없다 — 그
    응답을 쓰지 않고 줄 단위로 다시 묻는다."""
    models = ScriptedModels(['"hello"', '"hello"', '"world"'])
    _mock(monkeypatch, models)

    assert translate.translate_lines(["안녕", "세계"]) == ["hello", "world"]
    assert len(models.calls) == 3


def test_fenced_object_is_also_unwrapped(monkeypatch):
    """코드펜스와 객체 래핑이 겹쳐 오는 경우."""
    _mock(monkeypatch, FakeModels(FakeResponse(text='```json\n{"lines": ["hello"]}\n```')))

    assert translate.translate_lines(["안녕"]) == ["hello"]


def test_unquoted_prose_is_rejected_even_for_a_single_line(monkeypatch):
    """따옴표 없는 산문은 번역문인지 거절·해명인지 구분할 근거가 없다 — 받아
    주면 모델의 거절 문구가 그대로 Slack DM 본문으로 나간다. 한국어 폴백이 낫다."""
    _mock(monkeypatch, FakeModels(FakeResponse(text="I cannot translate that.")))

    assert translate.translate_lines(["안녕"]) is None


# --- 줄이 많을 때 (청크 분할) ---
#
# Gemma는 줄 수가 늘수록 "입력과 같은 개수의 배열"을 덜 지킨다(payflow-docs
# journal 2026-08-26 — 관측된 실패 유형 둘 중 하나가 줄 수 불일치였다).
# translate_lines는 전부 아니면 전무라, 한 줄만 어긋나도 그 요청의 번역이
# 통째로 사라진다 — 품목이 많은 영수증은 가맹점명까지 번역을 잃고, 청구 건이
# 여럿인 정산 실행은 이상징후 한국어 번역이 통째로 안 붙는다.
#
# 그래서 큰 입력은 작은 청크로 나눠 부른다. 청크가 실패하면 그 청크만 줄
# 단위로 다시 부른다 — 한 줄짜리 호출이 가장 안정적인 형태다.


def test_small_input_is_still_a_single_call(monkeypatch):
    """청크 크기 이하면 호출 수가 늘지 않는다 — 흔한 경우의 지연을 안 건드린다."""
    models = ScriptedModels(['["1", "2", "3", "4", "5"]'])
    _mock(monkeypatch, models)

    assert translate.translate_lines(["a", "b", "c", "d", "e"]) == ["1", "2", "3", "4", "5"]
    assert len(models.calls) == 1


def test_large_input_is_split_into_chunks_and_concatenated_in_order(monkeypatch):
    models = ScriptedModels(['["1", "2", "3", "4", "5"]', '["6", "7"]'])
    _mock(monkeypatch, models)

    result = translate.translate_lines(["a", "b", "c", "d", "e", "f", "g"])

    assert result == ["1", "2", "3", "4", "5", "6", "7"]
    assert len(models.calls) == 2
    # 두 번째 호출에는 남은 두 줄만 실려야 한다 — 청크가 겹치면 순서가 깨진다.
    assert "f" in models.calls[1]["contents"] and "a" not in models.calls[1]["contents"]


def test_failed_chunk_is_retried_line_by_line(monkeypatch):
    """청크가 줄 수 불일치로 실패해도 그 청크만 줄 단위로 다시 부른다 —
    한 줄이 어긋났다고 나머지 번역까지 버리지 않는다."""
    models = ScriptedModels(
        [
            '["1", "2"]',  # 5줄 청크인데 2개만 왔다 → 실패
            '"1"',
            '"2"',
            '"3"',
            '"4"',
            '"5"',
        ]
    )
    _mock(monkeypatch, models)

    assert translate.translate_lines(["a", "b", "c", "d", "e"]) == ["1", "2", "3", "4", "5"]
    assert len(models.calls) == 6


def test_line_level_retry_failure_gives_up_on_the_whole_batch(monkeypatch):
    """줄 단위로도 실패하면 None이다 — 일부만 번역된 목록을 돌려주면 호출부가
    한국어와 영어가 섞인 화면을 만든다."""
    models = ScriptedModels(['["1", "2"]', '"1"', "그건 번역 못 하겠습니다"])
    _mock(monkeypatch, models)

    assert translate.translate_lines(["a", "b", "c", "d", "e"]) is None


def test_single_line_failure_is_not_retried(monkeypatch):
    """한 줄짜리는 이미 가장 안정적인 형태다 — 같은 호출을 한 번 더 할 이유가 없다."""
    models = ScriptedModels(["I cannot translate that."])
    _mock(monkeypatch, models)

    assert translate.translate_lines(["a"]) is None
    assert len(models.calls) == 1


def test_target_language_is_kept_across_chunks(monkeypatch):
    models = ScriptedModels(['["1", "2", "3", "4", "5"]', '["6"]'])
    _mock(monkeypatch, models)

    translate.translate_lines(["a", "b", "c", "d", "e", "f"], target_language="Korean")

    assert all("Korean" in c["contents"] for c in models.calls)


# --- 여러 줄짜리 원문 ---
#
# 집행자 summary_text는 반려가 있으면 여러 줄이다(payflow-agent
# executor/agent.py — "Rejected items" 섹션을 반려마다 한 줄씩 적으라고 지시).
# 프롬프트가 원문을 "1. ...\n2. ..." 로 이어붙이면 원소 안의 줄바꿈이 그
# 줄 구조를 깨뜨린다 — 모델은 물리적 줄 수만큼 배열을 돌려주고, 개수가
# 어긋나 번역이 통째로 버려진다. 반려가 없을 땐 summary_text가 한 줄이라
# 성공하고, 반려가 생기는 순간부터 한국어가 영영 안 붙었다.
#
# 그래서 원문을 JSON 배열로 넘긴다 — 줄바꿈이 \n으로 이스케이프돼 원소
# 경계가 모호해지지 않는다.


def test_multiline_text_is_sent_as_escaped_json_not_raw_lines(monkeypatch):
    models = ScriptedModels(['["번역"]'])
    _mock(monkeypatch, models)

    translate.translate_lines(["Summary.\n\nRejected items\n#AX5K4MF7: duplicate"])

    contents = models.calls[0]["contents"]
    # 원문의 줄바꿈이 프롬프트에 날것으로 들어가면 원소 경계가 무너진다.
    assert "Rejected items\n#AX5K4MF7" not in contents
    assert "\\n" in contents  # JSON 이스케이프된 형태로 들어간다


def test_multiline_text_round_trips(monkeypatch):
    models = ScriptedModels(['["요약.\\n\\n반려 내역\\n#AX5K4MF7: 중복"]'])
    _mock(monkeypatch, models)

    result = translate.translate_lines(["Summary.\n\nRejected items\n#AX5K4MF7: duplicate"])

    assert result == ["요약.\n\n반려 내역\n#AX5K4MF7: 중복"]


def test_multiline_text_mixed_with_single_line_items_keeps_order(monkeypatch):
    """집행자 번역이 정확히 이 모양이다 — [여러 줄 summary_text, *한 줄 anomalies]."""
    models = ScriptedModels(['["요약\\n둘째 줄", "이상징후 하나", "이상징후 둘"]'])
    _mock(monkeypatch, models)

    result = translate.translate_lines(
        ["Summary\nsecond line", "anomaly one", "anomaly two"], target_language="Korean"
    )

    assert result == ["요약\n둘째 줄", "이상징후 하나", "이상징후 둘"]


def test_input_is_not_numbered_so_the_model_cannot_echo_the_numbering(monkeypatch):
    """원문에 "1. ", "2. "를 붙여 보내면 모델이 그 번호를 그대로 되돌려줘도
    길이·타입 검사를 전부 통과한다 — "1. The total amount ..."가 조용히 DM으로
    나간다. 번호를 아예 안 붙이면 그 경로가 사라진다."""
    models = ScriptedModels(['["hello", "world"]'])
    _mock(monkeypatch, models)

    translate.translate_lines(["안녕", "세계"])

    contents = models.calls[0]["contents"]
    assert "1. 안녕" not in contents
    assert "2. 세계" not in contents
