"""schema-contract.md §10 — POST /slack/events · /slack/interactions 서명 검증.

**raw body를 그대로 써야 한다.** FastAPI가 파싱한 dict를 다시 직렬화하면 키 순서와
공백이 달라져 서명이 깨진다. /slack/interactions는 본문이 form-encoded(`payload=`)라
더 그렇다 — 두 라우트가 이 함수를 공유하는 이유다.
"""

import hashlib
import hmac
import os
import time

# Slack 권장값. 이보다 오래된 요청은 서명이 맞아도 재전송 공격으로 본다.
MAX_SKEW_SECONDS = 60 * 5


class SignatureError(Exception):
    """서명 검증 실패. 호출부가 401로 바꾼다."""


def verify_slack_signature(raw_body: bytes, timestamp: str, signature: str) -> None:
    if not timestamp or not signature:
        raise SignatureError("missing signature headers")

    try:
        sent_at = int(timestamp)
    except ValueError:
        raise SignatureError("malformed timestamp")

    if abs(time.time() - sent_at) > MAX_SKEW_SECONDS:
        raise SignatureError("timestamp outside allowed skew")

    secret = os.environ["SLACK_SIGNING_SECRET"].encode("utf-8")
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + raw_body
    expected = "v0=" + hmac.new(secret, basestring, hashlib.sha256).hexdigest()

    # compare_digest — 타이밍 공격 방어. == 로 비교하지 않는다.
    # bytes로 넘긴다: str끼리는 non-ASCII가 섞이면 TypeError가 나고, 그러면
    # 401이어야 할 적대적 요청이 500이 된다. 500은 Slack 재전송까지 부른다.
    if not hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8")):
        raise SignatureError("signature mismatch")
