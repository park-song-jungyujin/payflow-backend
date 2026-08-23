"""schema-contract.md §9 — fixture 02·06의 CLAIMANT draft를 실제 payload로 흘려
`parse_claimant_payload` → `apply_claimant_verdict` 경로가 도는지 확인하는 인수 게이트.

청구자 에이전트가 501 스텁인 지금도, 실제 데모 fixture에 이미 담긴 CLAIMANT
draft(payload)를 그대로 판정 함수에 넣으면 (1) 파싱이 되고 (2) 전이가 올바로
나며 (3) 재요청 문안(requery_message)이 코드가 지어낸 게 아니라 fixture 그대로
보존되는지를 본다.

fixture 02의 receipt는 이미 `NEEDS_REQUERY`인 "반영 후" 상태를 담고 있으므로
그대로 쓰면 apply_claimant_verdict의 멱등 가드에 걸려 SKIPPED가 난다. 여기서는
payload만 fixture에서 읽고, receipt·claim은 반영 직전 상태(PARSED/CONFIRMED)로
직접 구성한다 — 실제 실행 순서(파싱 완료 → 청구자 판정 반영)를 재현하기 위함.

tests/ingest/test_draft_apply.py의 FakeClient/FakeTransaction 구조를 그대로
재사용한다 — 실제 Firestore에는 붙지 않는다.
"""

import glob
import json
from datetime import UTC, datetime, timedelta

import pytest

from src.ingest import store
from src.ingest.drafts import parse_claimant_payload
from src.schemas.models import ClaimRequest

from tests.ingest.test_draft_apply import FakeClient, FakeTransaction

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _claimant_drafts():
    """fixture 전체에서 CLAIMANT draft만 (fixture 경로, draft dict)로 뽑는다."""
    drafts = []
    for path in sorted(glob.glob("tests/fixtures/*.json")):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for draft in data.get("agent_drafts", []):
            if draft.get("agent") == "CLAIMANT":
                drafts.append((path, draft))
    return drafts


CLAIMANT_DRAFTS = _claimant_drafts()


def test_claimant_draft_corpus_is_not_empty():
    """fixture 경로나 파일명이 바뀌면 아래 회귀 전부가 조용히 0건으로 통과한다."""
    assert len(CLAIMANT_DRAFTS) >= 2, (
        f"fixture에서 찾은 CLAIMANT draft가 {len(CLAIMANT_DRAFTS)}건뿐이다"
    )


def _target_ids():
    return [draft["target_id"] for _, draft in CLAIMANT_DRAFTS]


def test_claimant_draft_targets_are_the_expected_two_receipts():
    """fixture 02·06의 CLAIMANT draft가 실제로 뽑히는지 target_id로 고정 확인한다."""
    assert _target_ids() == [
        "rct_01SCN02BLURRYPHOTO0000001",
        "rct_01SCN06INJECTIONRCT00001",
    ]


@pytest.fixture
def fake(monkeypatch):
    log = []
    client = FakeClient(log)
    monkeypatch.setattr(store, "get_client", lambda: client)
    monkeypatch.setattr(store, "_run_in_transaction", lambda fn: fn(FakeTransaction(log)))
    audit_calls = []
    monkeypatch.setattr(store, "record_audit_log", lambda **kwargs: audit_calls.append(kwargs))
    client.log = log
    client.audit_calls = audit_calls
    return client


def _seed_pre_verdict_state(fake, receipt_id, claim_id, recipient_id="rcp_e2e"):
    """반영 직전 상태 — receipt는 PARSED, claim은 CONFIRMED. fixture의 receipt
    스냅샷(NEEDS_REQUERY)은 여기서 쓰지 않는다 — 그건 판정이 반영된 *뒤*의 상태다."""
    fake.data["receipts"][receipt_id] = {
        "receipt_id": receipt_id,
        "org_id": "org_1",
        "recipient_id": recipient_id,
        "status": "PARSED",
        "created_at": NOW,
        "updated_at": NOW,
    }
    fake.data["claims"][claim_id] = {
        "claim_id": claim_id,
        "recipient_id": recipient_id,
        "receipt_id": receipt_id,
        "amount_minor": 99000,
        "currency": "KRW",
        "account_category_code": "UNCLASSIFIED",
        "is_business": True,
        "settlement_run_id": None,
        "settled_at": None,
        "status": "CONFIRMED",
        "created_at": NOW,
        "updated_at": NOW,
    }


@pytest.mark.parametrize("fixture_path,draft", CLAIMANT_DRAFTS, ids=[d["target_id"] for _, d in CLAIMANT_DRAFTS])
def test_fixture_claimant_draft_transitions_receipt_and_claim(fake, fixture_path, draft):
    receipt_id = draft["target_id"]
    claim_id = f"clm_{receipt_id}"

    verdict = parse_claimant_payload(draft["payload"])
    assert verdict.needs_requery is True, (
        f"{fixture_path}: 이 게이트는 needs_requery=true fixture만 다룬다"
    )

    _seed_pre_verdict_state(fake, receipt_id, claim_id)

    result, _ = store.apply_claimant_verdict(receipt_id, verdict, now=NOW)

    assert result == "REQUERY"
    assert fake.data["receipts"][receipt_id]["status"] == "NEEDS_REQUERY"
    assert fake.data["claims"][claim_id]["status"] == "DRAFT"

    assert len(fake.data["claim_requests"]) == 1
    request = next(iter(fake.data["claim_requests"].values()))
    assert request["status"] == "PENDING"
    assert request["reason"] == "AMOUNT_MISMATCH"
    assert request["receipt_id"] == receipt_id
    assert request["expires_at"] == NOW + timedelta(seconds=86400)

    ClaimRequest.model_validate(request)


def test_fixture_06_injection_requery_message_is_preserved_verbatim(fake):
    """§9 경계 — 재요청 문안은 에이전트가 만든 것이고 코드가 지어내지 않는다.
    fixture 06(프롬프트 인젝션 시나리오)의 requery_message가 판정 파싱과
    전이 반영을 거치고도 글자 하나 안 바뀌고 나오는지 확인한다."""
    fixture_path, draft = next(
        (path, d) for path, d in CLAIMANT_DRAFTS if d["target_id"] == "rct_01SCN06INJECTIONRCT00001"
    )
    expected_message = draft["payload"]["requery_message"]
    assert expected_message, f"{fixture_path}: requery_message가 비어 있으면 이 테스트가 의미 없다"

    verdict = parse_claimant_payload(draft["payload"])
    assert verdict.requery_message == expected_message

    receipt_id = draft["target_id"]
    claim_id = f"clm_{receipt_id}"
    _seed_pre_verdict_state(fake, receipt_id, claim_id)

    store.apply_claimant_verdict(receipt_id, verdict, now=NOW)

    # 판정 자체(verdict)에 requery_message가 실려 있고 apply 호출 전후로
    # 바뀌지 않았다는 확인 — apply_claimant_verdict의 반환값·claim_requests
    # 스키마(§3)는 requery_message를 저장 필드로 갖지 않으므로(재요청 문안은
    # 별도 Slack 발송 경로가 다룬다), 여기서는 판정 모델 단계에서의 보존을 본다.
    assert verdict.requery_message == expected_message
