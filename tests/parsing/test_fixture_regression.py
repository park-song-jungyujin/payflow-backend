"""fixture 9종 종단 회귀 — 실제 Gemini를 붙이기 전의 인수 조건.

schema-contract.md §12: fixture는 데모 데이터셋이자 계약 예시다. 파이프라인이
같은 입력에서 같은 계정과목 라우팅을 내는지 통째로 확인한다.

**04는 제외한다.** 저장된 category_source가 EXECUTOR_AGENT라 파싱 시점 값이
아니라 집행자 에이전트가 재판단한 뒤 값이다. 07·08에는 receipts가 없다.
"""

import glob
import json

import pytest

from src.parsing import pipeline
from src.parsing.parser import FixtureReceiptParser
from src.parsing.storage import LocalObjectStore
from src.parsing.slack_files import SlackFile


def _fixture_receipts():
    """파싱 시점 값을 담고 있는 receipt만 고른다."""
    cases = []
    for path in sorted(glob.glob("tests/fixtures/*.json")):
        with open(path, encoding="utf-8") as f:
            for receipt in json.load(f).get("receipts", []):
                if receipt.get("category_source") in ("LLM_PARSE", "DETERMINISTIC_FALLBACK"):
                    cases.append(pytest.param(receipt, id=receipt["receipt_id"]))
    return cases


CASES = _fixture_receipts()


def test_fixture_corpus_is_not_empty():
    """fixture 경로나 파일명이 바뀌면 아래 전부가 조용히 0건으로 통과한다."""
    assert len(CASES) >= 8, f"파싱 대상 fixture receipt이 {len(CASES)}건뿐이다"


@pytest.fixture
def wired_pipeline(monkeypatch, tmp_path):
    """pipeline의 외부 경계를 전부 monkeypatch하고 Firestore write 결과를 캡처한다.

    실제 Firestore·Slack·GCS·Cloud Tasks에는 절대 붙지 않는다 — get_receipt/
    update_receipt/download_slack_file/get_object_store/record_audit_log/
    enqueue_claimant_review/get_parser 전부를 여기서 대체한다.
    """
    written = {}

    monkeypatch.setattr(
        pipeline,
        "get_receipt",
        lambda rid: {"receipt_id": rid, "recipient_id": "rcp_1", "slack_file_id": "F1", "status": "RECEIVED"},
    )
    monkeypatch.setattr(pipeline, "update_receipt", lambda rid, updates: written.update(updates))
    monkeypatch.setattr(
        pipeline,
        "download_slack_file",
        lambda file_id: SlackFile(data=b"\xff\xd8img", mimetype="image/jpeg", ext="jpg"),
    )
    monkeypatch.setattr(pipeline, "get_object_store", lambda: LocalObjectStore(tmp_path))
    monkeypatch.setattr(pipeline, "record_audit_log", lambda **kwargs: None)
    monkeypatch.setattr(pipeline, "enqueue_claimant_review", lambda rid: None)
    monkeypatch.setattr(pipeline, "get_parser", lambda: FixtureReceiptParser.from_fixtures())

    return written


@pytest.mark.parametrize("expected", CASES)
def test_pipeline_reproduces_fixture_routing(wired_pipeline, expected):
    receipt_id = expected["receipt_id"]
    written = wired_pipeline

    assert pipeline.parse_receipt(receipt_id) == "PARSED"

    assert written["account_category_code"] == expected["account_category_code"], (
        f"{receipt_id}: 계정과목 라우팅이 fixture와 다르다"
    )
    assert written["category_source"] == expected["category_source"], (
        f"{receipt_id}: category_source가 fixture와 다르다"
    )
    assert written["parsed_amount_minor"] == expected.get("parsed_amount_minor")
    assert written["currency"] == expected.get("currency")
    assert written["transaction_date"] == expected.get("transaction_date")
    assert written["parse_signals"] == expected["parse_signals"]
    assert written["llm_confidence"] == expected.get("llm_confidence")


@pytest.mark.parametrize("expected", CASES)
def test_pipeline_never_downgrades_status(wired_pipeline, expected):
    """fixture 9종 어디에서도 파싱이 NEEDS_REQUERY를 쓰지 않는다 (§2)."""
    written = wired_pipeline

    pipeline.parse_receipt(expected["receipt_id"])
    assert written["status"] == "PARSED"
