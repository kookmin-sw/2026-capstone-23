import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("STATUS_CACHE_BACKEND", "none")

from infra.store_maps import _HybridStoreMap, _MemoryStoreMap, _RedisStoreMap


class FakeRedis:
    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._strings: dict[str, str] = {}

    def ping(self) -> bool:
        return True

    def hget(self, key: str, field: str):
        return self._hashes.get(key, {}).get(field)

    def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def hdel(self, key: str, field: str) -> int:
        bucket = self._hashes.get(key, {})
        if field not in bucket:
            return 0
        del bucket[field]
        return 1

    def hkeys(self, key: str):
        return list(self._hashes.get(key, {}).keys())

    def hgetall(self, key: str):
        return dict(self._hashes.get(key, {}))

    def hlen(self, key: str) -> int:
        return len(self._hashes.get(key, {}))

    def get(self, key: str):
        return self._strings.get(key)

    def set(self, key: str, value: str) -> None:
        self._strings[key] = value

    def delete(self, key: str) -> None:
        self._hashes.pop(key, None)
        self._strings.pop(key, None)


class FailingRedis(FakeRedis):
    def _fail(self):
        raise ConnectionError("redis unavailable")

    def hget(self, key: str, field: str):
        self._fail()

    def hset(self, key: str, field: str, value: str) -> None:
        self._fail()

    def hdel(self, key: str, field: str) -> int:
        self._fail()

    def hkeys(self, key: str):
        self._fail()

    def hgetall(self, key: str):
        self._fail()

    def hlen(self, key: str) -> int:
        self._fail()

    def get(self, key: str):
        self._fail()

    def set(self, key: str, value: str) -> None:
        self._fail()

    def delete(self, key: str) -> None:
        self._fail()


class DeleteFailingRedis(FakeRedis):
    def hdel(self, key: str, field: str) -> int:
        raise ConnectionError("redis delete failed")


def test_hybrid_store_writes_to_sqlite_primary_and_redis_cache() -> None:
    primary = _MemoryStoreMap()
    cache = _RedisStoreMap(FakeRedis(), "jobs:status:jobs")
    store = _HybridStoreMap(primary, cache)

    store["j_1"] = {"jobId": "j_1", "status": "QUEUED"}

    assert primary.get("j_1") == {"jobId": "j_1", "status": "QUEUED"}
    assert cache.get("j_1") == {"jobId": "j_1", "status": "QUEUED"}


def test_hybrid_store_reads_through_cache_and_warms_on_full_scan() -> None:
    primary = _MemoryStoreMap()
    cache = _RedisStoreMap(FakeRedis(), "jobs:status:job_items")
    store = _HybridStoreMap(primary, cache)

    primary["ji_1"] = {"jobItemId": "ji_1", "status": "QUEUED"}
    primary["ji_2"] = {"jobItemId": "ji_2", "status": "PROCESSING"}

    # Cache miss should load from the primary store and backfill Redis.
    assert store.get("ji_1") == {"jobItemId": "ji_1", "status": "QUEUED"}
    assert cache.get("ji_1") == {"jobItemId": "ji_1", "status": "QUEUED"}

    # Full scans warm the Redis side so subsequent status listings stay fast.
    values = sorted(store.values(), key=lambda item: item["jobItemId"])
    assert values == [
        {"jobItemId": "ji_1", "status": "QUEUED"},
        {"jobItemId": "ji_2", "status": "PROCESSING"},
    ]
    assert cache.is_warmed() is True
    assert cache.get("ji_2") == {"jobItemId": "ji_2", "status": "PROCESSING"}


def test_hybrid_store_warm_scan_does_not_delete_existing_redis_entries() -> None:
    primary = _MemoryStoreMap()
    cache = _RedisStoreMap(FakeRedis(), "jobs:status:qwen_infer_tasks")
    store = _HybridStoreMap(primary, cache)

    primary["local-task"] = {"taskId": "local-task", "status": "QUEUED"}
    cache["remote-task"] = {"taskId": "remote-task", "status": "PROCESSING"}

    values = sorted(store.values(), key=lambda item: item["taskId"])
    assert values == [
        {"taskId": "local-task", "status": "QUEUED"},
        {"taskId": "remote-task", "status": "PROCESSING"},
    ]
    assert cache.is_warmed() is True


def test_hybrid_store_falls_back_to_primary_when_cache_payload_is_invalid() -> None:
    redis = FakeRedis()
    primary = _MemoryStoreMap()
    cache = _RedisStoreMap(redis, "jobs:status:jobs")
    store = _HybridStoreMap(primary, cache)

    primary["j_1"] = {"jobId": "j_1", "status": "QUEUED"}
    redis.hset("jobs:status:jobs", "stale", "not-json")
    cache.mark_warmed()

    assert store.values() == [{"jobId": "j_1", "status": "QUEUED"}]
    assert store.get("stale") is None


def test_hybrid_store_cache_outage_does_not_break_primary_reads_or_writes() -> None:
    primary = _MemoryStoreMap()
    cache = _RedisStoreMap(FailingRedis(), "jobs:status:documents")
    store = _HybridStoreMap(primary, cache)

    store["d_1"] = {"documentId": "d_1", "latestStatus": "COMPLETED"}

    assert primary.get("d_1") == {"documentId": "d_1", "latestStatus": "COMPLETED"}
    assert store.get("d_1") == {"documentId": "d_1", "latestStatus": "COMPLETED"}
    assert store.values() == [{"documentId": "d_1", "latestStatus": "COMPLETED"}]
    assert len(store) == 1


def test_hybrid_store_ignores_stale_cache_after_failed_cache_delete() -> None:
    redis = DeleteFailingRedis()
    primary = _MemoryStoreMap()
    cache = _RedisStoreMap(redis, "jobs:status:documents")
    store = _HybridStoreMap(primary, cache)

    store["d_1"] = {"documentId": "d_1", "latestStatus": "QUEUED"}
    assert store.pop("d_1") == {"documentId": "d_1", "latestStatus": "QUEUED"}

    assert primary.get("d_1") is None
    assert cache.get("d_1") == {"documentId": "d_1", "latestStatus": "QUEUED"}
    assert store.get("d_1") is None
    assert store.values() == []
    assert cache.get("d_1") is None


def test_redis_primary_store_discards_invalid_payloads_without_raising() -> None:
    redis = FakeRedis()
    store = _RedisStoreMap(redis, "jobs:store:jobs", strict_decode=False)

    redis.hset("jobs:store:jobs", "bad-json", "not-json")
    redis.hset("jobs:store:jobs", "bad-shape", '"not-object"')
    store["j_1"] = {"jobId": "j_1", "status": "QUEUED"}

    assert store.get("bad-json") is None
    assert store.values() == [{"jobId": "j_1", "status": "QUEUED"}]
    assert redis.hget("jobs:store:jobs", "bad-json") is None
    assert redis.hget("jobs:store:jobs", "bad-shape") is None
