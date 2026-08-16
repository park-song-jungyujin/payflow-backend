"""schema-contract.md §12 — 데모 fixture 8종을 로드해 stub 라우트에 제공한다.

fixture는 `tests/fixtures/`에 한 벌만 두고 stub과 최종 데모가 같이 쓴다.
Firestore가 아직 안 붙어 있어, 여기서 로드한 dict를 프로세스 메모리 안의 임시
저장소로도 쓴다 — `guards`가 승인 시 이 dict를 그대로 mutate한다. 재시작하면
fixture 원본 값으로 돌아간다.
"""

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

_SUB_CASE_KEYS = ("sub_case_a_cap_exceeded_at_approve", "sub_case_b_payouts_without_token")


def _load_all() -> tuple[dict[str, dict], dict[str, list[dict]], dict[str, dict], dict[str, dict]]:
    runs: dict[str, dict] = {}
    sender_items: dict[str, list[dict]] = {}
    claims: dict[str, dict] = {}
    recipients: dict[str, dict] = {}

    for path in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(path.read_text())

        blocks = [data] + [data[key] for key in _SUB_CASE_KEYS if key in data]
        for block in blocks:
            for run in block.get("settlement_runs", []):
                runs[run["settlement_run_id"]] = run
            for claim in block.get("claims", []):
                claims[claim["claim_id"]] = claim

        for rcp in data.get("recipients", []):
            recipients[rcp["recipient_id"]] = rcp

        for item in data.get("sender_items", []):
            sender_items.setdefault(item["settlement_run_id"], []).append(item)

    return runs, sender_items, claims, recipients


_RUNS, _SENDER_ITEMS, _CLAIMS, _RECIPIENTS = _load_all()


def get_settlement_run(run_id: str) -> dict | None:
    return _RUNS.get(run_id)


def get_sender_items(run_id: str) -> list[dict]:
    return _SENDER_ITEMS.get(run_id, [])


def set_sender_items(run_id: str, items: list[dict]) -> None:
    """execute-payout이 PayPal 호출 결과로 만든 sender_items를 저장한다. Firestore가
    붙기 전까지 이 프로세스 메모리가 유일한 저장소다."""
    _SENDER_ITEMS[run_id] = items


def get_claims_for_run(run_id: str) -> list[dict]:
    return [c for c in _CLAIMS.values() if c.get("settlement_run_id") == run_id]


def get_recipient(recipient_id: str) -> dict | None:
    return _RECIPIENTS.get(recipient_id)
