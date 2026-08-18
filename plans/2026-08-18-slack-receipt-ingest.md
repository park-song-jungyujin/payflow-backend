# Slack 영수증 인입 경로 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Slack에 올라온 영수증 이미지를 서명 검증 후 3초 안에 ack하고, `receipts` 문서를 하나 만들어 Cloud Tasks로 파싱을 위임한다.

**Architecture:** `POST /slack/events`가 raw body로 Slack v0 서명을 검증하고, `message` 이벤트에 붙은 이미지 파일마다 `receipts` 문서를 `RECEIVED` 상태로 만든 뒤 파싱 태스크를 enqueue하고 즉시 200을 돌려준다. 파싱·PII 마스킹·`claims` 생성은 이 계획에 없다. 중복 방지는 `slack_file_id`를 키로 Firestore 트랜잭션 안에서 한다 — Slack은 ack가 늦으면 같은 이벤트를 재전송하고, Cloud Tasks도 재시도한다.

**Tech Stack:** FastAPI, Firestore (`google-cloud-firestore`), Cloud Tasks (`google-cloud-tasks`), `python-ulid`, pytest + httpx.

## 실행 순서와 게이트

```
Task 1 ① docs 반영          ✅ 완료 — payflow-docs 88a6031
Task 1 ② src/schemas 수정    ← 지금. schema: 커밋까지
Task 1 ③ v0.4.0 태그 + 통보  ← 사용자가 직접. "시작" 신호를 준다
        ↓
Task 2 → Task 3 → Task 5 → Task 6
        ↑
Task 4 는 보류 (아래 참조). Task 5는 Task 4 없이도 완결된다.
```

**Task 2는 ③이 끝나고 "시작"이라는 말을 들은 뒤에 착수한다.** 그전에는 실행하지 않는다.

## Global Constraints

- 소유 디렉터리는 `src/ingest/`와 `src/parsing/`뿐이다. `src/guards/`, `src/payouts/`, `src/matching/`, `src/settlements/`는 **수정하지 않는다** — import(읽기)는 허용된다.
- `src/schemas/`는 공유다. 변경 시 `schema:` 접두사 커밋 + 본문에 `affects:` 명시 (`schema-contract.md` §13). 태그와 전원 통보는 사용자가 한다.
- 변경 순서는 `docs → api → agent → web`. 역순 금지.
- 상태 필드 이름은 전 컬렉션 `status`, 상태 값은 전부 `UPPER_SNAKE`.
- 시각은 Firestore `Timestamp`, 저장은 UTC. 예외는 `receipts.transaction_date`(`YYYY-MM-DD` 문자열) 하나.
- 금액은 정수 minor unit. `float` 금지.
- Slack webhook은 **3초 안에 200**을 돌려준다. 3초 넘게 걸리는 일은 전부 Cloud Tasks로 넘긴다.
- 커밋 메시지 · 주석 · docstring은 한국어. 기존 파일들의 톤을 따른다.
- `tests/fixtures/`는 **추가만, 수정 금지**.
- 이 계획의 범위는 **인입 · ack · enqueue까지**다. 파싱, PII 마스킹, `claims` 생성, DM 발송, 재촉 루프는 들어가지 않는다.

## 확정된 계약 — payflow-docs `88a6031`

`src/schemas/`는 이것과 **정확히** 일치해야 한다. 더도 덜도 아니다.

| 변경 | 내용 |
|---|---|
| `receipts.slack_file_id` 신설 | `str \| None`. `F0123ABC`. Slack 재전송 dedup 키 |
| `receipts` 파싱 필드 5개 완화 | `image_gcs_uri` · `raw_text_gcs_uri` · `currency` · `category_source` · `parse_signals` → 전부 `\| None` |
| dedup 트랜잭션 규칙 | "`slack_file_id` 중복 검사와 `receipts` 생성은 하나의 Firestore 트랜잭션 안에서 한다" — 문서 규칙이라 코드 변경 없음. Task 3이 구현한다 |
| `claim_requests.slack_dm_ts` 완화 | `str \| None`. 생성 시점엔 아직 DM 전이라 비어 있다 |

**계약에 들어가지 않은 것 — 코드에도 넣지 않는다.**

| 항목 | 왜 빠졌나 |
|---|---|
| `claim_requests.settlement_run_id` | 이번 범위 밖. `UNPAID_NOTICE`가 어느 배치 결과인지는 여전히 열려 있다 |
| `AgentSession` / `Turn` 모델 | 이번 범위 밖. docs §2에는 `agent_sessions`가 있는데 코드에 모델이 없는 상태가 유지된다 |
| 청구자 `entity_id` → `receipt_id` 정정 | **각하됨.** `receipt_id`로 바꾸면 세션 단위가 청구 요청당에서 영수증당으로 바뀌어 `MISSING_CLAIM`(영수증이 없는 사유)을 표현할 수 없게 된다. 계약은 `claim_request_id`를 유지한다 |

`entity_id` 정정은 `agent_sessions` 한 곳에만 나오고 그 모델은 `src/schemas/`에 아직 없으므로, **②에서 고칠 코드가 애초에 없다.** 각하 여부와 무관하게 ② 범위는 위 3건(코드에 반영되는 것 기준)이다.

## 사전 확인된 사실

계획을 쓰기 전 레포에서 확인한 것들이다. 구현자가 다시 확인할 필요 없다.

| 사실 | 근거 |
|---|---|
| ULID 생성기가 레포에 없다 | `ulid` grep 결과 0건. A가 처음 도입한다 (Task 3) |
| 테스트 하네스가 없다 | `pyproject.toml`에 dev 의존성 없음, `tests/`에 fixture만 (Task 2가 세운다) |
| `record_audit_log`는 keyword-only | `src/guards/audit.py:10` |
| Firestore 클라이언트는 `payouts/store.py:17`의 `get_client()` 하나뿐 | 재사용한다. 두 번 만들지 않는다 |
| `seed_firestore.py`는 Pydantic 검증을 안 한다 | raw dict를 그대로 쓴다. 계약 위반 데이터가 조용히 들어간다 |
| 로컬 `main` = `origin/main` = `1fe0e42` | docs submodule 포인터는 `3f8b8eb`(의도적으로 안 올림) |

**docs submodule 주의.** 최신 계약은 payflow-docs `origin/main`(`88a6031`)에 있고 이 레포의 포인터(`3f8b8eb`)는 그보다 뒤처져 있다. 판단은 **`88a6031` 기준**으로 한다. 포인터 갱신은 팀 공용 결정이라 이 계획에 넣지 않는다.

## File Structure

| 파일 | 책임 | 태스크 |
|---|---|---|
| `src/schemas/models.py` (수정) | v0.4.0 계약 반영 | 1② |
| `src/ingest/signature.py` (신규) | Slack v0 서명 검증. raw body 기준. `/slack/events`와 `/slack/interactions`가 공유한다 | 2 |
| `src/ingest/store.py` (신규) | A 소유 Firestore 창구. `recipients` 조회, `receipts` 생성(dedup 트랜잭션) | 3 |
| `src/ingest/enqueue.py` (신규) | 파싱 태스크 enqueue **경계**. C의 공용 모듈이 오기 전까지 `QueueNotConfigured`를 던지는 자리표시자 | 5 |
| `src/ingest/routes.py` (신규) | `POST /slack/events` 핸들러. 위 셋을 조립만 한다 | 5 |
| `src/main.py` (수정) | ingest 라우터 등록 1줄 | 5 |
| `tests/conftest.py` (신규) | 환경변수 스텁, `--snapshot-update` 옵션 | 2 |
| `tests/ingest/test_signature.py` (신규) | 서명 검증 단위 테스트 | 2 |
| `tests/ingest/test_store.py` (신규) | dedup 트랜잭션 테스트 | 3 |
| `tests/ingest/test_slack_events.py` (신규) | 라우트 통합 테스트 + 3초 예산 측정 | 5 |
| `tests/test_openapi_snapshot.py` (신규) | §6 계약 스냅샷 | 6 |

## Task 4 보류 — enqueue 일반화 요청

원래 계획은 `payouts/tasks_queue.py`(C 소유, `/tasks/execute-payout` URL이 박혀 있음)를 `src/ingest/tasks_queue.py`로 30줄 복제하는 것이었다. **보류한다.** C에게 `enqueue_task(path, payload)` 형태로 일반화해 `src/shared/`로 빼달라고 요청해 둔 상태고, 답이 오면 그 모듈을 쓴다.

그동안 Task 5는 `src/ingest/enqueue.py`라는 **얇은 경계 하나**를 두고 진행한다. 이 파일은 지금 `QueueNotConfigured`를 던지기만 한다. 라우트는 이미 이 예외를 잡아 200으로 ack하고 감사 로그를 남기도록 설계돼 있으므로 — 큐 문제로 Slack 재전송을 유발하지 않는다 — 인입 경로는 이 상태로도 끝까지 동작하고 테스트된다. C의 모듈이 도착하면 이 파일의 본문 3줄만 바꾸면 된다.

**`src/shared/`는 C가 만든다. A가 먼저 만들지 않는다.**

---

## Task 1 ②: 스키마 계약 반영 (src/schemas)

payflow-docs `88a6031`을 Pydantic 모델에 옮긴다. 계약에 없는 것은 넣지 않는다.

**Files:**
- Modify: `src/schemas/models.py`

**Interfaces:**
- Produces: `Receipt.slack_file_id: str | None`, `Receipt.image_gcs_uri | raw_text_gcs_uri | currency | category_source | parse_signals` 전부 nullable, `ClaimRequest.slack_dm_ts: str | None`

- [ ] **Step 1: 브랜치를 판다**

```bash
git checkout -b feat/slack-ingest
```

`src/schemas/`는 공유 디렉터리다. 돈 경로는 아니지만 세 레포가 따라오는 변경이라 main에 직접 밀지 않는다.

- [ ] **Step 2: `Receipt`를 고친다**

`src/schemas/models.py`의 `Receipt`를 교체한다. 필드 순서는 docs §2 표 순서를 그대로 따른다.

```python
class Receipt(BaseModel):
    receipt_id: str
    recipient_id: str
    # 아래 다섯은 전부 파싱 파이프라인이 채운다. RECEIVED(Slack 인입 완료, 파싱 전)
    # 상태에서는 없다 — 필수로 두면 그 상태의 문서를 애초에 만들 수 없다.
    image_gcs_uri: str | None = None
    raw_text_gcs_uri: str | None = None
    # slack_file_id가 Slack 재전송 dedup 키다. slack_channel_id + slack_message_ts
    # 조합은 키가 못 된다 — 한 메시지에 이미지를 여러 장 붙이면 ts가 같다.
    slack_file_id: str | None = None
    slack_channel_id: str | None = None
    slack_message_ts: str | None = None
    merchant_name: str | None = None
    transaction_date: date | None = None
    parsed_amount_minor: int | None = None
    currency: str | None = None
    account_category_code: AccountCategory | None = None
    category_source: CategorySource | None = None
    parse_signals: ParseSignals | None = None
    llm_confidence: float | None = None
    verified_at: datetime | None = None
    verification_signals: VerificationSignals | None = None
    status: ReceiptStatus
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 3: `ClaimRequest.slack_dm_ts`를 완화한다**

```python
    slack_dm_ts: str | None = None
```

주석을 붙인다:

```python
    # 값은 chat.postMessage 응답에서 나온다. 문서를 먼저 만들어 멱등키를 확보하고
    # DM을 보낸 뒤 채운다 — 필수면 재시도 때 같은 DM이 두 번 나간다.
    slack_dm_ts: str | None = None
```

- [ ] **Step 4: 계약과 일치하는지 확인한다**

임포트와 필드 구성을 확인한다. 테스트 하네스는 Task 2가 세우므로 여기서는 인터프리터로 본다.

Run:
```bash
uv run python -c "
from datetime import UTC, datetime
from src.schemas.models import ClaimRequest, Receipt
now = datetime.now(UTC)
r = Receipt(receipt_id='rct_x', recipient_id='rcp_x', slack_file_id='F01ABCDEF',
            slack_channel_id='C01ABCDEF', slack_message_ts='1755500000.000100',
            status='RECEIVED', created_at=now, updated_at=now)
assert r.image_gcs_uri is None and r.currency is None and r.parse_signals is None
c = ClaimRequest(claim_request_id='crq_x', recipient_id='rcp_x', receipt_id='rct_x',
                 reason='PARSE_FAILED', expires_at=now, status='PENDING',
                 created_at=now, updated_at=now)
assert c.slack_dm_ts is None
print('ok')
"
```
Expected: `ok`

- [ ] **Step 5: 기존 fixture 8종이 그대로 통과하는지 확인한다**

전부 완화 방향이라 깨질 이유가 없지만, 확인 없이 넘어가지 않는다.

Run:
```bash
uv run python -c "
import json, pathlib
from src.schemas.models import Receipt
for path in sorted(pathlib.Path('tests/fixtures').glob('*.json')):
    for raw in json.loads(path.read_text(encoding='utf-8')).get('receipts', []):
        Receipt.model_validate(raw)
    print('ok', path.name)
"
```
Expected: 모든 fixture에 `ok`

- [ ] **Step 6: 커밋**

```bash
git add src/schemas/models.py
git commit -m "$(cat <<'EOF'
schema: receipts 인입 시점 필드 + slack_dm_ts nullable 완화

docs rules/schema-contract.md 88a6031을 Pydantic 모델에 반영한다.

RECEIVED("Slack 인입 완료, 파싱 전")인데 파싱 결과 필드 5개가 필수라
그 상태의 유효한 문서를 만들 수 없었다.

- Receipt.slack_file_id 신설 — Slack 재전송 dedup 키. slack_channel_id +
  slack_message_ts 조합은 한 메시지 다중 첨부에서 ts가 같아 키가 못 된다
- Receipt의 image_gcs_uri / raw_text_gcs_uri / currency / category_source /
  parse_signals 를 nullable로 완화 — 전부 파싱 파이프라인이 채우는 값이다
- ClaimRequest.slack_dm_ts 를 nullable로 완화 — 값은 chat.postMessage 응답에서
  나온다. 필수면 "문서 먼저, DM 나중" 순서가 불가능해 재시도 때 DM이 두 번 나간다

Firestore는 스키마리스라 마이그레이션 없음. 전부 완화 방향이라 기존 fixture
8종은 그대로 통과한다(확인함). receipts.currency가 nullable이 되므로 B의
결정론적 매칭에 영향이 있다.

affects: agent(핀 갱신), web(openapi-typescript 재생성)
EOF
)"
```

**태그(`v0.4.0`)와 전원 통보는 사용자가 한다.** 여기서 끊지 않는다.

---

## Task 2: Slack 서명 검증

테스트 하네스를 여기서 세운다. 이 태스크가 pytest를 처음 필요로 한다.

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`, `tests/ingest/__init__.py` (둘 다 빈 파일)
- Create: `tests/conftest.py`
- Create: `src/ingest/signature.py`
- Create: `tests/ingest/test_signature.py`

**Interfaces:**
- Produces: `verify_slack_signature(raw_body: bytes, timestamp: str, signature: str) -> None`, `SignatureError`

- [ ] **Step 1: 테스트 하네스를 세운다**

`pyproject.toml`의 `dependencies` 리스트는 건드리지 않고, 파일 끝에 블록 두 개를 추가한다.

```toml
[dependency-groups]
dev = [
    "pytest>=8.3",
    "httpx>=0.28",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`tests/__init__.py`, `tests/ingest/__init__.py`를 빈 파일로 만든다.

`tests/conftest.py`:

```python
"""테스트 전역 환경변수 스텁. 실제 GCP·Slack에 붙지 않는다."""

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("GCP_PROJECT", "payflow-test")
    monkeypatch.setenv("FIRESTORE_DATABASE", "development")
    monkeypatch.setenv("CLOUD_TASKS_LOCATION", "asia-northeast3")
    monkeypatch.setenv("OIDC_AUDIENCE", "https://api.test.invalid")
    monkeypatch.delenv("CLOUD_TASKS_QUEUE", raising=False)
    yield


def pytest_addoption(parser):
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="OpenAPI 스냅샷을 현재 스키마로 덮어쓴다 (schema: 커밋과 함께 쓴다)",
    )
```

Run: `uv sync`

- [ ] **Step 2: 실패하는 테스트를 쓴다**

`tests/ingest/test_signature.py`:

```python
"""schema-contract.md §10 — Slack 서명 검증."""

import hashlib
import hmac
import time

import pytest

from src.ingest.signature import SignatureError, verify_slack_signature

SECRET = "test-signing-secret"


def _sign(raw_body: bytes, timestamp: str) -> str:
    basestring = b"v0:" + timestamp.encode() + b":" + raw_body
    digest = hmac.new(SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_valid_signature_passes():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    verify_slack_signature(body, ts, _sign(body, ts))  # 예외가 없으면 통과


def test_tampered_body_fails():
    ts = str(int(time.time()))
    signature = _sign(b'{"amount":1000}', ts)
    with pytest.raises(SignatureError, match="signature mismatch"):
        verify_slack_signature(b'{"amount":9999999}', ts, signature)


def test_stale_timestamp_fails():
    """재전송 공격 방어. 5분을 넘긴 요청은 서명이 맞아도 거부한다."""
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()) - 600)
    with pytest.raises(SignatureError, match="skew"):
        verify_slack_signature(body, ts, _sign(body, ts))


def test_future_timestamp_fails():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()) + 600)
    with pytest.raises(SignatureError, match="skew"):
        verify_slack_signature(body, ts, _sign(body, ts))


def test_missing_headers_fail():
    with pytest.raises(SignatureError, match="missing"):
        verify_slack_signature(b"{}", "", "")


def test_malformed_timestamp_fails():
    with pytest.raises(SignatureError, match="malformed"):
        verify_slack_signature(b"{}", "not-a-number", "v0=deadbeef")
```

- [ ] **Step 3: 테스트가 실패하는 걸 확인한다**

Run: `uv run pytest tests/ingest/test_signature.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ingest.signature'`

- [ ] **Step 4: 구현한다**

`src/ingest/signature.py`:

```python
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
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("signature mismatch")
```

- [ ] **Step 5: 테스트가 통과하는 걸 확인한다**

Run: `uv run pytest tests/ingest/test_signature.py -v`
Expected: PASS 6건

- [ ] **Step 6: 커밋**

```bash
git add pyproject.toml uv.lock src/ingest/signature.py tests/
git commit -m "feat: Slack v0 서명 검증 (raw body 기준, 5분 skew 제한)

pytest·httpx 개발 의존성과 tests/conftest.py 하네스를 함께 세운다."
```

---

## Task 3: Firestore 인입 창구

`slack_file_id` dedup이 이 태스크의 전부다. 여기가 새면 파싱이 두 번 돌고 `claims`가 두 건 생겨 이중 지급으로 이어진다. docs `88a6031`이 "중복 검사와 생성은 하나의 트랜잭션 안에서"를 계약으로 못박았다.

**Files:**
- Modify: `pyproject.toml` (`python-ulid` 런타임 의존성)
- Create: `src/ingest/store.py`
- Create: `tests/ingest/test_store.py`

**Interfaces:**
- Consumes: `src.payouts.store.get_client` (읽기만 — C 소유 파일을 수정하지 않는다)
- Produces: `find_recipient_by_slack_user(slack_user_id: str) -> dict | None`, `create_receipt_if_absent(*, recipient_id: str, slack_file_id: str, slack_channel_id: str, slack_message_ts: str) -> tuple[str, bool]` — `(receipt_id, created)`. `created=False`면 Slack 재전송이다

- [ ] **Step 1: 의존성을 추가한다**

`pyproject.toml`의 `dependencies` 마지막에 추가:

```toml
    "python-ulid>=2.7",
```

Run: `uv sync`

레포에 ULID 생성기가 아직 없다 — A가 처음 도입한다. `schema-contract.md` §3이 ID 체계를 ULID로 못박고 있다.

- [ ] **Step 2: 실패하는 테스트를 쓴다**

`tests/ingest/test_store.py`:

```python
"""schema-contract.md §2 — receipts 생성과 Slack 재전송 dedup."""

import pytest

from src.ingest import store


class FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = True

    def to_dict(self):
        return dict(self._data)


class FakeQuery:
    """where().limit().stream() 체인만 흉내낸다."""

    def __init__(self, docs, field=None, value=None, limit=None):
        self._docs, self._field, self._value, self._limit = docs, field, value, limit

    def where(self, filter=None):
        return FakeQuery(self._docs, filter.field_path, filter.value, self._limit)

    def limit(self, n):
        return FakeQuery(self._docs, self._field, self._value, n)

    def stream(self, transaction=None):
        hits = [d for d in self._docs if d.to_dict().get(self._field) == self._value]
        return iter(hits[: self._limit] if self._limit else hits)


class FakeDocRef:
    def __init__(self, store_dict, doc_id):
        self._store, self.id = store_dict, doc_id

    def set(self, data):
        self._store[self.id] = data


class FakeCollection:
    def __init__(self, store_dict):
        self._store = store_dict

    def where(self, filter=None):
        docs = [FakeDoc(k, v) for k, v in self._store.items()]
        return FakeQuery(docs).where(filter=filter)

    def document(self, doc_id):
        return FakeDocRef(self._store, doc_id)


class FakeTransaction:
    """Firestore 트랜잭션의 set만 흉내낸다. 커밋은 즉시 반영된다."""

    def set(self, ref, data):
        ref.set(data)


class FakeClient:
    def __init__(self):
        self.data = {"recipients": {}, "receipts": {}}

    def collection(self, name):
        return FakeCollection(self.data.setdefault(name, {}))


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(store, "get_client", lambda: client)
    # 트랜잭션 래퍼만 갈아끼운다 — 실제 Firestore 없이 콜백 본문을 돌린다.
    monkeypatch.setattr(
        store, "_run_in_transaction", lambda fn: fn(FakeTransaction())
    )
    return client


def test_finds_recipient_by_slack_user(fake_client):
    fake_client.data["recipients"]["rcp_1"] = {
        "recipient_id": "rcp_1",
        "slack_user_id": "U01ABCDEF",
    }
    assert store.find_recipient_by_slack_user("U01ABCDEF")["recipient_id"] == "rcp_1"


def test_unknown_slack_user_returns_none(fake_client):
    assert store.find_recipient_by_slack_user("U_NOBODY") is None


def test_creates_receipt_in_received_status(fake_client):
    receipt_id, created = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F01ABCDEF",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    assert created is True
    assert receipt_id.startswith("rct_")
    doc = fake_client.data["receipts"][receipt_id]
    assert doc["status"] == "RECEIVED"
    assert doc["slack_file_id"] == "F01ABCDEF"
    # 파싱 파이프라인 몫은 자리조차 만들지 않는다 — 만들어 두면 "채워졌는지"를
    # 구분할 수 없어진다.
    assert "image_gcs_uri" not in doc
    assert "parse_signals" not in doc


def test_slack_retry_does_not_create_second_receipt(fake_client):
    """Slack은 ack가 3초를 넘기면 같은 이벤트를 최대 3회 재전송한다."""
    first_id, first_created = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F01ABCDEF",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    second_id, second_created = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F01ABCDEF",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    assert first_created is True
    assert second_created is False
    assert second_id == first_id
    assert len(fake_client.data["receipts"]) == 1


def test_different_files_in_same_message_create_two_receipts(fake_client):
    """한 메시지에 이미지 2장이면 message_ts가 같다. file_id로만 갈린다 —
    이게 channel_id + message_ts를 dedup 키로 못 쓰는 이유다."""
    a, _ = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F_AAA",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    b, _ = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F_BBB",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    assert a != b
    assert len(fake_client.data["receipts"]) == 2


def test_created_receipt_validates_against_contract(fake_client):
    """schema-contract.md §2 — 인입이 쓴 문서가 Receipt로 검증돼야 한다."""
    from src.schemas.models import Receipt

    receipt_id, _ = store.create_receipt_if_absent(
        recipient_id="rcp_1",
        slack_file_id="F01ABCDEF",
        slack_channel_id="C01ABCDEF",
        slack_message_ts="1755500000.000100",
    )
    Receipt.model_validate(fake_client.data["receipts"][receipt_id])
```

- [ ] **Step 3: 테스트가 실패하는 걸 확인한다**

Run: `uv run pytest tests/ingest/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ingest.store'`

- [ ] **Step 4: 구현한다**

`src/ingest/store.py`:

```python
"""schema-contract.md §2 — Slack 인입이 쓰는 Firestore 창구(A 소유).

payouts/store.py(C 소유)와 컬렉션이 겹치지 않는다. `get_client`만 재사용한다 —
Firestore 클라이언트를 두 번 만들 이유가 없고, C 파일은 읽기만 한다.

**dedup이 이 모듈의 핵심이다.** Slack은 ack가 3초를 넘기면 같은 이벤트를 최대 3회
재전송하고 Cloud Tasks도 재시도한다. receipts 문서가 두 개 생기면 파싱이 두 번 돌고
claims가 두 건 생겨 이중 지급으로 이어진다. 그래서 계약(§2)이 "slack_file_id 중복
검사와 receipts 생성은 하나의 Firestore 트랜잭션 안에서 한다"를 못박고 있다.
"""

from datetime import UTC, datetime

from google.cloud.firestore_v1.base_query import FieldFilter
from ulid import ULID

from ..payouts.store import get_client


def _run_in_transaction(fn):
    """트랜잭션 실행 경계. 테스트가 이 함수만 갈아끼워 실제 Firestore 없이
    콜백 본문을 돌린다."""
    client = get_client()
    return client.run_transaction(fn)


def find_recipient_by_slack_user(slack_user_id: str) -> dict | None:
    """schema-contract.md §2 — recipients의 Slack ID 매핑 조회는 A 소유다.
    단일 동등 필터라 복합 색인이 필요 없다."""
    docs = (
        get_client()
        .collection("recipients")
        .where(filter=FieldFilter("slack_user_id", "==", slack_user_id))
        .limit(1)
        .stream()
    )
    doc = next(iter(docs), None)
    return doc.to_dict() if doc else None


def create_receipt_if_absent(
    *,
    recipient_id: str,
    slack_file_id: str,
    slack_channel_id: str,
    slack_message_ts: str,
) -> tuple[str, bool]:
    """(receipt_id, created)를 돌려준다. created=False면 Slack 재전송이다.

    문서 ID는 §3대로 receipt_id(`rct_{ulid}`)를 쓴다 — fixture·seed_firestore.py와
    같은 규칙이라 파싱 태스크가 조회로 우회할 필요가 없다. dedup은 문서 ID가 아니라
    slack_file_id 조회로 하되, 조회와 생성을 한 트랜잭션에 넣어 원자성을 확보한다.
    """

    def _txn(transaction):
        existing = (
            get_client()
            .collection("receipts")
            .where(filter=FieldFilter("slack_file_id", "==", slack_file_id))
            .limit(1)
            .stream(transaction=transaction)
        )
        found = next(iter(existing), None)
        if found is not None:
            return found.to_dict()["receipt_id"], False

        receipt_id = f"rct_{ULID()}"
        now = datetime.now(UTC)
        # 파싱 파이프라인 몫(image_gcs_uri·currency·parse_signals 등)은 쓰지 않는다.
        # 계약상 전부 nullable이고, 여기서 자리만 만들어두면 "채워졌는지"를
        # 구분할 수 없어진다.
        transaction.set(
            get_client().collection("receipts").document(receipt_id),
            {
                "receipt_id": receipt_id,
                "recipient_id": recipient_id,
                "slack_file_id": slack_file_id,
                "slack_channel_id": slack_channel_id,
                "slack_message_ts": slack_message_ts,
                "status": "RECEIVED",
                "created_at": now,
                "updated_at": now,
            },
        )
        return receipt_id, True

    return _run_in_transaction(_txn)
```

`client.run_transaction(fn)`이 콜백 첫 인자로 transaction 객체를 넘긴다. 실제 Firestore에서 이 시그니처가 다르면 `@firestore.transactional` 데코레이터 형태로 바꾸되, `_run_in_transaction` **한 곳만** 고친다 — 테스트 seam이 거기다.

- [ ] **Step 5: 테스트가 통과하는 걸 확인한다**

Run: `uv run pytest tests/ingest/test_store.py -v`
Expected: PASS 6건

- [ ] **Step 6: 커밋**

```bash
git add pyproject.toml uv.lock src/ingest/store.py tests/ingest/test_store.py
git commit -m "feat: receipts 인입 창구 — slack_file_id dedup을 트랜잭션으로

schema-contract.md 88a6031의 '중복 검사와 생성은 하나의 트랜잭션 안에서'를 구현한다.
ULID 생성기가 레포에 없어 python-ulid를 함께 도입한다."
```

---

## Task 5: `POST /slack/events`

앞의 둘을 조립한다. 이 라우트는 분기와 순서만 담당하고 로직을 갖지 않는다.

**Files:**
- Create: `src/ingest/enqueue.py`
- Create: `src/ingest/routes.py`
- Modify: `src/main.py`
- Create: `tests/ingest/test_slack_events.py`

**Interfaces:**
- Consumes: `verify_slack_signature`, `SignatureError` (Task 2) · `find_recipient_by_slack_user`, `create_receipt_if_absent` (Task 3) · `src.guards.audit.record_audit_log` (읽기)
- Produces: `router` (APIRouter), `enqueue_parse_receipt(receipt_id: str) -> None`, `QueueNotConfigured`

- [ ] **Step 1: enqueue 경계를 만든다**

Task 4가 보류이므로 자리표시자를 둔다. C의 `src/shared/enqueue_task(path, payload)`가 도착하면 본문만 바꾼다.

`src/ingest/enqueue.py`:

```python
"""schema-contract.md §10 — 파싱 태스크 enqueue 경계 (A 소유).

**아직 큐에 넣지 않는다.** payouts/tasks_queue.py는 /tasks/execute-payout URL이
박혀 있고 C 소유라 고칠 수 없어서, C에게 enqueue_task(path, payload) 형태로
일반화해 src/shared/로 빼달라고 요청해 둔 상태다. 도착하면 이 파일의 함수 본문만
그 호출로 바꾼다 — 라우트와 테스트는 손대지 않는다.

그때까지는 QueueNotConfigured를 던진다. 라우트가 이 예외를 잡아 200으로 ack하고
감사 로그를 남기므로, 큐가 없다고 Slack 재전송을 유발하지는 않는다. receipts
문서는 이미 남아 있어 수동 재개가 가능하다.
"""


class QueueNotConfigured(RuntimeError):
    pass


def enqueue_parse_receipt(receipt_id: str) -> None:
    raise QueueNotConfigured(
        "공용 enqueue 모듈(src/shared/) 미도착 — POST /tasks/parse-receipt를 "
        f"직접 호출해 시뮬레이션한다. receipt_id={receipt_id}"
    )
```

- [ ] **Step 2: 실패하는 테스트를 쓴다**

`tests/ingest/test_slack_events.py`:

```python
"""schema-contract.md §10 — POST /slack/events.

서명 검증 → Firestore raw 저장 → enqueue → 200. architecture.md §비동기가
목표를 0.5s로 두고 Slack 제한이 3초다.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.ingest import routes
from src.main import app

SECRET = "test-signing-secret"


def _post(client, payload: dict, *, timestamp: str | None = None, secret: str = SECRET):
    raw = json.dumps(payload).encode()
    ts = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode(), b"v0:" + ts.encode() + b":" + raw, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/slack/events",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": f"v0={digest}",
        },
    )


def _file_message(file_ids: list[str]) -> dict:
    return {
        "type": "event_callback",
        "event_id": "Ev01ABCDEF",
        "event": {
            "type": "message",
            "user": "U01ABCDEF",
            "channel": "C01ABCDEF",
            "ts": "1755500000.000100",
            "files": [{"id": f, "mimetype": "image/jpeg"} for f in file_ids],
        },
    }


@pytest.fixture
def client(monkeypatch):
    calls = {"created": [], "enqueued": []}

    def fake_create(*, recipient_id, slack_file_id, slack_channel_id, slack_message_ts):
        seen = [c["slack_file_id"] for c in calls["created"]]
        if slack_file_id in seen:
            return f"rct_{slack_file_id}", False
        calls["created"].append(
            {"recipient_id": recipient_id, "slack_file_id": slack_file_id}
        )
        return f"rct_{slack_file_id}", True

    monkeypatch.setattr(
        routes,
        "find_recipient_by_slack_user",
        lambda uid: {"recipient_id": "rcp_1"} if uid == "U01ABCDEF" else None,
    )
    monkeypatch.setattr(routes, "create_receipt_if_absent", fake_create)
    monkeypatch.setattr(
        routes, "enqueue_parse_receipt", lambda rid: calls["enqueued"].append(rid)
    )
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)

    test_client = TestClient(app)
    test_client.calls = calls
    return test_client


def test_url_verification_echoes_challenge(client):
    """Slack 앱 설정에서 Event Subscriptions URL을 등록할 때 오는 핸드셰이크.
    이게 없으면 앱 등록 자체가 안 된다."""
    response = _post(client, {"type": "url_verification", "challenge": "abc123xyz"})
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123xyz"}


def test_url_verification_still_requires_valid_signature(client):
    """핸드셰이크라고 서명 검증을 건너뛰지 않는다."""
    response = _post(
        client, {"type": "url_verification", "challenge": "abc"}, secret="wrong"
    )
    assert response.status_code == 401


def test_bad_signature_is_rejected(client):
    response = _post(client, _file_message(["F_AAA"]), secret="wrong-secret")
    assert response.status_code == 401
    assert client.calls["created"] == []


def test_stale_timestamp_is_rejected(client):
    stale = str(int(time.time()) - 600)
    response = _post(client, _file_message(["F_AAA"]), timestamp=stale)
    assert response.status_code == 401


def test_image_upload_creates_receipt_and_enqueues(client):
    response = _post(client, _file_message(["F_AAA"]))
    assert response.status_code == 200
    assert client.calls["created"] == [
        {"recipient_id": "rcp_1", "slack_file_id": "F_AAA"}
    ]
    assert client.calls["enqueued"] == ["rct_F_AAA"]


def test_two_files_in_one_message_create_two_receipts(client):
    response = _post(client, _file_message(["F_AAA", "F_BBB"]))
    assert response.status_code == 200
    assert len(client.calls["created"]) == 2
    assert client.calls["enqueued"] == ["rct_F_AAA", "rct_F_BBB"]


def test_slack_retry_does_not_duplicate_receipt(client):
    _post(client, _file_message(["F_AAA"]))
    response = _post(client, _file_message(["F_AAA"]))
    assert response.status_code == 200
    assert len(client.calls["created"]) == 1
    # 문서는 하나지만 enqueue는 다시 한다 — 파싱 태스크는 같은 receipt_id를
    # 덮어쓰므로 멱등이고, 앞 요청이 enqueue 직전에 죽었을 수 있다.
    assert client.calls["enqueued"] == ["rct_F_AAA", "rct_F_AAA"]


def test_unregistered_user_is_acked_without_receipt(client):
    payload = _file_message(["F_AAA"])
    payload["event"]["user"] = "U_NOBODY"
    response = _post(client, payload)
    assert response.status_code == 200
    assert client.calls["created"] == []


def test_message_without_files_is_ignored(client):
    payload = _file_message([])
    payload["event"]["text"] = "안녕하세요"
    response = _post(client, payload)
    assert response.status_code == 200
    assert client.calls["created"] == []


def test_bot_message_is_ignored(client):
    """자기가 보낸 메시지에 반응해 무한 루프를 만들지 않는다."""
    payload = _file_message(["F_AAA"])
    payload["event"]["bot_id"] = "B01ABCDEF"
    response = _post(client, payload)
    assert response.status_code == 200
    assert client.calls["created"] == []


def test_non_image_file_is_ignored(client):
    payload = _file_message(["F_AAA"])
    payload["event"]["files"] = [{"id": "F_AAA", "mimetype": "application/pdf"}]
    response = _post(client, payload)
    assert response.status_code == 200
    assert client.calls["created"] == []


def test_queue_not_configured_still_acks(client, monkeypatch):
    """3초 ack가 최우선이다. 큐 문제로 Slack 재전송을 유발하지 않는다 —
    receipts 문서는 이미 남았으므로 수동 재개가 가능하다."""
    from src.ingest.enqueue import QueueNotConfigured

    def boom(_):
        raise QueueNotConfigured("no queue")

    monkeypatch.setattr(routes, "enqueue_parse_receipt", boom)
    response = _post(client, _file_message(["F_AAA"]))
    assert response.status_code == 200
    assert len(client.calls["created"]) == 1


def test_firestore_contention_returns_503_not_500(client, monkeypatch):
    """트랜잭션 ABORTED가 SDK 기본 5회를 넘기면 ValueError가 올라온다.

    이건 enqueue 실패와 다르게 **receipts 문서가 안 남은** 상황이므로 200으로
    삼키면 영수증이 조용히 사라진다. 재전송을 받아야 맞다. 다만 스택 트레이스
    500이 아니라 명시적 503 + 감사 로그로 남긴다 — 원인 추적이 가능해야 한다.
    """
    audit = []

    def boom(**kwargs):
        raise ValueError("Failed to commit transaction in 5 attempts.")

    monkeypatch.setattr(routes, "create_receipt_if_absent", boom)
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit.append(kw))

    response = _post(client, _file_message(["F_AAA"]))
    assert response.status_code == 503
    assert any(a["action"] == "RECEIPT_INGEST_FAILED" for a in audit)


def test_round_trip_stays_within_slack_budget(client, monkeypatch):
    """서명검증 → Firestore 쓰기 → enqueue 왕복이 3초 안이어야 한다.

    Firestore·Cloud Tasks에 현실적인 지연을 주입해 잰다. fake가 즉시 반환하면
    아무것도 검증하지 못하므로 일부러 느리게 만든다. 이미지 3장짜리 메시지를
    쓰는 이유는 파일 수에 비례해 늘어나는 직렬 호출을 잡기 위해서다 — 여기서
    새는 설계가 실제 배포에서 3초를 넘긴다.

    절대적 보장은 아니다. 실측은 배포 후 Cloud Run 로그로 다시 확인한다.
    """
    FIRESTORE_LATENCY = 0.15
    ENQUEUE_LATENCY = 0.10

    real_create = routes.create_receipt_if_absent

    def slow_create(**kwargs):
        time.sleep(FIRESTORE_LATENCY)
        return real_create(**kwargs)

    def slow_enqueue(receipt_id):
        time.sleep(ENQUEUE_LATENCY)

    monkeypatch.setattr(routes, "create_receipt_if_absent", slow_create)
    monkeypatch.setattr(routes, "enqueue_parse_receipt", slow_enqueue)

    started = time.perf_counter()
    response = _post(client, _file_message(["F_AAA", "F_BBB", "F_CCC"]))
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 3.0, f"Slack 3초 제한 초과: {elapsed:.2f}s"
    # architecture.md §비동기의 목표는 0.5s다. 주입한 지연 합(0.75s)을 뺀
    # 라우트 자체 오버헤드가 그 안에 들어오는지 본다.
    injected = 3 * (FIRESTORE_LATENCY + ENQUEUE_LATENCY)
    assert elapsed - injected < 0.5, f"라우트 자체 오버헤드 과다: {elapsed - injected:.2f}s"
```

- [ ] **Step 3: 테스트가 실패하는 걸 확인한다**

Run: `uv run pytest tests/ingest/test_slack_events.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ingest.routes'`

- [ ] **Step 4: 구현한다**

`src/ingest/routes.py`:

```python
"""schema-contract.md §10 — POST /slack/events (A 소유).

architecture.md §비동기: 서명검증 → Firestore raw 저장 → enqueue → 200, 목표 0.5s.
**3초 안에 200을 돌려주는 게 이 라우트의 유일한 성능 요구다.** 파일 다운로드,
GCS 업로드, Gemini 호출은 전부 파싱 태스크 몫이다 — 여기서 하면 3초를 넘긴다.

Slack은 ack가 늦으면 같은 이벤트를 최대 3회 재전송한다. dedup은 store.py가
slack_file_id로 하고, 이 라우트는 재전송이어도 enqueue는 다시 한다 — 파싱 태스크는
같은 receipt_id를 덮어쓰므로 멱등이고, 앞 요청이 enqueue 직전에 죽었을 수 있다.
"""

from fastapi import APIRouter, HTTPException, Request

from ..guards.audit import record_audit_log
from .enqueue import QueueNotConfigured, enqueue_parse_receipt
from .signature import SignatureError, verify_slack_signature
from .store import create_receipt_if_absent, find_recipient_by_slack_user

router = APIRouter()

_IMAGE_MIMETYPES = {"image/jpeg", "image/png", "image/heic", "image/webp"}


@router.post("/slack/events")
async def slack_events(request: Request):
    # 서명은 raw body 기준이다. request.json()을 먼저 부르면 안 된다.
    raw_body = await request.body()
    try:
        verify_slack_signature(
            raw_body,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            request.headers.get("X-Slack-Signature", ""),
        )
    except SignatureError as e:
        raise HTTPException(status_code=401, detail=str(e))

    payload = await request.json()

    # 앱 설정에서 Event Subscriptions URL을 등록할 때 오는 핸드셰이크.
    # 이걸 에코하지 않으면 Slack 앱 등록 자체가 안 된다.
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    event = payload.get("event", {})
    if event.get("type") != "message" or event.get("bot_id"):
        return {"status": "ignored"}

    files = [f for f in event.get("files", []) if f.get("mimetype") in _IMAGE_MIMETYPES]
    if not files:
        return {"status": "ignored"}

    recipient = find_recipient_by_slack_user(event.get("user", ""))
    if recipient is None:
        # 조용히 버리지 않는다. 안내 DM은 재촉 루프(범위 밖)가 붙을 때 함께 단다.
        record_audit_log(
            actor="api/src/ingest",
            action="RECEIPT_INGEST_SKIPPED",
            reason=f"unregistered slack_user_id: {event.get('user')}",
        )
        return {"status": "ignored", "reason": "unregistered_user"}

    received = []
    for slack_file in files:
        try:
            receipt_id, created = create_receipt_if_absent(
                recipient_id=recipient["recipient_id"],
                slack_file_id=slack_file["id"],
                slack_channel_id=event["channel"],
                slack_message_ts=event["ts"],
            )
        except ValueError as e:
            # 트랜잭션 ABORTED가 SDK 재시도 상한(기본 5회)을 넘긴 경우.
            # enqueue 실패와 달리 receipts 문서가 남지 않았으므로 200으로 삼키면
            # 영수증이 조용히 사라진다. Slack 재전송을 받아야 맞다 — 다만 스택
            # 트레이스 500이 아니라 명시적 503으로 남긴다.
            record_audit_log(
                actor="api/src/ingest",
                action="RECEIPT_INGEST_FAILED",
                reason=f"firestore transaction failed: {e}",
            )
            raise HTTPException(status_code=503, detail="receipt store unavailable")
        if created:
            record_audit_log(
                actor="api/src/ingest",
                action="RECEIPT_RECEIVED",
                after={"receipt_id": receipt_id, "status": "RECEIVED"},
            )
        try:
            enqueue_parse_receipt(receipt_id)
        except QueueNotConfigured as e:
            # 3초 ack가 우선이다. 여기서 500을 내면 Slack이 재전송하는데 큐는
            # 여전히 없다. 문서는 남았으니 수동 재개가 가능하다.
            record_audit_log(
                actor="api/src/ingest",
                action="PARSE_ENQUEUE_FAILED",
                reason=str(e),
                after={"receipt_id": receipt_id},
            )
        received.append(receipt_id)

    return {"status": "ok", "receipt_ids": received}
```

`src/main.py`에 import를 추가한다(알파벳 순서에 맞춰 `guards`와 `payouts` 사이):

```python
from .ingest.routes import router as ingest_router  # noqa: E402
```

그리고 등록:

```python
app.include_router(ingest_router)
```

- [ ] **Step 5: 테스트가 통과하는 걸 확인한다**

Run: `uv run pytest tests/ingest/ -v`
Expected: PASS — 이 파일 14건 포함 전체 통과

- [ ] **Step 6: 3초 예산을 눈으로 확인한다**

Run: `uv run pytest tests/ingest/test_slack_events.py --durations=5 -q`
Expected: `test_round_trip_stays_within_slack_budget`만 0.75s 근처(주입한 지연), 나머지는 0.1s 미만. 다른 테스트가 느리면 라우트에 예상 못 한 동기 호출이 섞인 것이다.

- [ ] **Step 7: 커밋**

```bash
git add src/ingest/enqueue.py src/ingest/routes.py src/main.py tests/ingest/test_slack_events.py
git commit -m "feat: POST /slack/events — 서명 검증 후 receipts 생성하고 파싱 enqueue

url_verification 핸드셰이크 응답과 3초 예산 측정 테스트를 포함한다.
enqueue는 src/ingest/enqueue.py 경계로만 두고 실제 큐 호출은 C의 공용
모듈(src/shared/) 도착 후 붙인다."
```

---

## Task 6: OpenAPI 계약 스냅샷

`schema-contract.md` §6이 요구하는데 없다. 지금 세워두면 다음 스키마 변경이 조용히 지나가지 않는다.

**Files:**
- Create: `tests/test_openapi_snapshot.py`
- Create: `tests/openapi.snapshot.json`

`--snapshot-update` 옵션은 Task 2에서 `conftest.py`에 이미 넣었다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_openapi_snapshot.py`:

```python
"""schema-contract.md §6 계약 테스트.

스냅샷이 깨지면 스키마가 바뀐 것이다. 의도한 변경이면 스냅샷을 갱신하고
커밋 메시지에 `schema:` 접두사를 붙인다 — 다른 레포가 따라와야 한다는 신호다.

    uv run pytest tests/test_openapi_snapshot.py --snapshot-update
"""

import json
from pathlib import Path

from src.main import app

SNAPSHOT = Path(__file__).parent / "openapi.snapshot.json"


def test_openapi_snapshot(pytestconfig):
    current = app.openapi()
    if pytestconfig.getoption("--snapshot-update"):
        SNAPSHOT.write_text(
            json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return
    assert current == json.loads(SNAPSHOT.read_text(encoding="utf-8"))
```

- [ ] **Step 2: 테스트가 실패하는 걸 확인한다**

Run: `uv run pytest tests/test_openapi_snapshot.py -v`
Expected: FAIL — `FileNotFoundError: tests/openapi.snapshot.json`

- [ ] **Step 3: 스냅샷을 만든다**

Run: `uv run pytest tests/test_openapi_snapshot.py --snapshot-update -q`

생성된 `tests/openapi.snapshot.json`에 `/slack/events`가 들어 있는지 확인한다.

- [ ] **Step 4: 전체가 통과하는 걸 확인한다**

Run: `uv run pytest -v`
Expected: 전체 PASS

- [ ] **Step 5: 커밋하고 PR을 연다**

```bash
git add tests/
git commit -m "test: OpenAPI 계약 스냅샷 테스트 추가 (schema-contract.md §6)"
git push -u origin feat/slack-ingest
gh pr create --title "feat: Slack 영수증 인입 경로 (서명 검증 → raw 저장 → enqueue)" --body "$(cat <<'EOF'
## 범위

`POST /slack/events` — 서명 검증, 3초 내 ack, `receipts` 생성, 파싱 태스크 enqueue.
파싱·PII 마스킹·`claims` 생성은 들어 있지 않다.

## 스키마

payflow-docs `88a6031`을 그대로 반영한다. `src/schemas/`가 공유 디렉터리라 리뷰가 필요하다.

- `receipts.slack_file_id` 신설 (Slack 재전송 dedup 키)
- `receipts`의 파싱 필드 5개 nullable 완화 — `RECEIVED`(파싱 전) 문서를 만들 수 없었다
- `claim_requests.slack_dm_ts` nullable 완화

**`receipts.currency`가 nullable이 된다. B의 결정론적 매칭이 영향을 받는다.**

## 확인

- `uv run pytest` 전체 통과
- Slack 재전송 → `receipts` 1건 (`slack_file_id` dedup, 트랜잭션)
- 한 메시지 이미지 2장 → `receipts` 2건
- `url_verification` 챌린지 에코 (없으면 Slack 앱 등록 불가)
- 이미지 3장 + 지연 주입 왕복 3초 이내

## 아직 안 된 것

실제 Cloud Tasks enqueue는 `src/ingest/enqueue.py` 경계에서 `QueueNotConfigured`를
던진다. C의 공용 `enqueue_task(path, payload)`(`src/shared/`)가 도착하면 본문 3줄을
바꿔 붙인다. 그동안 인입은 200으로 ack하고 감사 로그를 남긴다.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## 이 계획에 넣지 않은 것

범위 밖이지만 계약상 열려 있어 나중에 막힐 것들이다. 잊지 않으려고 적어둔다.

| 항목 | 언제 |
|---|---|
| **`receipts.slack_file_id` 인덱스 실측** — 단일 필드라 Firestore 자동 인덱스로 커버되지만, 자동 인덱싱이 꺼져 있거나 예외 목록에 있으면 조회가 항상 0건을 돌려주고 **dedup이 조용히 무력화된다**. 실제 development DB에 재전송 시나리오를 한 번 돌려 확인해야 한다 | 배포 전 (필수) |
| **fake 테스트는 원자성을 검증하지 못한다** — `FakeTransaction.set`은 즉시 반영되고 락·ABORTED·재시도가 없다. 검사와 쓰기를 트랜잭션 밖으로 꺼내도 6건이 똑같이 통과한다. 계약 준수는 테스트가 아니라 **리뷰**가 게이트다 | 리뷰 시 상시 |
| 실제 Cloud Tasks enqueue — C의 `src/shared/enqueue_task(path, payload)` 대기 | C 응답 후 |
| `claim_requests.settlement_run_id` — `UNPAID_NOTICE`가 어느 배치 결과인지 담을 자리가 없다 | 지급 결과 통지 |
| `AgentSession` / `Turn` 모델 — docs §2에 `agent_sessions`가 있는데 코드에 모델이 없다 | agent 세션 작업 |
| 청구자 `entity_id` 문제 — `claim_request_id`는 인입 직후 호출 시점에 없고, `receipt_id`는 `MISSING_CLAIM`을 표현 못 한다. 양쪽 다 안 맞는 상태로 열려 있다 | agent 세션 작업 |
| 미등록 사용자 안내 DM (지금은 audit log만) | DM 발송 |
| `GCS_RECEIPTS_BUCKET` 환경변수 + `google-cloud-storage` 의존성 | 파싱 |
| agent 호출 시 OIDC audience는 `AGENT_SERVICE_URL` — §11에 명시 없음 | 청구자 에이전트 호출 |
| `claims` 문서 ID를 `receipt_id`로 (중복 청구 방지) | claims 생성 |
| `POST /tasks/notify-claimants` 신설 + C의 reconcile이 enqueue | 지급 결과 통지 |
| `/tasks/remind`가 `EXPIRED` 전이도 담당한다는 §10 명시 | 재촉 루프 |
| B가 `VERIFICATION_FAILED` claim_request를 쓴 뒤 `/tasks/remind`를 직접 enqueue (A에 폴러 없음) | B와 협의 |
| fixture 02·05에 `reason` 없음 → `09_*.json` 추가로 덮기 | 재촉 루프 테스트 |
| `agent_sessions.turns.content`가 원문 저장이라 §2 "Firestore는 마스킹 후" 규칙과 충돌 | 계약 정리 |
| docs submodule 포인터가 `3f8b8eb`로 뒤처짐 (최신 `88a6031`) | 팀 결정 |
