"""저장소 경계를 Protocol로 끊어 로컬 임시 폴더로도 파이프라인 전체가 돌아가게
한다. GCS 구현체는 Task 10에서 붙인다 — 여기서는 로컬 구현체와 **키 규칙**만
고정한다. 키 함수를 두 구현체가 공유하므로 갈아끼워도 오브젝트 경로가 안 바뀌고,
테스트는 계속 로컬 폴더만 쓴다(실제 GCS에 붙지 않는다)."""

import pytest

from src.parsing import storage


def test_local_store_writes_bytes_and_returns_file_uri(tmp_path):
    store = storage.LocalObjectStore(tmp_path)
    uri = store.put(key="images/rct_1.jpg", data=b"\xff\xd8jpeg", content_type="image/jpeg")

    assert uri.startswith("file://")
    assert (tmp_path / "images" / "rct_1.jpg").read_bytes() == b"\xff\xd8jpeg"


def test_local_store_creates_nested_directories(tmp_path):
    store = storage.LocalObjectStore(tmp_path)
    store.put(key="a/b/c/d.txt", data=b"x", content_type="text/plain")
    assert (tmp_path / "a" / "b" / "c" / "d.txt").exists()


def test_local_store_overwrites_on_retry(tmp_path):
    """Cloud Tasks 재시도로 같은 receipt_id가 두 번 돌 수 있다. 키가 receipt_id
    기준이라 결정론적이므로 두 번째 쓰기가 첫 번째를 덮어써야 하고, 예외가 나면
    안 된다 — 멱등성 요구가 저장소 계층에도 걸린다."""
    store = storage.LocalObjectStore(tmp_path)
    store.put(key="images/rct_1.jpg", data=b"first", content_type="image/jpeg")
    uri = store.put(key="images/rct_1.jpg", data=b"second", content_type="image/jpeg")
    assert (tmp_path / "images" / "rct_1.jpg").read_bytes() == b"second"
    assert uri.startswith("file://")


def test_factory_returns_local_store_when_no_bucket_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("GCS_RECEIPTS_BUCKET", raising=False)
    monkeypatch.setenv("LOCAL_RECEIPTS_DIR", str(tmp_path))
    assert isinstance(storage.get_object_store(), storage.LocalObjectStore)


def test_keys_are_deterministic_per_receipt():
    """C가 확정한 키 규칙(2026-08-19). 로컬·GCS 구현체가 같은 함수를 쓴다."""
    assert storage.image_key("rct_1", "jpg") == "images/rct_1.jpg"
    assert storage.raw_text_key("rct_1") == "raw_text/rct_1.txt"


BUCKET = "payflow-hackathon-2026-receipts"


class FakeBlob:
    def __init__(self, name, uploads):
        self.name, self._uploads = name, uploads

    def upload_from_string(self, data, content_type):
        # GCS 오브젝트 쓰기는 기본이 덮어쓰기다. 같은 키를 두 번 올려도 예외가
        # 나지 않고 마지막 값이 남는다 — 재시도 멱등성이 여기 걸려 있다.
        self._uploads.append({"name": self.name, "data": data, "content_type": content_type})


class FakeBucket:
    def __init__(self, uploads):
        self._uploads = uploads

    def blob(self, name):
        return FakeBlob(name, self._uploads)


@pytest.fixture
def fake_bucket(monkeypatch):
    """`_get_bucket`을 통째로 갈아끼운다 — gcs.Client()가 생성되지 않으므로
    테스트가 ADC를 찾거나 네트워크로 나가지 않는다."""
    uploads = []
    monkeypatch.setattr(storage, "_get_bucket", lambda bucket_name: FakeBucket(uploads))
    return uploads


def test_gcs_store_returns_gs_uri(fake_bucket):
    store = storage.GcsObjectStore(BUCKET)
    uri = store.put(key="images/rct_1.jpg", data=b"img", content_type="image/jpeg")

    assert uri == f"gs://{BUCKET}/images/rct_1.jpg"
    assert fake_bucket[-1] == {"name": "images/rct_1.jpg", "data": b"img", "content_type": "image/jpeg"}


def test_gcs_store_uses_same_keys_as_local_store(fake_bucket):
    """구현체를 갈아끼워도 경로가 안 바뀐다 — 키 함수가 한 곳이기 때문이다."""
    store = storage.GcsObjectStore(BUCKET)
    image_uri = store.put(key=storage.image_key("rct_1", "png"), data=b"i", content_type="image/png")
    text_uri = store.put(key=storage.raw_text_key("rct_1"), data=b"t", content_type="text/plain")

    assert image_uri == f"gs://{BUCKET}/images/rct_1.png"
    assert text_uri == f"gs://{BUCKET}/raw_text/rct_1.txt"


def test_gcs_retry_overwrites_same_object(fake_bucket):
    """Cloud Tasks 재시도. 같은 receipt_id는 같은 오브젝트를 덮어써야 하고,
    두 번째 URI가 첫 번째와 같아야 한다 — 다르면 고아 오브젝트가 쌓인다."""
    store = storage.GcsObjectStore(BUCKET)
    first = store.put(key=storage.image_key("rct_1", "jpg"), data=b"first", content_type="image/jpeg")
    second = store.put(key=storage.image_key("rct_1", "jpg"), data=b"second", content_type="image/jpeg")

    assert first == second
    assert [u["name"] for u in fake_bucket] == ["images/rct_1.jpg", "images/rct_1.jpg"]
    assert fake_bucket[-1]["data"] == b"second"


def test_factory_returns_gcs_store_when_bucket_configured(monkeypatch, fake_bucket):
    monkeypatch.setenv("GCS_RECEIPTS_BUCKET", BUCKET)
    store = storage.get_object_store()
    assert isinstance(store, storage.GcsObjectStore)
    assert store.put(key="images/rct_1.jpg", data=b"x", content_type="image/jpeg").startswith(f"gs://{BUCKET}/")
