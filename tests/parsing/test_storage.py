"""저장소 경계를 Protocol로 끊어 로컬 임시 폴더로도 파이프라인 전체가 돌아가게
한다. GCS 구현체는 Task 10에서 붙인다 — 여기서는 로컬 구현체와 **키 규칙**만
고정한다. 키 함수를 두 구현체가 공유하므로 갈아끼워도 오브젝트 경로가 안 바뀌고,
테스트는 계속 로컬 폴더만 쓴다(실제 GCS에 붙지 않는다)."""

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
