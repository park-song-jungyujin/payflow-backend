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

from google.cloud import storage as gcs


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


_bucket_cache: dict[str, "gcs.Bucket"] = {}


def _get_bucket(bucket_name: str) -> "gcs.Bucket":
    """클라이언트 생성을 이 함수 하나로 모은다. 두 가지를 동시에 해결한다:
    요청마다 클라이언트를 새로 만들지 않는 것(payouts/store.py의 get_client와 같은
    이유), 그리고 테스트가 여기만 갈아끼우면 gcs.Client()가 아예 생성되지 않아
    ADC를 찾거나 네트워크로 나가지 않는 것.
    """
    if bucket_name not in _bucket_cache:
        _bucket_cache[bucket_name] = gcs.Client(project=os.environ.get("GCP_PROJECT")).bucket(bucket_name)
    return _bucket_cache[bucket_name]


class GcsObjectStore:
    """LocalObjectStore와 시그니처가 같다. 갈아끼우는 게 이 클래스의 전부다.

    버킷: payflow-hackathon-2026-receipts (asia-northeast3). 키는 image_key/
    raw_text_key가 만들고 receipt_id 기준이라 결정론적이다 — Cloud Tasks
    재시도가 같은 오브젝트를 덮어쓴다. GCS 오브젝트 쓰기는 기본이 덮어쓰기라
    별도 처리가 필요 없다. 세대 조건(`if_generation_match`)을 걸면 오히려
    재시도가 412로 죽는다.
    """

    def __init__(self, bucket_name: str):
        self._bucket_name = bucket_name

    def put(self, *, key: str, data: bytes, content_type: str) -> str:
        _get_bucket(self._bucket_name).blob(key).upload_from_string(data, content_type=content_type)
        return f"gs://{self._bucket_name}/{key}"


def get_object_store() -> ObjectStore:
    bucket = os.environ.get("GCS_RECEIPTS_BUCKET")
    if bucket:
        return GcsObjectStore(bucket)
    root = os.environ.get("LOCAL_RECEIPTS_DIR") or str(Path(tempfile.gettempdir()) / "payflow-receipts")
    return LocalObjectStore(Path(root))


def image_key(receipt_id: str, ext: str) -> str:
    return f"images/{receipt_id}.{ext}"


def raw_text_key(receipt_id: str) -> str:
    return f"raw_text/{receipt_id}.txt"
