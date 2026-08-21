"""schema-contract.md §10 POST /settlements/runs — verify_candidates 이후 흐름
(claim 요약 · 중복 판정 · 집행자 에이전트 enqueue)을 검증한다.

tests/guards/test_routes.py와 같은 패턴 — TestClient/ASGI를 거치지 않고 라우트
핸들러 함수를 직접 부른다. select_claims_for_run/verify_candidates/Firestore
쓰기/enqueue를 전부 모듈 레벨에서 monkeypatch한다.
"""

from datetime import date

from src.settlements import routes


def _claim(claim_id, receipt_id="rct_1", **overrides):
    claim = {
        "claim_id": claim_id,
        "recipient_id": "rcp_1",
        "receipt_id": receipt_id,
        "amount_minor": 10000,
        "currency": "KRW",
        "account_category_code": "TRAVEL",
    }
    claim.update(overrides)
    return claim


class _FakeStub:
    """create_settlement_run/link_claims_to_run 호출만 기록한다."""

    def __init__(self):
        self.created = []
        self.linked = []

    def create(self, run_id, doc):
        self.created.append((run_id, doc))

    def link(self, run_id, claim_ids):
        self.linked.append((run_id, claim_ids))


def _wire(monkeypatch, *, claims, receipts, enqueue_error=None):
    monkeypatch.setattr(routes, "select_claims_for_run", lambda filter: claims)
    monkeypatch.setattr(
        routes,
        "verify_candidates",
        lambda candidates: {"passed_claims": claims, "failed_claims": [], "receipts": receipts},
    )

    stub = _FakeStub()
    monkeypatch.setattr(routes, "create_settlement_run", stub.create)
    monkeypatch.setattr(routes, "link_claims_to_run", stub.link)

    enqueue_calls = []

    def fake_enqueue(run_id, claim_summaries, duplicate_groups):
        enqueue_calls.append((run_id, claim_summaries, duplicate_groups))
        if enqueue_error:
            raise enqueue_error

    monkeypatch.setattr(routes, "enqueue_executor_analyze", fake_enqueue)

    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))

    return stub, enqueue_calls, audit_calls


def test_create_run_enqueues_executor_analyze_with_claim_summaries(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    stub, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts=receipts)

    result = routes.create_settlement_run_route(body={})

    assert len(enqueue_calls) == 1
    run_id, claim_summaries, duplicate_groups = enqueue_calls[0]
    assert run_id == result["settlement_run_id"]
    assert claim_summaries == [
        {
            "claim_id": "clm_1",
            "recipient_id": "rcp_1",
            "amount_minor": 10000,
            "currency": "KRW",
            "account_category_code": "TRAVEL",
            "merchant_name": "스타벅스",
            "transaction_date": "2026-08-10",
        }
    ]
    assert duplicate_groups == []  # claim 1건뿐이라 중복 그룹이 안 생긴다


def test_create_run_finds_duplicate_group_among_passed_claims(monkeypatch):
    claims = [_claim("clm_1", receipt_id="rct_1"), _claim("clm_2", receipt_id="rct_2")]
    receipts = {
        "rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)},
        "rct_2": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)},
    }
    _, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts=receipts)

    routes.create_settlement_run_route(body={})

    _, _, duplicate_groups = enqueue_calls[0]
    assert len(duplicate_groups) == 1
    assert set(duplicate_groups[0]["claim_ids"]) == {"clm_1", "clm_2"}


def test_missing_receipt_produces_null_merchant_and_date(monkeypatch):
    """receipt가 없는 claim도(이론상 발생하지 않아야 하지만) 요약 생성이 죽지 않는다."""
    claims = [_claim("clm_1", receipt_id="rct_missing")]
    _, enqueue_calls, _ = _wire(monkeypatch, claims=claims, receipts={})

    routes.create_settlement_run_route(body={})

    _, claim_summaries, _ = enqueue_calls[0]
    assert claim_summaries[0]["merchant_name"] is None
    assert claim_summaries[0]["transaction_date"] is None


def test_enqueue_failure_does_not_break_run_creation(monkeypatch):
    """집행자 분석은 조언일 뿐이다 — enqueue가 실패해도 정산 실행은 정상 생성된다."""
    claims = [_claim("clm_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    stub, enqueue_calls, audit_calls = _wire(
        monkeypatch, claims=claims, receipts=receipts, enqueue_error=RuntimeError("boom")
    )

    result = routes.create_settlement_run_route(body={})

    assert result["status"] == "DRAFT"
    assert len(stub.created) == 1  # 배치는 정상적으로 만들어졌다
    assert audit_calls == [
        {
            "actor": "api/src/settlements",
            "action": "EXECUTOR_ENQUEUE_FAILED",
            "run_id": result["settlement_run_id"],
            "reason": "boom",
            "after": {"settlement_run_id": result["settlement_run_id"]},
        }
    ]


def test_audit_log_failure_does_not_mask_response(monkeypatch):
    """감사 로그 자체가 죽어도(Firestore 장애 등) 응답은 그대로 나간다."""
    claims = [_claim("clm_1")]
    receipts = {"rct_1": {"merchant_name": "스타벅스", "transaction_date": date(2026, 8, 10)}}
    _wire(monkeypatch, claims=claims, receipts=receipts, enqueue_error=RuntimeError("boom"))
    monkeypatch.setattr(
        routes,
        "record_audit_log",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("firestore down")),
    )

    result = routes.create_settlement_run_route(body={})

    assert result["status"] == "DRAFT"


def test_empty_candidate_batch_enqueues_with_empty_lists(monkeypatch):
    _, enqueue_calls, _ = _wire(monkeypatch, claims=[], receipts={})

    routes.create_settlement_run_route(body={})

    _, claim_summaries, duplicate_groups = enqueue_calls[0]
    assert claim_summaries == []
    assert duplicate_groups == []
