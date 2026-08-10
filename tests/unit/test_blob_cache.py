import pytest

from jmap_bridge.blob_cache import BlobCache


def test_put_get_round_trip():
    cache = BlobCache()
    blob_id = cache.put(b"hello world", "text/plain")
    data, content_type = cache.get(blob_id)
    assert data == b"hello world"
    assert content_type == "text/plain"


def test_unknown_blob_id_returns_none():
    cache = BlobCache()
    assert cache.get("Unotreal") is None


def test_blob_ids_are_unique():
    cache = BlobCache()
    id1 = cache.put(b"a", "text/plain")
    id2 = cache.put(b"b", "text/plain")
    assert id1 != id2


def test_expiry(monkeypatch):
    import jmap_bridge.blob_cache as blob_cache_module

    fake_now = [1000.0]
    monkeypatch.setattr(blob_cache_module.time, "monotonic", lambda: fake_now[0])

    cache = BlobCache(ttl_seconds=10)
    blob_id = cache.put(b"data", "text/plain")
    assert cache.get(blob_id) is not None

    fake_now[0] += 11
    assert cache.get(blob_id) is None


def test_capacity_exceeded_raises():
    cache = BlobCache(max_total_bytes=10)
    cache.put(b"1234567890", "text/plain")  # exactly at capacity
    with pytest.raises(MemoryError):
        cache.put(b"x", "text/plain")
