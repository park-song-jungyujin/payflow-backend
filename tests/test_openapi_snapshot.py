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
