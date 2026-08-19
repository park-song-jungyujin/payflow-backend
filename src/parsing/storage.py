"""영수증 원본과 파싱 원문이 나가는 유일한 출구 (A 소유).

schema-contract.md §2: 원본은 GCS에만 두고 Firestore에는 마스킹된 값만 넣는다.
로컬 임시 폴더 구현체로 먼저 돌린다 — 경계를 Protocol로 끊어놨으므로 GCS를
붙일 때 `get_object_store`의 분기 하나와 구현체 클래스만 늘고 파이프라인은
그대로다. 테스트가 실제 GCS에 안 붙는 것도 이 경계가 보장한다.

**키는 receipt_id 기준이라 결정론적이다.** Cloud Tasks 재시도가 같은 경로를
덮어써서 멱등이 된다 — 호출마다 새 경로를 만들면 고아 오브젝트가 쌓이고,
image_gcs_uri가 어느 객체를 가리키는지도 불안정해진다.
"""

import os
import tempfile
from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, *, key: str, data: bytes, content_type: str) -> str:
        """저장하고 URI를 돌려준다. 같은 key로 다시 부르면 덮어쓴다 —
        Cloud Tasks 재시도가 예외로 죽으면 안 된다."""
        ...


class LocalObjectStore:
    """버킷이 오기 전까지 쓰는 구현체. content_type은 받아두고 쓰지 않는다 —
    로컬 파일시스템에는 얹을 자리가 없고, 시그니처는 GCS 구현체와 맞춰야 한다."""

    def __init__(self, root: Path):
        self._root = Path(root)

    def put(self, *, key: str, data: bytes, content_type: str) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.resolve().as_uri()


def get_object_store() -> ObjectStore:
    root = os.environ.get("LOCAL_RECEIPTS_DIR") or str(Path(tempfile.gettempdir()) / "payflow-receipts")
    return LocalObjectStore(Path(root))


def image_key(receipt_id: str, ext: str) -> str:
    return f"images/{receipt_id}.{ext}"


def raw_text_key(receipt_id: str) -> str:
    return f"raw_text/{receipt_id}.txt"
