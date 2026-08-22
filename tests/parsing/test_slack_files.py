"""Slack file_url_private로 원본 이미지를 받는다.

receipts 문서에는 slack_file_id만 있고 url_private는 없다(ingest/store.py) —
files.info로 URL을 먼저 조회하는 이유다. 스키마 v0.5.0을 안 건드린다.

url_private는 공개 URL이 아니다. 봇 토큰 Bearer 없이 GET하면 HTML 로그인
페이지가 200으로 돌아온다 — 그걸 이미지로 착각해 Gemini에 넣으면 파싱이
조용히 이상해진다. 그래서 Content-Type을 검사한다.
"""

import pytest

from src.parsing import slack_files
from src.parsing.slack_files import PermanentParseError, TransientParseError


class FakeResponse:
    def __init__(self, *, status_code=200, json_body=None, content=b"", headers=None):
        self.status_code = status_code
        self._json = json_body or {}
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def _bot_token(monkeypatch):
    monkeypatch.setattr(
        slack_files,
        "get_slack_workspace_by_org",
        lambda org_id: {"bot_token": "xoxb-test"},
    )


def _wire(monkeypatch, info_response, download_response=None):
    calls = []

    def fake_get(url, headers=None, timeout=None, params=None):
        calls.append({"url": url, "headers": headers, "params": params})
        if "files.info" in url:
            return info_response
        return download_response

    monkeypatch.setattr(slack_files.http_requests, "get", fake_get)
    return calls


def _ok_info(url="https://files.slack.com/priv/F1/receipt.jpg", mimetype="image/jpeg"):
    return FakeResponse(json_body={"ok": True, "file": {"url_private": url, "mimetype": mimetype, "filetype": "jpg"}})


def test_downloads_with_bearer_token(monkeypatch):
    calls = _wire(
        monkeypatch,
        _ok_info(),
        FakeResponse(content=b"\xff\xd8jpegbytes", headers={"Content-Type": "image/jpeg"}),
    )

    result = slack_files.download_slack_file("F01ABCDEF", "org_1")

    assert result.data == b"\xff\xd8jpegbytes"
    assert result.mimetype == "image/jpeg"
    assert result.ext == "jpg"
    assert all(c["headers"]["Authorization"] == "Bearer xoxb-test" for c in calls)


def test_looks_up_url_via_files_info(monkeypatch):
    calls = _wire(monkeypatch, _ok_info(), FakeResponse(content=b"x", headers={"Content-Type": "image/jpeg"}))
    slack_files.download_slack_file("F01ABCDEF", "org_1")
    assert "files.info" in calls[0]["url"]
    assert calls[0]["params"] == {"file": "F01ABCDEF"}


def test_html_login_page_is_permanent_failure(monkeypatch):
    """토큰 스코프가 모자라면 Slack이 200 + HTML을 준다. 이미지로 착각하면 안 된다."""
    _wire(monkeypatch, _ok_info(), FakeResponse(content=b"<html>login", headers={"Content-Type": "text/html"}))
    with pytest.raises(PermanentParseError):
        slack_files.download_slack_file("F01ABCDEF", "org_1")


def test_slack_api_error_is_permanent(monkeypatch):
    _wire(monkeypatch, FakeResponse(json_body={"ok": False, "error": "file_not_found"}))
    with pytest.raises(PermanentParseError):
        slack_files.download_slack_file("F01ABCDEF", "org_1")


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_retryable_status_is_transient(monkeypatch, status):
    """일시적 실패에 FAILED를 찍으면 멀쩡한 영수증이 재요청 대상이 된다.
    상태를 안 바꾸고 Cloud Tasks가 다시 부르게 둔다."""
    _wire(monkeypatch, _ok_info(), FakeResponse(status_code=status, headers={"Content-Type": "text/plain"}))
    with pytest.raises(TransientParseError):
        slack_files.download_slack_file("F01ABCDEF", "org_1")


def test_network_error_is_transient(monkeypatch):
    def boom(*args, **kwargs):
        raise slack_files.http_requests.RequestException("connection reset")

    monkeypatch.setattr(slack_files.http_requests, "get", boom)
    with pytest.raises(TransientParseError):
        slack_files.download_slack_file("F01ABCDEF", "org_1")


def test_missing_bot_token_is_transient(monkeypatch):
    """설정 누락이지 영수증 문제가 아니다. FAILED로 찍어 재요청 DM을 보내면 안 된다."""
    monkeypatch.setattr(slack_files, "get_slack_workspace_by_org", lambda org_id: None)
    with pytest.raises(TransientParseError):
        slack_files.download_slack_file("F01ABCDEF", "org_1")


class RaisingJsonResponse:
    """`.json()`이 예외를 던지는 가짜 응답 — 게이트웨이/프록시의 비-JSON 오류 페이지를 흉내낸다."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.content = b""
        self.headers = {}

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_files_info_retryable_status_is_transient(monkeypatch, status):
    """files.info 자체가 재시도 대상 상태 코드를 주는 경로."""
    _wire(monkeypatch, FakeResponse(status_code=status))
    with pytest.raises(TransientParseError):
        slack_files.download_slack_file("F01ABCDEF", "org_1")


def test_files_info_non_json_body_is_transient(monkeypatch):
    """F1: files.info가 200 + 비-JSON 본문을 주면 인프라 이상이지 영수증 속성이 아니다."""
    _wire(monkeypatch, RaisingJsonResponse())
    with pytest.raises(TransientParseError):
        slack_files.download_slack_file("F01ABCDEF", "org_1")


def test_files_info_non_retryable_error_is_permanent(monkeypatch):
    """403/404처럼 재시도 대상이 아닌 상태 코드 + ok:false JSON 본문은 영구 실패다."""
    _wire(
        monkeypatch,
        FakeResponse(status_code=404, json_body={"ok": False, "error": "file_not_found"}),
    )
    with pytest.raises(PermanentParseError):
        slack_files.download_slack_file("F01ABCDEF", "org_1")


def test_ok_true_but_missing_file_key_is_permanent(monkeypatch):
    """ok:true인데 'file'이 없는 비정상 응답(스코프 변경 등)이 KeyError로 새 나가
    영수증을 RECEIVED에 영구 잔류시키면 안 된다."""
    info = FakeResponse(json_body={"ok": True})
    _wire(monkeypatch, info)
    with pytest.raises(PermanentParseError):
        slack_files.download_slack_file("F01ABCDEF", "org_1")


def test_missing_url_private_is_permanent(monkeypatch):
    info = FakeResponse(json_body={"ok": True, "file": {"mimetype": "image/jpeg", "filetype": "jpg"}})
    _wire(monkeypatch, info)
    with pytest.raises(PermanentParseError):
        slack_files.download_slack_file("F01ABCDEF", "org_1")


def test_unknown_mimetype_falls_back_to_safe_filetype(monkeypatch):
    """알려진 4개 mimetype 밖이면 filetype 폴백을 쓰되, 안전한 값만 통과시킨다."""
    info = _ok_info(mimetype="image/gif")
    _wire(monkeypatch, info, FakeResponse(content=b"gifbytes", headers={"Content-Type": "image/gif"}))
    result = slack_files.download_slack_file("F01ABCDEF", "org_1")
    assert result.ext == "jpg"  # _ok_info의 filetype 기본값


def test_unsafe_filetype_falls_back_to_bin(monkeypatch):
    """F2: filetype이 경로 구분자 등 위험한 값이면 storage 키를 보호하기 위해 bin으로 떨어뜨린다."""
    info = FakeResponse(
        json_body={
            "ok": True,
            "file": {
                "url_private": "https://files.slack.com/priv/F1/receipt",
                "mimetype": "image/gif",
                "filetype": "../../etc/passwd",
            },
        }
    )
    _wire(monkeypatch, info, FakeResponse(content=b"gifbytes", headers={"Content-Type": "image/gif"}))
    result = slack_files.download_slack_file("F01ABCDEF", "org_1")
    assert result.ext == "bin"
