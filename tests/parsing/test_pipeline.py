"""schema-contract.md §2 — 파싱 파이프라인 조립 순서.

**이 스위트가 지키는 불변식 세 가지:**
1. PII 마스킹이 Firestore 쓰기보다 **앞**에 온다 (§2)
2. 파싱이 쓰는 status는 PARSED와 FAILED 둘뿐이다 — NEEDS_REQUERY는
   청구자 에이전트의 판단이지 코드의 판단이 아니다 (§2)
3. 일시적 실패는 상태를 바꾸지 않는다 — 멀쩡한 영수증에 FAILED를 찍으면
   재요청 DM이 잘못 나간다
"""

from datetime import date

import pytest

from src.parsing import pipeline
from src.parsing.models import ParsedReceipt
from src.parsing.slack_files import PermanentParseError, SlackFile, TransientParseError
from src.parsing.storage import LocalObjectStore
from src.schemas.enums import AccountCategory


class RecordingParser:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error
        self.calls = []

    def parse(self, *, image, mimetype, receipt_id):
        self.calls.append(receipt_id)
        if self._error:
            raise self._error
        return self._result


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """파이프라인 밖의 모든 경계를 가짜로 바꾼다. 남는 건 조립 순서뿐이다."""
    state = {
        "receipts": {
            "rct_1": {
                "receipt_id": "rct_1",
                "recipient_id": "rcp_1",
                "slack_file_id": "F01ABCDEF",
                "status": "RECEIVED",
            }
        },
        "updates": [],
        "audit": [],
        "enqueued": [],
    }

    monkeypatch.setattr(pipeline, "get_receipt", lambda rid: state["receipts"].get(rid))

    def fake_update(receipt_id, updates):
        state["updates"].append((receipt_id, updates))
        state["receipts"][receipt_id].update(updates)

    monkeypatch.setattr(pipeline, "update_receipt", fake_update)
    monkeypatch.setattr(
        pipeline,
        "download_slack_file",
        lambda file_id: SlackFile(data=b"\xff\xd8img", mimetype="image/jpeg", ext="jpg"),
    )
    monkeypatch.setattr(pipeline, "get_object_store", lambda: LocalObjectStore(tmp_path))
    monkeypatch.setattr(
        pipeline, "record_audit_log", lambda **kwargs: state["audit"].append(kwargs)
    )
    monkeypatch.setattr(
        pipeline, "enqueue_claimant_review", lambda rid: state["enqueued"].append(rid)
    )
    return state


def _install_parser(monkeypatch, parser):
    monkeypatch.setattr(pipeline, "get_parser", lambda: parser)
    return parser


def _clean_result(**overrides) -> ParsedReceipt:
    kwargs = {
        "merchant_name": "스타벅스 강남점 02-1234-5678",
        "transaction_date": date(2026, 8, 5),
        "amount_text": "45,000",
        "currency": "KRW",
        "account_category_code": AccountCategory.SUPPLIES,
        "confidence": 0.93,
        "raw_text": "스타벅스 강남점 02-1234-5678\n합계 45,000원\n2026-08-05",
    }
    kwargs.update(overrides)
    return ParsedReceipt(**kwargs)


# --- 골든 패스 ---

def test_writes_parsed_status_and_fields(monkeypatch, wired):
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))

    assert pipeline.parse_receipt("rct_1") == "PARSED"

    _, updates = wired["updates"][-1]
    assert updates["status"] == "PARSED"
    assert updates["parsed_amount_minor"] == 45000
    assert updates["currency"] == "KRW"
    assert updates["transaction_date"] == "2026-08-05"
    assert updates["account_category_code"] == "SUPPLIES"
    assert updates["category_source"] == "LLM_PARSE"
    assert updates["llm_confidence"] == 0.93
    assert updates["image_gcs_uri"].startswith("file://")
    assert updates["raw_text_gcs_uri"].startswith("file://")


def test_transaction_date_is_stored_as_yyyy_mm_dd_string(monkeypatch, wired):
    """§1 시각 — transaction_date는 유일한 예외다. Timestamp로 저장하면
    KST/UTC 경계에서 하루가 밀린다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    pipeline.parse_receipt("rct_1")
    _, updates = wired["updates"][-1]
    assert updates["transaction_date"] == "2026-08-05"
    assert isinstance(updates["transaction_date"], str)


def test_amount_is_integer_minor_unit(monkeypatch, wired):
    """USD 영수증의 ×100은 코드가 한다 (절대 규칙 3)."""
    _install_parser(
        monkeypatch, RecordingParser(result=_clean_result(amount_text="25.00", currency="USD"))
    )
    pipeline.parse_receipt("rct_1")
    _, updates = wired["updates"][-1]
    assert updates["parsed_amount_minor"] == 2500
    assert isinstance(updates["parsed_amount_minor"], int)


# --- ★ 불변식 1: 마스킹이 Firestore 쓰기보다 앞 ---

def test_merchant_name_is_masked_in_firestore_write(monkeypatch, wired):
    """이 테스트는 최종 값만 본다(updates[-1]) — "먼저 raw로 쓰고 masked로
    덮어쓴다"는 구현도 단독으로는 통과한다. 순서 보증(마스킹이 쓰기보다 앞)은
    아래 test_no_unmasked_value_appears_in_any_firestore_write가 전체 쓰기를
    훑어서 담당한다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    pipeline.parse_receipt("rct_1")

    _, updates = wired["updates"][-1]
    assert updates["merchant_name"] == "스타벅스 강남점 [PHONE]"
    assert "02-1234-5678" not in updates["merchant_name"]


def test_raw_text_reaches_object_store_unmasked(monkeypatch, wired, tmp_path):
    """원본은 GCS에만 (§2). 마스킹된 텍스트를 저장하면 청구자 에이전트가
    비신뢰 원문을 못 보고, 인젝션 시연 재료도 사라진다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    pipeline.parse_receipt("rct_1")

    stored = (tmp_path / "raw_text" / "rct_1.txt").read_text(encoding="utf-8")
    assert "02-1234-5678" in stored


def test_no_unmasked_value_appears_in_any_firestore_write(monkeypatch, wired):
    """구조 테스트 — 필드가 늘어나도 원문이 새지 않는지 통째로 훑는다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    pipeline.parse_receipt("rct_1")

    written = repr(wired["updates"])
    assert "02-1234-5678" not in written


def test_audit_reason_is_masked(monkeypatch, wired):
    """§2 audit_logs — reason에 들어가는 값은 PII 마스킹 이후다."""
    _install_parser(
        monkeypatch,
        RecordingParser(error=PermanentParseError("bad receipt from help@store.example.com")),
    )
    pipeline.parse_receipt("rct_1")

    reasons = [entry.get("reason", "") for entry in wired["audit"]]
    assert any("[EMAIL]" in reason for reason in reasons)
    assert not any("help@store.example.com" in reason for reason in reasons)


# --- ★ 불변식 2: 파싱은 PARSED와 FAILED만 쓴다 ---

def test_low_confidence_still_parses(monkeypatch, wired):
    """fixture 04 — confidence 0.42는 UNCLASSIFIED로 표현되지 상태로 표현되지 않는다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result(confidence=0.42)))

    assert pipeline.parse_receipt("rct_1") == "PARSED"
    _, updates = wired["updates"][-1]
    assert updates["status"] == "PARSED"
    assert updates["account_category_code"] == "UNCLASSIFIED"
    assert updates["category_source"] == "DETERMINISTIC_FALLBACK"


def test_unreadable_fields_still_parse(monkeypatch, wired):
    """fixture 02 — 금액·가맹점을 못 읽어도 파싱 호출 자체는 성공했다.
    NEEDS_REQUERY로 내리는 건 청구자 에이전트의 몫이다 (§2)."""
    _install_parser(
        monkeypatch,
        RecordingParser(
            result=_clean_result(merchant_name=None, amount_text=None, currency=None, confidence=None)
        ),
    )

    assert pipeline.parse_receipt("rct_1") == "PARSED"
    _, updates = wired["updates"][-1]
    assert updates["status"] == "PARSED"
    assert updates["parsed_amount_minor"] is None
    assert updates["llm_confidence"] is None


def test_pipeline_never_writes_needs_requery(monkeypatch, wired):
    """§2 — NEEDS_REQUERY의 판정 주체는 청구자 에이전트다. 코드가 쓰면
    감사 로그에서 판정 주체를 잃는다."""
    for parser in (
        RecordingParser(result=_clean_result(merchant_name=None, amount_text=None, currency=None)),
        RecordingParser(error=PermanentParseError("unreadable")),
    ):
        wired["receipts"]["rct_1"]["status"] = "RECEIVED"
        _install_parser(monkeypatch, parser)
        pipeline.parse_receipt("rct_1")

    written = {updates["status"] for _, updates in wired["updates"]}
    assert written <= {"PARSED", "FAILED"}, f"파싱이 쓰면 안 되는 상태를 썼다: {written}"


def test_permanent_failure_writes_failed(monkeypatch, wired):
    _install_parser(monkeypatch, RecordingParser(error=PermanentParseError("file_not_found")))

    assert pipeline.parse_receipt("rct_1") == "FAILED"
    _, updates = wired["updates"][-1]
    assert updates["status"] == "FAILED"


def test_missing_slack_file_id_writes_failed_without_calling_parser(monkeypatch, wired):
    """F1 회귀 — slack_file_id는 계약상 nullable이다(Slack 외 경로로 만들어진
    영수증). 대괄호로 읽으면 KeyError가 밖으로 새 나가 영수증이 RECEIVED에
    영원히 남는다."""
    wired["receipts"]["rct_1"].pop("slack_file_id")
    parser = _install_parser(monkeypatch, RecordingParser(result=_clean_result()))

    assert pipeline.parse_receipt("rct_1") == "FAILED"
    assert parser.calls == []
    _, updates = wired["updates"][-1]
    assert updates["status"] == "FAILED"


# --- ★ 불변식 3: 일시적 실패는 상태를 안 바꾼다 ---

def test_transient_failure_leaves_status_untouched(monkeypatch, wired):
    _install_parser(monkeypatch, RecordingParser(error=TransientParseError("vertex 503")))

    with pytest.raises(TransientParseError):
        pipeline.parse_receipt("rct_1")

    assert wired["updates"] == []
    assert wired["receipts"]["rct_1"]["status"] == "RECEIVED"
    assert wired["enqueued"] == []


# --- 멱등성 ---

def test_already_parsed_receipt_is_skipped(monkeypatch, wired):
    """Cloud Tasks 재시도. 두 번째 호출이 파서를 다시 부르면 Gemini 비용이
    두 배가 되고 image_gcs_uri가 덮어써진다."""
    wired["receipts"]["rct_1"]["status"] = "PARSED"
    parser = _install_parser(monkeypatch, RecordingParser(result=_clean_result()))

    assert pipeline.parse_receipt("rct_1") == "SKIPPED"
    assert parser.calls == []
    assert wired["updates"] == []
    assert wired["enqueued"] == []


def test_missing_receipt_raises(monkeypatch, wired):
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    with pytest.raises(pipeline.ReceiptNotFound):
        pipeline.parse_receipt("rct_nope")


# --- 청구자 에이전트 enqueue ---

def test_enqueues_claimant_review_only_on_parsed(monkeypatch, wired):
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    pipeline.parse_receipt("rct_1")
    assert wired["enqueued"] == ["rct_1"]


def test_does_not_enqueue_on_failure(monkeypatch, wired):
    """FAILED는 재촉 루프 몫이지 청구자 에이전트 검토 대상이 아니다."""
    _install_parser(monkeypatch, RecordingParser(error=PermanentParseError("unreadable")))
    pipeline.parse_receipt("rct_1")
    assert wired["enqueued"] == []


def test_enqueue_failure_does_not_undo_parsed(monkeypatch, wired):
    """큐가 없어도 파싱 결과는 남아야 한다. ingest/routes.py가 같은 판단을 한다."""
    from src.guards.tasks import QueueNotConfigured

    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))

    def boom(receipt_id):
        raise QueueNotConfigured("CLOUD_TASKS_QUEUE not configured")

    monkeypatch.setattr(pipeline, "enqueue_claimant_review", boom)

    assert pipeline.parse_receipt("rct_1") == "PARSED"
    assert wired["receipts"]["rct_1"]["status"] == "PARSED"
    assert any(entry["action"] == "CLAIMANT_ENQUEUE_FAILED" for entry in wired["audit"])


def test_enqueue_failure_other_than_queue_not_configured_does_not_undo_parsed(monkeypatch, wired):
    """F2 회귀 — 큐가 설정된 상태에서도 enqueue_task는 os.environ 대괄호 접근
    (KeyError)이나 Cloud Tasks 네트워크 호출(Google API 예외)로 던질 수 있다.
    QueueNotConfigured만 잡으면 이런 실패가 500으로 새 나가고, 재시도해도
    status != RECEIVED라 SKIPPED로 빠져 청구자 에이전트 호출이 영영 사라진다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))

    def boom(receipt_id):
        raise RuntimeError("Cloud Tasks unavailable")

    monkeypatch.setattr(pipeline, "enqueue_claimant_review", boom)

    assert pipeline.parse_receipt("rct_1") == "PARSED"
    assert wired["receipts"]["rct_1"]["status"] == "PARSED"
    assert any(entry["action"] == "CLAIMANT_ENQUEUE_FAILED" for entry in wired["audit"])
