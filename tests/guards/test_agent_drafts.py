"""schema-contract.md §9 — POST /agents/drafts. Task 3 hook: CLAIMANT draft
쓰기가 /tasks/apply-claimant-draft enqueue를 촉발하는지, EXECUTOR draft
쓰기가 /tasks/translate-executor-draft enqueue를 촉발하는지, SAFETY에서는
둘 다 안 촉발하는지, 큐 미설정이어도 draft 쓰기 자체는 여전히 200을
돌려주는지 검증한다."""

import pytest
from fastapi import HTTPException

from src.guards import agent_drafts


class FakeSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return self._data


class FakeDocRef:
    def __init__(self, store, doc_id):
        self._store, self._doc_id = store, doc_id

    def set(self, data):
        self._store[self._doc_id] = data

    def get(self):
        return FakeSnapshot(self._store.get(self._doc_id))

    def update(self, data):
        """실제 Firestore처럼 점(.) 경로 키("payload.summary_text_ko")를
        중첩 dict 병합으로 처리한다 — task_translate_executor_draft가 이 방식을 쓴다."""
        doc = self._store[self._doc_id]
        for key, value in data.items():
            parts = key.split(".")
            target = doc
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value


class FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return FakeDocRef(self._store, doc_id)


class FakeClient:
    def __init__(self):
        self.data = {}

    def collection(self, name):
        assert name == "agent_drafts"
        return FakeCollection(self.data)


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(agent_drafts, "verify_oidc", lambda auth: {})
    client = FakeClient()
    monkeypatch.setattr(agent_drafts, "get_client", lambda: client)
    audit_calls = []
    monkeypatch.setattr(agent_drafts, "record_audit_log", lambda **kw: audit_calls.append(kw))
    return {"client": client, "audit_calls": audit_calls}


def _body(**overrides):
    body = {
        "agent": "CLAIMANT",
        "target_type": "RECEIPT",
        "target_id": "rct_1",
        "task_id": "CLAIMANT:rct_1",
        "payload": {"needs_requery": False},
    }
    body.update(overrides)
    return body


def test_claimant_draft_enqueues_apply_task(monkeypatch, _patch):
    calls = []
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: calls.append((path, payload)))

    result = agent_drafts.write_agent_draft(_body())

    assert result["status"] == "ok"
    assert calls == [("/tasks/apply-claimant-draft", {"task_id": "CLAIMANT:rct_1"})]


def test_safety_draft_does_not_enqueue(monkeypatch, _patch):
    calls = []
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: calls.append((path, payload)))

    result = agent_drafts.write_agent_draft(
        _body(agent="SAFETY", target_type="SETTLEMENT_RUN", target_id="run_1", task_id="SAFETY:run_1")
    )

    assert result["status"] == "ok"
    assert calls == []


def test_executor_draft_enqueues_translation_task(monkeypatch, _patch):
    """write_agent_draft_document는 EXECUTOR draft를 커밋한 직후 한국어 번역을
    별도 Cloud Task로 미룬다 — Gemma 호출을 요청 경로에서 완전히 뺀다."""
    calls = []
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: calls.append((path, payload)))

    result = agent_drafts.write_agent_draft(
        _body(
            agent="EXECUTOR",
            target_type="SETTLEMENT_RUN",
            target_id="run_1",
            task_id="EXECUTOR:run_1",
            payload={"summary_text": "1 suspected duplicate", "anomalies": ["anomaly 1", "anomaly 2"]},
        )
    )

    assert result["status"] == "ok"
    assert calls == [
        (
            "/tasks/translate-executor-draft",
            {
                "task_id": "EXECUTOR:run_1",
                "summary_text": "1 suspected duplicate",
                "anomalies": ["anomaly 1", "anomaly 2"],
            },
        )
    ]
    # draft 자체는 영어 그대로 즉시 커밋된다 — 번역 완료를 기다리지 않는다.
    stored = _patch["client"].data["EXECUTOR:run_1"]
    assert "summary_text_ko" not in stored["payload"]
    assert "anomalies_ko" not in stored["payload"]


def test_executor_empty_summary_skips_translation_enqueue(monkeypatch, _patch):
    calls = []
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: calls.append((path, payload)))

    agent_drafts.write_agent_draft(
        _body(
            agent="EXECUTOR",
            target_type="SETTLEMENT_RUN",
            target_id="run_1",
            task_id="EXECUTOR:run_1",
            payload={"summary_text": "", "anomalies": []},
        )
    )

    assert calls == []


def test_executor_translation_enqueue_failure_is_audited_but_draft_still_written(monkeypatch, _patch):
    def boom(path, payload):
        raise Exception("CLOUD_TASKS_QUEUE not configured")

    monkeypatch.setattr(agent_drafts, "enqueue_task", boom)

    result = agent_drafts.write_agent_draft(
        _body(
            agent="EXECUTOR",
            target_type="SETTLEMENT_RUN",
            target_id="run_1",
            task_id="EXECUTOR:run_1",
            payload={"summary_text": "1 suspected duplicate", "anomalies": []},
        )
    )

    assert result["status"] == "ok"
    stored = _patch["client"].data["EXECUTOR:run_1"]
    assert stored["payload"]["summary_text"] == "1 suspected duplicate"
    audit_calls = _patch["audit_calls"]
    failed_call = next(c for c in audit_calls if c["action"] == "EXECUTOR_TRANSLATION_ENQUEUE_FAILED")
    assert failed_call["after"] == {"task_id": "EXECUTOR:run_1"}


def test_queue_not_configured_still_returns_200_and_logs_audit(monkeypatch, _patch):
    def boom(path, payload):
        raise Exception("CLOUD_TASKS_QUEUE not configured")

    monkeypatch.setattr(agent_drafts, "enqueue_task", boom)

    result = agent_drafts.write_agent_draft(_body())

    assert result == {"draft_id": "drf_CLAIMANT:rct_1", "status": "ok"}
    audit_calls = _patch["audit_calls"]
    actions = [c["action"] for c in audit_calls]
    assert "AGENT_DRAFT_WRITTEN" in actions
    assert "CLAIMANT_DRAFT_APPLY_ENQUEUE_FAILED" in actions
    failed_call = next(c for c in audit_calls if c["action"] == "CLAIMANT_DRAFT_APPLY_ENQUEUE_FAILED")
    assert failed_call["after"] == {"draft_id": "drf_CLAIMANT:rct_1", "task_id": "CLAIMANT:rct_1"}


def test_draft_document_and_original_audit_log_unchanged(monkeypatch, _patch):
    """훅이 붙어도 draft 문서 내용과 기존 AGENT_DRAFT_WRITTEN 감사 로그는 그대로여야 한다."""
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: None)

    result = agent_drafts.write_agent_draft(_body())

    client = _patch["client"]
    stored = client.data["CLAIMANT:rct_1"]
    assert stored["draft_id"] == "drf_CLAIMANT:rct_1"
    assert stored["agent"] == "CLAIMANT"
    assert stored["target_type"] == "RECEIPT"
    assert stored["target_id"] == "rct_1"
    assert stored["task_id"] == "CLAIMANT:rct_1"
    assert stored["payload"] == {"needs_requery": False}
    assert result == {"draft_id": "drf_CLAIMANT:rct_1", "status": "ok"}

    audit_calls = _patch["audit_calls"]
    written = next(c for c in audit_calls if c["action"] == "AGENT_DRAFT_WRITTEN")
    assert written["actor"] == "agent/claimant"
    assert written["after"] == {"draft_id": "drf_CLAIMANT:rct_1"}


def test_missing_authorization_returns_401(monkeypatch, _patch):
    from src.guards.oidc import verify_oidc as real_verify_oidc

    monkeypatch.setattr(agent_drafts, "verify_oidc", real_verify_oidc)
    with pytest.raises(HTTPException) as exc:
        agent_drafts.write_agent_draft(_body(), authorization="")
    assert exc.value.status_code == 401


# --- Gemma 번역 배선 — draft 쓰기 경로에는 이제 아무 것도 없다 ---
#
# CLAIMANT의 requery_message는 청구자 에이전트가 처음부터 영어로 쓴다
# (payflow-agent claimant/agent.py). 한때 여기서 Gemma로 번역해
# requery_message_en을 채웠지만, 그 번역이 간헐적으로 실패하면 조용히 한국어로
# 폴백해 같은 문구가 어떤 영수증에는 영어로, 어떤 영수증에는 한국어로 Slack에
# 도착했다. EXECUTOR의 한국어 번역은 이 경로가 아니라 비동기 Cloud Task다
# (_enqueue_executor_translation).


def test_claimant_draft_is_not_translated(monkeypatch, _patch):
    """청구자 문안은 이미 영어다 — Gemma를 부르지 않고, _en 필드도 안 만든다.
    번역을 한 번 더 태우면 실패할 기회만 생기고 draft 쓰기에 최대 15초가 붙는다."""
    monkeypatch.setattr(agent_drafts, "enqueue_task", lambda path, payload: None)
    calls = []
    monkeypatch.setattr(agent_drafts, "translate_lines", lambda *a, **kw: calls.append(a) or [])

    agent_drafts.write_agent_draft(
        _body(
            payload={
                "needs_requery": True,
                "requery_message": "The receipt is unreadable. Please send it again.",
            }
        )
    )

    assert calls == []
    stored = _patch["client"].data["CLAIMANT:rct_1"]
    assert "requery_message_en" not in stored["payload"]
    assert (
        stored["payload"]["requery_message"]
        == "The receipt is unreadable. Please send it again."
    )


def test_safety_draft_is_never_translated(monkeypatch, _patch):
    """safety는 아직 아무 데도 안 보이는 출력이다 — 번역 대상이 아니다."""
    calls = []
    monkeypatch.setattr(agent_drafts, "translate_lines", lambda texts: calls.append(texts) or [])

    agent_drafts.write_agent_draft(
        _body(
            agent="SAFETY",
            target_type="SETTLEMENT_RUN",
            target_id="run_1",
            task_id="SAFETY:run_1",
            payload={"risk_report": "위험 없음"},
        )
    )

    assert calls == []


# --- /tasks/translate-executor-draft — 비동기 한국어 번역 ---


def _seed_executor_draft(client, task_id, *, summary_text, anomalies):
    client.data[task_id] = {
        "draft_id": f"drf_{task_id}",
        "agent": "EXECUTOR",
        "target_type": "SETTLEMENT_RUN",
        "target_id": "run_1",
        "task_id": task_id,
        "payload": {"summary_text": summary_text, "anomalies": anomalies},
        "created_at": "2026-08-26T00:00:00Z",
    }


def test_translate_executor_draft_updates_payload_with_korean_fields(monkeypatch, _patch):
    _seed_executor_draft(
        _patch["client"], "EXECUTOR:run_1", summary_text="1 suspected duplicate", anomalies=["anomaly 1"]
    )
    calls = []

    def fake_translate(texts, target_language=None):
        calls.append((texts, target_language))
        return ["중복 의심 1건", "이상징후 1"]

    monkeypatch.setattr(agent_drafts, "translate_lines", fake_translate)

    result = agent_drafts.task_translate_executor_draft(
        {"task_id": "EXECUTOR:run_1", "summary_text": "1 suspected duplicate", "anomalies": ["anomaly 1"]}
    )

    assert result == {"status": "ok", "translated": True}
    assert calls == [(["1 suspected duplicate", "anomaly 1"], "Korean")]
    stored = _patch["client"].data["EXECUTOR:run_1"]
    assert stored["payload"]["summary_text_ko"] == "중복 의심 1건"
    assert stored["payload"]["anomalies_ko"] == ["이상징후 1"]
    # 영어 원본은 그대로 남아 있어야 한다 — update가 다른 필드를 건드리면 안 된다.
    assert stored["payload"]["summary_text"] == "1 suspected duplicate"


def test_translate_executor_draft_translation_failure_leaves_payload_untouched(monkeypatch, _patch):
    _seed_executor_draft(_patch["client"], "EXECUTOR:run_1", summary_text="1 suspected duplicate", anomalies=[])
    monkeypatch.setattr(agent_drafts, "translate_lines", lambda texts, target_language=None: None)

    result = agent_drafts.task_translate_executor_draft(
        {"task_id": "EXECUTOR:run_1", "summary_text": "1 suspected duplicate", "anomalies": []}
    )

    assert result == {"status": "ok", "translated": False}
    stored = _patch["client"].data["EXECUTOR:run_1"]
    assert "summary_text_ko" not in stored["payload"]


def test_translate_executor_draft_missing_draft_is_ignored(monkeypatch, _patch):
    monkeypatch.setattr(agent_drafts, "translate_lines", lambda texts, target_language=None: ["요약"])

    result = agent_drafts.task_translate_executor_draft(
        {"task_id": "EXECUTOR:missing", "summary_text": "summary", "anomalies": []}
    )

    assert result == {"status": "ignored", "reason": "draft_not_found"}


def test_translate_executor_draft_skips_stale_translation_after_reanalysis(monkeypatch, _patch):
    """회귀 테스트 — 이 번역 태스크가 도착하기 전에 같은 task_id로 재분석(재시도
    버튼)이 새 영어 draft를 덮어썼다면, 낡은 번역을 새 영어 내용 위에 잘못
    붙이면 안 된다. 새 draft에는 그 draft 전용 번역 태스크가 이미 따로 돈다."""
    client = _patch["client"]
    _seed_executor_draft(client, "EXECUTOR:run_1", summary_text="OLD summary (superseded)", anomalies=[])
    monkeypatch.setattr(agent_drafts, "translate_lines", lambda texts, target_language=None: ["옛날 요약"])

    # 오래된(태스크 큐에 남아 있던) 번역 요청 — summary_text가 지금 Firestore의
    # 값과 다르다(새 재분석이 이미 덮어씀).
    result = agent_drafts.task_translate_executor_draft(
        {"task_id": "EXECUTOR:run_1", "summary_text": "OLD summary (before retry)", "anomalies": []}
    )

    assert result == {"status": "ignored", "reason": "draft_superseded"}
    stored = client.data["EXECUTOR:run_1"]
    assert "summary_text_ko" not in stored["payload"]
    assert stored["payload"]["summary_text"] == "OLD summary (superseded)"


def test_translate_executor_draft_requires_task_id_and_summary_text(monkeypatch, _patch):
    with pytest.raises(HTTPException) as exc:
        agent_drafts.task_translate_executor_draft({"task_id": "EXECUTOR:run_1"})
    assert exc.value.status_code == 400


def test_translate_executor_draft_missing_authorization_returns_401(monkeypatch, _patch):
    from src.guards.oidc import verify_oidc as real_verify_oidc

    monkeypatch.setattr(agent_drafts, "verify_oidc", real_verify_oidc)
    with pytest.raises(HTTPException) as exc:
        agent_drafts.task_translate_executor_draft(
            {"task_id": "EXECUTOR:run_1", "summary_text": "summary"}, authorization=""
        )
    assert exc.value.status_code == 401
