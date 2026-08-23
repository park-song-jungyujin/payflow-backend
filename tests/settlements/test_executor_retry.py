"""schema-contract.md §9 — 집행자 분석 확인 태스크(재시도 루프).

ingest/routes.py의 /tasks/remind와 같은 "자기 재예약이 유일한 생명선" 패턴을
settlements 쪽에 적용한 것. Cloud Tasks에 enqueue된 초기 시도가 배포 도중
재배포로 조용히 죽어도, 이 확인 태스크가 스스로를 다시 깨워 재-enqueue한다.
"""

import pytest
from fastapi import HTTPException

from src.settlements import routes


def _wire(monkeypatch, *, draft=None, retry_check_error=None, analyze_error=None):
    monkeypatch.setattr(routes, "verify_oidc", lambda authorization: None)
    monkeypatch.setattr(routes, "get_agent_draft", lambda task_id: draft)

    analyze_calls = []

    def fake_analyze(run_id, claim_summaries, duplicate_groups, exact_duplicate_groups):
        analyze_calls.append((run_id, claim_summaries, duplicate_groups, exact_duplicate_groups))
        if analyze_error:
            raise analyze_error

    monkeypatch.setattr(routes, "enqueue_executor_analyze", fake_analyze)

    retry_calls = []

    def fake_retry_check(run_id, claim_summaries, duplicate_groups, exact_duplicate_groups, *, attempt, delay_seconds):
        retry_calls.append(
            {
                "run_id": run_id,
                "attempt": attempt,
                "delay_seconds": delay_seconds,
            }
        )
        if retry_check_error:
            raise retry_check_error

    monkeypatch.setattr(routes, "enqueue_executor_retry_check", fake_retry_check)

    audit_calls = []
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: audit_calls.append(kw))

    return analyze_calls, retry_calls, audit_calls


def _body(**overrides):
    body = {
        "settlement_run_id": "run_1",
        "candidate_claims": [{"claim_id": "clm_1"}],
        "duplicate_groups": [],
        "exact_duplicate_groups": [],
        "attempt": 1,
    }
    body.update(overrides)
    return body


def test_already_analyzed_is_a_noop(monkeypatch):
    """agent_drafts.EXECUTOR가 이미 있으면 재-enqueue도 재예약도 안 한다 —
    정상적으로 끝난 run을 계속 깨울 이유가 없다."""
    analyze_calls, retry_calls, _ = _wire(monkeypatch, draft={"payload": {}})

    result = routes.task_retry_executor_analysis(_body(), authorization="Bearer x")

    assert result == {"status": "ok", "reason": "already_analyzed"}
    assert analyze_calls == []
    assert retry_calls == []


def test_missing_analysis_re_enqueues_and_reschedules(monkeypatch):
    analyze_calls, retry_calls, _ = _wire(monkeypatch, draft=None)

    result = routes.task_retry_executor_analysis(_body(attempt=1), authorization="Bearer x")

    assert result == {"status": "ok", "reason": "retried"}
    assert len(analyze_calls) == 1
    assert retry_calls == [{"run_id": "run_1", "attempt": 2, "delay_seconds": 120}]


def test_gives_up_at_max_attempts_without_rescheduling(monkeypatch):
    """무한 재예약이면 진짜로 죽은 run이 영원히 태스크를 만든다 — 상한이 필요하다."""
    analyze_calls, retry_calls, audit_calls = _wire(monkeypatch, draft=None)

    result = routes.task_retry_executor_analysis(
        _body(attempt=routes._EXECUTOR_RETRY_MAX_ATTEMPTS), authorization="Bearer x"
    )

    assert result == {"status": "ok", "reason": "gave_up"}
    assert analyze_calls == []
    assert retry_calls == []
    assert any(a["action"] == "EXECUTOR_ANALYSIS_STALLED" for a in audit_calls)


def test_analyze_failure_during_retry_still_reschedules(monkeypatch):
    """재-enqueue 자체가 또 실패해도(같은 배포 창) 재예약 시도는 계속한다 —
    다음 깨어남에서 배포가 끝나 있을 수 있다."""
    analyze_calls, retry_calls, audit_calls = _wire(
        monkeypatch, draft=None, analyze_error=RuntimeError("still deploying")
    )

    result = routes.task_retry_executor_analysis(_body(attempt=1), authorization="Bearer x")

    assert result == {"status": "ok", "reason": "retried"}
    assert len(analyze_calls) == 1
    assert retry_calls == [{"run_id": "run_1", "attempt": 2, "delay_seconds": 120}]
    assert any(a["action"] == "EXECUTOR_ENQUEUE_FAILED" for a in audit_calls)


def test_missing_run_id_is_rejected(monkeypatch):
    _wire(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        routes.task_retry_executor_analysis({}, authorization="Bearer x")
    assert exc_info.value.status_code == 400


def test_create_run_schedules_retry_check(monkeypatch):
    """정산 실행 생성 직후엔 항상 확인 태스크를 건다 — 최초 enqueue 성공 여부와
    무관하게(성공해도 agent 쪽에서 조용히 죽을 수 있으니까)."""
    claims = [{"claim_id": "clm_1", "recipient_id": "rcp_1", "receipt_id": "rct_1", "amount_minor": 1000, "currency": "KRW", "account_category_code": "TRAVEL"}]
    monkeypatch.setattr(routes, "select_claims_for_run", lambda filter: claims)
    monkeypatch.setattr(
        routes,
        "verify_candidates",
        lambda candidates: {"passed_claims": claims, "failed_claims": [], "receipts": {}},
    )
    monkeypatch.setattr(routes, "create_settlement_run", lambda run_id, doc: None)
    monkeypatch.setattr(routes, "link_claims_to_run", lambda run_id, claim_ids: None)
    monkeypatch.setattr(routes, "enqueue_executor_analyze", lambda *a: None)
    monkeypatch.setattr(routes, "record_audit_log", lambda **kw: None)

    retry_calls = []
    monkeypatch.setattr(
        routes,
        "enqueue_executor_retry_check",
        lambda run_id, *a, attempt, delay_seconds: retry_calls.append(
            {"run_id": run_id, "attempt": attempt}
        ),
    )

    result = routes.create_settlement_run_route(body={})

    assert retry_calls == [{"run_id": result["settlement_run_id"], "attempt": 1}]
