"""schema-contract.md §2 — 파싱 파이프라인 조립 순서.

**이 스위트가 지키는 불변식 세 가지:**
1. PII 마스킹이 Firestore 쓰기보다 **앞**에 온다 (§2)
2. 파싱이 쓰는 status는 PARSED와 FAILED 둘뿐이다 — NEEDS_REQUERY는
   청구자 에이전트의 판단이지 코드의 판단이 아니다 (§2)
3. 일시적 실패는 상태를 바꾸지 않는다 — 멀쩡한 영수증에 FAILED를 찍으면
   재요청 DM이 잘못 나간다
"""

import json
from datetime import date

import pytest

from src.parsing import pipeline
from src.parsing import store as parsing_store
from src.parsing.models import ParsedReceipt, ParsedReceiptItem
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
                "org_id": "org_1",
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
        lambda file_id, org_id: SlackFile(data=b"\xff\xd8img", mimetype="image/jpeg", ext="jpg"),
    )
    monkeypatch.setattr(pipeline, "get_object_store", lambda: LocalObjectStore(tmp_path))
    monkeypatch.setattr(
        pipeline, "record_audit_log", lambda **kwargs: state["audit"].append(kwargs)
    )
    monkeypatch.setattr(
        pipeline, "enqueue_claimant_review", lambda rid, *, receipt: state["enqueued"].append(rid)
    )

    state["claims"] = []
    state["commits"] = []

    def fake_commit(receipt_id, updates, claim):
        state["commits"].append((updates, claim))
        state["receipts"][receipt_id].update(updates)
        state["updates"].append((receipt_id, updates))
        if claim is not None:
            state["claims"].append(claim)
        return True

    monkeypatch.setattr(pipeline, "commit_parsed_with_claim", fake_commit)

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


def test_items_converted_to_minor_and_masked(monkeypatch, wired):
    result = _clean_result(
        items=[
            ParsedReceiptItem(name="아메리카노 010-1234-5678", amount_text="4,500"),
            ParsedReceiptItem(name="배송비", amount_text=None),
        ]
    )
    _install_parser(monkeypatch, RecordingParser(result=result))

    assert pipeline.parse_receipt("rct_1") == "PARSED"

    _, updates = wired["updates"][-1]
    assert updates["items"] == [
        {"name": "아메리카노 [PHONE]", "amount_minor": 4500},
        {"name": "배송비", "amount_minor": None},
    ]


def test_empty_items_defaults_to_empty_list(monkeypatch, wired):
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))

    assert pipeline.parse_receipt("rct_1") == "PARSED"

    _, updates = wired["updates"][-1]
    assert updates["items"] == []


def test_merchant_and_items_translated_to_english(monkeypatch, wired):
    """web이 언어 전환 시 즉시 보여줄 영어를 파싱 시점에 미리 만들어 둔다 —
    마스킹 이후 텍스트(merchant_name 다음 각 item.name 순서)를 한 번에 번역
    요청하고, 응답을 같은 순서로 merchant_name_en/items[].name_en에 되돌려
    꽂는다."""
    result = _clean_result(
        items=[
            ParsedReceiptItem(name="아메리카노", amount_text="4,500"),
            ParsedReceiptItem(name="배송비", amount_text=None),
        ]
    )
    _install_parser(monkeypatch, RecordingParser(result=result))
    captured_texts = []

    def fake_translate(texts):
        captured_texts.append(texts)
        return [f"[EN] {t}" for t in texts]

    monkeypatch.setattr(pipeline, "translate_lines", fake_translate)

    assert pipeline.parse_receipt("rct_1") == "PARSED"

    assert captured_texts == [["스타벅스 강남점 [PHONE]", "아메리카노", "배송비"]]
    _, updates = wired["updates"][-1]
    assert updates["merchant_name_en"] == "[EN] 스타벅스 강남점 [PHONE]"
    assert updates["items"] == [
        {"name": "아메리카노", "amount_minor": 4500, "name_en": "[EN] 아메리카노"},
        {"name": "배송비", "amount_minor": None, "name_en": "[EN] 배송비"},
    ]


def test_translation_failure_leaves_english_fields_absent(monkeypatch, wired):
    """번역은 부가 기능이다 — 실패해도(None) 파싱 자체는 그대로 PARSED로
    끝나고, 영어 필드만 조용히 비게 된다."""
    result = _clean_result(items=[ParsedReceiptItem(name="아메리카노", amount_text="4,500")])
    _install_parser(monkeypatch, RecordingParser(result=result))
    monkeypatch.setattr(pipeline, "translate_lines", lambda texts: None)

    assert pipeline.parse_receipt("rct_1") == "PARSED"

    _, updates = wired["updates"][-1]
    assert updates["merchant_name_en"] is None
    assert updates["items"] == [{"name": "아메리카노", "amount_minor": 4500}]


def test_no_merchant_name_still_translates_items(monkeypatch, wired):
    """merchant_name을 못 읽었어도(None) item 번역까지 건너뛸 이유는 없다 —
    번역 대상 목록에서 merchant_name만 빠지고 인덱스가 밀리지 않아야 한다."""
    result = _clean_result(
        merchant_name=None, items=[ParsedReceiptItem(name="아메리카노", amount_text="4,500")]
    )
    _install_parser(monkeypatch, RecordingParser(result=result))
    captured_texts = []

    def fake_translate(texts):
        captured_texts.append(texts)
        return [f"[EN] {t}" for t in texts]

    monkeypatch.setattr(pipeline, "translate_lines", fake_translate)

    assert pipeline.parse_receipt("rct_1") == "PARSED"

    assert captured_texts == [["아메리카노"]]
    _, updates = wired["updates"][-1]
    assert updates["merchant_name_en"] is None
    assert updates["items"] == [{"name": "아메리카노", "amount_minor": 4500, "name_en": "[EN] 아메리카노"}]


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


def test_snapshot_carries_raw_text_uri_not_the_raw_text(monkeypatch, wired):
    """§9 입력 계약은 `raw_text_gcs_uri`다. 원문 자체를 태스크 본문에 실으면
    Cloud Tasks 큐에 영속화되고 로그에 남으며, 에이전트 컨텍스트를 타고
    audit_logs로 샐 경로(§2가 금지)가 열린다. 마스킹으로는 못 막는다 —
    money-safety.md의 마스킹 대상 4종(카드번호·CVC·주민등록번호·여권번호)에
    사업자번호·전화번호는 없다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    sent = {}
    monkeypatch.setattr(
        pipeline, "enqueue_claimant_review", lambda rid, *, receipt: sent.update(receipt)
    )

    pipeline.parse_receipt("rct_1")

    assert sent["raw_text_gcs_uri"].endswith("raw_text/rct_1.txt")
    assert "raw_text" not in sent
    assert "02-1234-5678" not in json.dumps(sent, ensure_ascii=False)


def test_does_not_enqueue_on_failure(monkeypatch, wired):
    """FAILED는 재촉 루프 몫이지 청구자 에이전트 검토 대상이 아니다."""
    _install_parser(monkeypatch, RecordingParser(error=PermanentParseError("unreadable")))
    pipeline.parse_receipt("rct_1")
    assert wired["enqueued"] == []


def test_enqueue_failure_does_not_undo_parsed(monkeypatch, wired):
    """큐가 없어도 파싱 결과는 남아야 한다. ingest/routes.py가 같은 판단을 한다."""
    from src.guards.tasks import QueueNotConfigured

    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))

    def boom(receipt_id, *, receipt):
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

    def boom(receipt_id, *, receipt):
        raise RuntimeError("Cloud Tasks unavailable")

    monkeypatch.setattr(pipeline, "enqueue_claimant_review", boom)

    assert pipeline.parse_receipt("rct_1") == "PARSED"
    assert wired["receipts"]["rct_1"]["status"] == "PARSED"
    assert any(entry["action"] == "CLAIMANT_ENQUEUE_FAILED" for entry in wired["audit"])


def test_enqueue_is_still_called_when_audit_log_raises(monkeypatch, wired):
    """감사 로그(RECEIPT_PARSED)가 던져도 enqueue_claimant_review는 반드시
    호출되어야 한다 — 같은 try에 묶으면 감사 로그 실패가 enqueue를 통째로
    건너뛰게 만들고, 재시도해도 status != RECEIVED라 SKIPPED로 빠져 영구
    유실이 된다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))

    def boom(**kwargs):
        if kwargs.get("action") == "RECEIPT_PARSED":
            raise RuntimeError("Firestore unavailable")
        wired["audit"].append(kwargs)

    monkeypatch.setattr(pipeline, "record_audit_log", boom)

    assert pipeline.parse_receipt("rct_1") == "PARSED"
    assert wired["enqueued"] == ["rct_1"]


def test_audit_log_failure_is_recorded_as_audit_failure_not_enqueue_failure(monkeypatch, wired):
    """I1 회귀 — RECEIPT_PARSED 감사 로그 기록 자체가 실패해도 그 직후의
    enqueue_claimant_review는 별개 try라 정상 성공한다. 폴백 액션명이
    CLAIMANT_ENQUEUE_FAILED면 성공한 enqueue를 실패로 오기록해 감사 로그가
    증거로서 틀리게 된다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))

    def boom(**kwargs):
        if kwargs.get("action") == "RECEIPT_PARSED":
            raise RuntimeError("Firestore unavailable")
        wired["audit"].append(kwargs)

    monkeypatch.setattr(pipeline, "record_audit_log", boom)

    assert pipeline.parse_receipt("rct_1") == "PARSED"
    assert wired["enqueued"] == ["rct_1"]
    actions = [entry["action"] for entry in wired["audit"]]
    assert "RECEIPT_PARSED_AUDIT_FAILED" in actions
    assert "CLAIMANT_ENQUEUE_FAILED" not in actions


# --- 청구 항목 생성 (Task 2) ---

def test_creates_claim_when_parsed(monkeypatch, wired):
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    pipeline.parse_receipt("rct_1")

    assert len(wired["claims"]) == 1
    claim = wired["claims"][0]
    assert claim["receipt_id"] == "rct_1"
    assert claim["recipient_id"] == "rcp_1"
    assert claim["amount_minor"] == 45000
    assert claim["currency"] == "KRW"
    assert claim["status"] == "CONFIRMED"
    assert claim["settlement_run_id"] is None


def test_claim_and_parsed_status_commit_together(monkeypatch, wired):
    """★ 한 트랜잭션이어야 한다. 갈라지면 '파싱은 됐는데 청구가 없는' 영수증이
    재시도로도 복구되지 않는다(status != RECEIVED라 SKIPPED로 빠진다)."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    pipeline.parse_receipt("rct_1")

    # 커밋이 한 번, 그 안에 receipts 갱신과 claims 생성이 함께 들어갔다.
    assert len(wired["commits"]) == 1
    updates, claim = wired["commits"][0]
    assert updates["status"] == "PARSED"
    assert claim is not None and claim["receipt_id"] == "rct_1"


def test_claim_creation_failure_leaves_receipt_received(monkeypatch, wired):
    """트랜잭션이 실패하면 상태가 안 바뀌고 큐가 재시도한다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))

    def boom(receipt_id, updates, claim):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(pipeline, "commit_parsed_with_claim", boom)

    with pytest.raises(RuntimeError):
        pipeline.parse_receipt("rct_1")
    assert wired["receipts"]["rct_1"]["status"] == "RECEIVED"
    assert wired["claims"] == []


def test_no_claim_when_amount_missing(monkeypatch, wired):
    """금액을 못 읽은 영수증은 PARSED로 남되 청구는 안 만든다.
    0원 claim을 조용히 만드는 것보다 낫다."""
    _install_parser(
        monkeypatch,
        RecordingParser(result=_clean_result(amount_text=None, currency=None, confidence=None)),
    )

    assert pipeline.parse_receipt("rct_1") == "PARSED"
    assert wired["claims"] == []
    assert any(e["action"] == "CLAIM_NOT_CREATED" for e in wired["audit"])


def test_claim_created_audit_log(monkeypatch, wired):
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    pipeline.parse_receipt("rct_1")

    entry = next(e for e in wired["audit"] if e["action"] == "CLAIM_CREATED")
    assert entry["after"]["receipt_id"] == "rct_1"
    assert entry["after"]["claim_id"].startswith("clm_")


def test_no_duplicate_claim_on_retry(monkeypatch, wired):
    """Cloud Tasks 재시도. 두 번째 호출은 SKIPPED라 claim이 하나만 남아야 한다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    pipeline.parse_receipt("rct_1")
    assert pipeline.parse_receipt("rct_1") == "SKIPPED"
    assert len(wired["claims"]) == 1


# --- F1: 동시 전달 — 트랜잭션 CAS가 False를 돌려주면 SKIPPED이고 부수 효과가 안 돈다 ---

def test_parse_returns_skipped_when_commit_loses_race(monkeypatch, wired):
    """트랜잭션 밖의 status 확인만으로는 동시 전달을 막지 못한다 — 둘째 시도가
    commit_parsed_with_claim 안에서 CAS에 걸려 False를 받는 상황을 흉내낸다.
    이 경우 CLAIM_CREATED·RECEIPT_PARSED 감사 로그와 enqueue가 중복 실행되면
    안 된다."""
    _install_parser(monkeypatch, RecordingParser(result=_clean_result()))
    monkeypatch.setattr(pipeline, "commit_parsed_with_claim", lambda rid, updates, claim: False)

    assert pipeline.parse_receipt("rct_1") == "SKIPPED"
    assert wired["audit"] == []
    assert wired["enqueued"] == []
    assert wired["claims"] == []


# --- F1: src.parsing.store.commit_parsed_with_claim의 트랜잭션 내부 CAS ---
#
# 아래 fake는 ingest/tests/test_store.py의 FakeClient/FakeTransaction과 같은
# 성격이다 — 원자성(락·ABORTED·재실행)은 검증하지 않는다. 검증하는 건 두 가지:
# 1. status != RECEIVED(또는 문서 없음)면 쓰기가 0건인지
# 2. 그 읽기가 모든 쓰기보다 앞에 오는지(구조 테스트)


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def get(self, field):
        return self._data[field]


class _FakeDocRef:
    def __init__(self, backing, doc_id, log):
        self._backing, self.id, self._log = backing, doc_id, log

    def get(self, transaction=None):
        self._log.append(("get", self.id, transaction is not None))
        return _FakeSnapshot(self._backing.get(self.id))

    def set(self, data):
        self._backing[self.id] = data


class _FakeCollection:
    def __init__(self, backing, log):
        self._backing, self._log = backing, log

    def document(self, doc_id):
        return _FakeDocRef(self._backing, doc_id, self._log)


class _FakeTransaction:
    def __init__(self, log):
        self._log = log

    def update(self, ref, data):
        self._log.append(("update", ref.id, True))
        ref._backing[ref.id] = {**ref._backing.get(ref.id, {}), **data}

    def set(self, ref, data):
        self._log.append(("set", ref.id, True))
        ref.set(data)


class _FakeClient:
    def __init__(self, log):
        self.data = {"receipts": {}, "claims": {}}
        self._log = log

    def collection(self, name):
        return _FakeCollection(self.data.setdefault(name, {}), self._log)

    def transaction(self):
        return _FakeTransaction(self._log)


@pytest.fixture
def store_fake(monkeypatch):
    """`@firestore.transactional`을 항등 데코레이터로 갈아끼운다 — 콜백을
    실제 재시도 없이 바로 실행해 콜백 본문(읽기→검사→쓰기 순서)만 검증한다."""
    log = []
    client = _FakeClient(log)
    monkeypatch.setattr(parsing_store, "get_client", lambda: client)
    monkeypatch.setattr(parsing_store.firestore, "transactional", lambda fn: fn)
    client.log = log
    return client


def test_commit_writes_when_receipt_is_received(store_fake):
    store_fake.data["receipts"]["rct_1"] = {"receipt_id": "rct_1", "status": "RECEIVED"}
    claim = {"claim_id": "clm_1", "receipt_id": "rct_1"}

    committed = parsing_store.commit_parsed_with_claim("rct_1", {"status": "PARSED"}, claim)

    assert committed is True
    assert store_fake.data["receipts"]["rct_1"]["status"] == "PARSED"
    assert store_fake.data["claims"]["clm_1"] == claim


def test_commit_writes_zero_when_receipt_already_confirmed(store_fake):
    """동시 전달 시나리오 — 다른 시도가 먼저 확정했으면(status != RECEIVED)
    쓰기가 0건이어야 한다."""
    store_fake.data["receipts"]["rct_1"] = {"receipt_id": "rct_1", "status": "PARSED"}
    claim = {"claim_id": "clm_1", "receipt_id": "rct_1"}

    committed = parsing_store.commit_parsed_with_claim("rct_1", {"status": "PARSED"}, claim)

    assert committed is False
    assert store_fake.data["claims"] == {}
    assert not [entry for entry in store_fake.log if entry[0] in ("update", "set")]


def test_commit_writes_zero_when_receipt_missing(store_fake):
    claim = {"claim_id": "clm_1", "receipt_id": "rct_nope"}

    committed = parsing_store.commit_parsed_with_claim("rct_nope", {"status": "PARSED"}, claim)

    assert committed is False
    assert store_fake.data["claims"] == {}


def test_commit_read_happens_before_any_write(store_fake):
    """구조 테스트 — ingest/test_store.py의
    test_dedup_key_is_read_before_any_write와 같은 성격이다. receipt 읽기가
    receipts 갱신·claims 생성보다 먼저 일어나야 트랜잭션 락이 걸린다."""
    store_fake.data["receipts"]["rct_1"] = {"receipt_id": "rct_1", "status": "RECEIVED"}
    claim = {"claim_id": "clm_1", "receipt_id": "rct_1"}

    parsing_store.commit_parsed_with_claim("rct_1", {"status": "PARSED"}, claim)

    kinds = [entry[0] for entry in store_fake.log]
    assert kinds[0] == "get", f"첫 연산이 읽기가 아니다: {store_fake.log}"
    first_write_index = min(kinds.index(k) for k in ("update", "set") if k in kinds)
    assert "get" not in kinds[first_write_index:], f"쓰기 뒤에 읽기가 있다: {store_fake.log}"

    first_read = store_fake.log[0]
    assert first_read[1] == "rct_1"
    assert first_read[2] is True, "receipt 읽기가 트랜잭션 밖에서 일어났다"
