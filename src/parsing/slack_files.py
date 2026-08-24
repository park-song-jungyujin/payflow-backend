"""Slack file_url_private → 원본 바이트 (A 소유).

`receipts`에는 slack_file_id만 있다(ingest/store.py) — url_private는 files.info로
조회한다. 스키마 v0.5.0을 건드리지 않는 유일한 경로다.

**실패를 두 종류로 가른다.** 이 구분이 파이프라인의 상태 결정을 좌우한다:
- TransientParseError — 네트워크·429·5xx·설정 누락. 재시도하면 된다.
  receipts 상태를 건드리지 않고 503으로 올려 Cloud Tasks가 다시 부르게 둔다.
- PermanentParseError — file_not_found·HTML 응답. 다시 불러도 같다. FAILED로 확정한다.

일시적 실패에 FAILED를 찍으면 멀쩡한 영수증이 재요청 DM 대상이 된다.
"""

import os
import re

import requests as http_requests
from pydantic import BaseModel

from ..auth.store import get_slack_workspace_by_org

_SLACK_FILES_INFO = "https://slack.com/api/files.info"
_TIMEOUT_SECONDS = 20
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

_EXT_BY_MIMETYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "image/webp": "webp",
}

# ext는 나중에 objects storage 키 images/{receipt_id}.{ext}가 된다. Slack이 주는
# filetype은 업로더가 정하는 신뢰할 수 없는 입력이라 화이트리스트로 검증한다.
_SAFE_EXT = re.compile(r"^[a-z0-9]{1,8}$")


class TransientParseError(RuntimeError):
    """재시도하면 될 실패. 상태를 바꾸지 않는다."""


class PermanentParseError(RuntimeError):
    """다시 불러도 같은 실패. receipts.status = FAILED로 확정한다."""


class SlackFile(BaseModel):
    data: bytes
    mimetype: str
    ext: str


def _bot_token(org_id: str) -> str:
    workspace = get_slack_workspace_by_org(org_id)
    if workspace is not None:
        return workspace["bot_token"]
    # ingest/routes.py와 같은 폴백 — 정식 설치(/auth/slack/callback)를 아직
    # 아무도 안 거쳤어도, ingest/slack_client.py가 발송용으로 이미 쓰는 전역
    # SLACK_BOT_TOKEN이 있으면 그걸로 다운로드도 시도한다.
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        # 설정 누락이지 영수증 문제가 아니다. 영구 실패로 찍으면 안 된다.
        raise TransientParseError(f"no slack_workspaces installed for org_id={org_id}")
    return token


def download_slack_file(slack_file_id: str, org_id: str) -> SlackFile:
    headers = {"Authorization": f"Bearer {_bot_token(org_id)}"}

    try:
        info = http_requests.get(
            _SLACK_FILES_INFO, headers=headers, params={"file": slack_file_id}, timeout=_TIMEOUT_SECONDS
        )
    except http_requests.RequestException as e:
        raise TransientParseError(f"files.info failed: {e}") from e

    if info.status_code in _RETRYABLE_STATUS:
        raise TransientParseError(f"files.info returned {info.status_code}")

    try:
        body = info.json()
    except ValueError as e:
        # 인프라(게이트웨이/프록시)가 뱉는 비-JSON 응답이지 이 영수증의 속성이 아니다.
        raise TransientParseError(f"files.info returned non-JSON body: {e}") from e

    if not body.get("ok"):
        raise PermanentParseError(f"files.info error: {body.get('error')}")

    slack_file = body.get("file")
    if not slack_file:
        raise PermanentParseError(f"files.info ok=true but missing 'file' for {slack_file_id}")
    url = slack_file.get("url_private")
    if not url:
        raise PermanentParseError(f"file {slack_file_id} has no url_private")

    try:
        response = http_requests.get(url, headers=headers, timeout=_TIMEOUT_SECONDS)
    except http_requests.RequestException as e:
        raise TransientParseError(f"url_private download failed: {e}") from e

    if response.status_code in _RETRYABLE_STATUS:
        raise TransientParseError(f"url_private returned {response.status_code}")
    if response.status_code != 200:
        raise PermanentParseError(f"url_private returned {response.status_code}")

    # 토큰 스코프가 모자라면 Slack은 200 + HTML 로그인 페이지를 준다.
    # 이걸 이미지로 넘기면 파싱이 조용히 이상해진다.
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    if not content_type.startswith("image/"):
        raise PermanentParseError(f"expected image, got Content-Type={content_type!r}")

    fallback_ext = slack_file.get("filetype") or "bin"
    if not _SAFE_EXT.match(fallback_ext):
        fallback_ext = "bin"

    return SlackFile(
        data=response.content,
        mimetype=content_type,
        ext=_EXT_BY_MIMETYPE.get(content_type, fallback_ext),
    )
