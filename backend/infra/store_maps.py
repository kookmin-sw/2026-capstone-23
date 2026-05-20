import json
from threading import RLock
from typing import Any, Callable, Dict, Iterable, Iterator, MutableMapping, Optional

from storage.sqlite_kv import (
    clear_namespace,
    count_entries,
    delete_entry,
    list_json_entries,
    list_keys,
    load_json_entry,
    save_json_entry,
)


def _to_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    return value


def _wrap_tracked(value: Any, on_change: Callable[[], None]) -> Any:
    if isinstance(value, _TrackedDict):
        return value
    if isinstance(value, dict):
        return _TrackedDict(value, on_change)
    if isinstance(value, _TrackedList):
        return value
    if isinstance(value, list):
        return _TrackedList(value, on_change)
    return value


class _TrackedDict(dict):
    def __init__(self, data: Dict[str, Any], on_change: Callable[[], None]) -> None:
        super().__init__()
        self._on_change = on_change
        for key, value in data.items():
            super().__setitem__(key, _wrap_tracked(value, on_change))

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, _wrap_tracked(value, self._on_change))
        self._on_change()

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self._on_change()

    def clear(self) -> None:
        super().clear()
        self._on_change()

    def pop(self, key: str, default: Any = None) -> Any:
        exists = key in self
        value = super().pop(key, default)
        if exists:
            self._on_change()
        return value

    def popitem(self) -> Any:
        value = super().popitem()
        self._on_change()
        return value

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key in self:
            return super().get(key)
        wrapped = _wrap_tracked(default, self._on_change)
        super().__setitem__(key, wrapped)
        self._on_change()
        return wrapped

    def update(self, *args: Any, **kwargs: Any) -> None:
        updates = dict(*args, **kwargs)
        for key, value in updates.items():
            super().__setitem__(key, _wrap_tracked(value, self._on_change))
        if updates:
            self._on_change()


class _TrackedList(list):
    def __init__(self, data: list[Any], on_change: Callable[[], None]) -> None:
        super().__init__(_wrap_tracked(v, on_change) for v in data)
        self._on_change = on_change

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            wrapped = [_wrap_tracked(v, self._on_change) for v in value]
            super().__setitem__(index, wrapped)
        else:
            super().__setitem__(index, _wrap_tracked(value, self._on_change))
        self._on_change()

    def __delitem__(self, index: Any) -> None:
        super().__delitem__(index)
        self._on_change()

    def append(self, value: Any) -> None:
        super().append(_wrap_tracked(value, self._on_change))
        self._on_change()

    def extend(self, values: Iterable[Any]) -> None:
        super().extend(_wrap_tracked(v, self._on_change) for v in values)
        self._on_change()

    def insert(self, index: int, value: Any) -> None:
        super().insert(index, _wrap_tracked(value, self._on_change))
        self._on_change()

    def remove(self, value: Any) -> None:
        super().remove(value)
        self._on_change()

    def pop(self, index: int = -1) -> Any:
        value = super().pop(index)
        self._on_change()
        return value

    def clear(self) -> None:
        super().clear()
        self._on_change()

    def reverse(self) -> None:
        super().reverse()
        self._on_change()

    def sort(self, *args: Any, **kwargs: Any) -> None:
        super().sort(*args, **kwargs)
        self._on_change()

    def __iadd__(self, other: Iterable[Any]) -> "_TrackedList":
        self.extend(other)
        return self

    def __imul__(self, count: int) -> "_TrackedList":
        super().__imul__(count)
        self._on_change()
        return self


class _BaseStoreMap(MutableMapping[str, Dict[str, Any]]):
    def __getitem__(self, key: str) -> Dict[str, Any]:
        raw = self._read(key)
        if raw is None:
            raise KeyError(key)
        return self._tracked(key, raw)

    def __setitem__(self, key: str, value: Dict[str, Any]) -> None:
        self._write(key, _to_plain(value))

    def __delitem__(self, key: str) -> None:
        if not self._delete(key):
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys())

    def __len__(self) -> int:
        return self._len()

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return self._read(key) is not None

    def get(self, key: str, default: Any = None) -> Any:
        raw = self._read(key)
        if raw is None:
            return default
        return self._tracked(key, raw)

    def items(self) -> list[tuple[str, Dict[str, Any]]]:
        return [(key, self._tracked(key, raw)) for key, raw in self._iter_raw()]

    def values(self) -> list[Dict[str, Any]]:
        return [self._tracked(key, raw) for key, raw in self._iter_raw()]

    def pop(self, key: str, default: Any = None) -> Any:
        raw = self._read(key)
        if raw is None:
            return default
        self._delete(key)
        return _to_plain(raw)

    def clear(self) -> None:
        self._clear()

    def _tracked(self, key: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        def _persist() -> None:
            self._write(key, _to_plain(tracked))

        tracked = _TrackedDict(_to_plain(raw), _persist)
        return tracked

    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def _write(self, key: str, value: Dict[str, Any]) -> None:
        raise NotImplementedError

    def _delete(self, key: str) -> bool:
        raise NotImplementedError

    def _keys(self) -> list[str]:
        raise NotImplementedError

    def _iter_raw(self) -> list[tuple[str, Dict[str, Any]]]:
        raise NotImplementedError

    def _len(self) -> int:
        raise NotImplementedError

    def _clear(self) -> None:
        raise NotImplementedError


class _MemoryStoreMap(_BaseStoreMap):
    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()

    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self._data.get(key)
            return _to_plain(value) if value is not None else None

    def _write(self, key: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = _to_plain(value)

    def _delete(self, key: str) -> bool:
        with self._lock:
            if key not in self._data:
                return False
            del self._data[key]
            return True

    def _keys(self) -> list[str]:
        with self._lock:
            return list(self._data.keys())

    def _iter_raw(self) -> list[tuple[str, Dict[str, Any]]]:
        with self._lock:
            return [(key, _to_plain(value)) for key, value in self._data.items()]

    def _len(self) -> int:
        with self._lock:
            return len(self._data)

    def _clear(self) -> None:
        with self._lock:
            self._data.clear()


class _RedisStoreMap(_BaseStoreMap):
    def __init__(
        self,
        redis_client: Any,
        redis_hash_key: str,
        *,
        warm_marker_key: Optional[str] = None,
        strict_decode: bool = True,
    ) -> None:
        self._redis = redis_client
        self._hash_key = redis_hash_key
        self._warm_marker_key = warm_marker_key or f"{redis_hash_key}:warmed"
        self._strict_decode = strict_decode

    def _discard_invalid_entry(self, key: str) -> None:
        try:
            self._redis.hdel(self._hash_key, key)
        except Exception:  # noqa: BLE001
            pass

    def _decode_entry(self, key: str, raw: Any) -> Optional[Dict[str, Any]]:
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            if self._strict_decode:
                raise
            self._discard_invalid_entry(key)
            return None
        if isinstance(decoded, dict):
            return decoded
        if self._strict_decode:
            raise ValueError(f"store entry is not an object: {key}")
        self._discard_invalid_entry(key)
        return None

    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        raw = self._redis.hget(self._hash_key, key)
        if raw is None:
            return None
        return self._decode_entry(key, raw)

    def _write(self, key: str, value: Dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self._redis.hset(self._hash_key, key, payload)

    def _delete(self, key: str) -> bool:
        return bool(self._redis.hdel(self._hash_key, key))

    def _keys(self) -> list[str]:
        keys = self._redis.hkeys(self._hash_key)
        return [str(key) for key in keys]

    def _iter_raw(self) -> list[tuple[str, Dict[str, Any]]]:
        entries = self._redis.hgetall(self._hash_key)
        decoded_entries: list[tuple[str, Dict[str, Any]]] = []
        for key, value in entries.items():
            decoded = self._decode_entry(str(key), value)
            if decoded is None:
                continue
            decoded_entries.append((str(key), decoded))
        return decoded_entries

    def _len(self) -> int:
        return int(self._redis.hlen(self._hash_key))

    def _clear(self) -> None:
        self.clear_all()

    def clear_entries(self) -> None:
        self._redis.delete(self._hash_key)

    def clear_all(self) -> None:
        self._redis.delete(self._hash_key)
        self._redis.delete(self._warm_marker_key)

    def is_warmed(self) -> bool:
        return bool(self._redis.get(self._warm_marker_key))

    def mark_warmed(self) -> None:
        self._redis.set(self._warm_marker_key, "1")

    def replace_all(self, entries: list[tuple[str, Dict[str, Any]]]) -> None:
        payloads = [
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            for key, value in entries
        ]
        pipeline_factory = getattr(self._redis, "pipeline", None)

        if callable(pipeline_factory):
            pipe = self._redis.pipeline()
            pipe.delete(self._hash_key)
            for key, payload in payloads:
                pipe.hset(self._hash_key, key, payload)
            pipe.set(self._warm_marker_key, "1")
            pipe.execute()
            return

        self.clear_entries()
        for key, payload in payloads:
            self._redis.hset(self._hash_key, key, payload)
        self.mark_warmed()

    def merge_all(self, entries: list[tuple[str, Dict[str, Any]]]) -> None:
        payloads = [
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            for key, value in entries
        ]
        if not payloads:
            return

        pipeline_factory = getattr(self._redis, "pipeline", None)
        if callable(pipeline_factory):
            pipe = self._redis.pipeline()
            for key, payload in payloads:
                pipe.hset(self._hash_key, key, payload)
            pipe.execute()
            return

        for key, payload in payloads:
            self._redis.hset(self._hash_key, key, payload)


class _SqliteStoreMap(_BaseStoreMap):
    def __init__(self, namespace: str) -> None:
        self._namespace = namespace

    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        raw = load_json_entry(self._namespace, key)
        return raw if isinstance(raw, dict) else None

    def _write(self, key: str, value: Dict[str, Any]) -> None:
        save_json_entry(self._namespace, key, value)

    def _delete(self, key: str) -> bool:
        return delete_entry(self._namespace, key)

    def _keys(self) -> list[str]:
        return list_keys(self._namespace)

    def _iter_raw(self) -> list[tuple[str, Dict[str, Any]]]:
        return [
            (key, raw)
            for key, raw in list_json_entries(self._namespace)
            if isinstance(raw, dict)
        ]

    def _len(self) -> int:
        return count_entries(self._namespace)

    def _clear(self) -> None:
        clear_namespace(self._namespace)


class _HybridStoreMap(_BaseStoreMap):
    def __init__(self, primary: _BaseStoreMap, cache: _RedisStoreMap) -> None:
        self._primary = primary
        self._cache = cache
        self._warm_lock = RLock()
        self._cache_dirty = False

    def _cache_write_best_effort(
        self,
        key: str,
        value: Dict[str, Any],
        *,
        mark_warmed: bool = True,
    ) -> None:
        try:
            self._cache._write(key, value)
            if mark_warmed:
                self._cache.mark_warmed()
        except Exception:  # noqa: BLE001
            self._cache_dirty = True

    def _cache_delete_best_effort(self, key: str) -> None:
        try:
            self._cache._delete(key)
            self._cache.mark_warmed()
        except Exception:  # noqa: BLE001
            self._cache_dirty = True

    def _cache_clear_best_effort(self) -> None:
        try:
            self._cache.clear_entries()
            self._cache.mark_warmed()
            self._cache_dirty = False
        except Exception:  # noqa: BLE001
            self._cache_dirty = True

    def _ensure_cache_warmed(self) -> bool:
        if not self._cache_dirty:
            try:
                if self._cache.is_warmed():
                    return True
            except Exception:  # noqa: BLE001
                return False

        with self._warm_lock:
            if not self._cache_dirty:
                try:
                    if self._cache.is_warmed():
                        return True
                except Exception:  # noqa: BLE001
                    return False

            primary_entries = self._primary._iter_raw()
            try:
                if self._cache_dirty:
                    self._cache.replace_all(primary_entries)
                elif primary_entries:
                    self._cache.merge_all(primary_entries)
                self._cache.mark_warmed()
                self._cache_dirty = False
                return True
            except Exception:  # noqa: BLE001
                self._cache_dirty = True
                return False

    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        raw = None
        if not self._cache_dirty:
            try:
                raw = self._cache._read(key)
            except Exception:  # noqa: BLE001
                self._cache_dirty = True
        if raw is not None:
            return raw

        raw = self._primary._read(key)
        if raw is None:
            return None

        self._cache_write_best_effort(key, raw, mark_warmed=False)
        return raw

    def _write(self, key: str, value: Dict[str, Any]) -> None:
        self._primary._write(key, value)
        self._cache_write_best_effort(key, value)

    def _delete(self, key: str) -> bool:
        deleted = self._primary._delete(key)
        self._cache_delete_best_effort(key)
        return deleted

    def _keys(self) -> list[str]:
        if self._ensure_cache_warmed():
            try:
                return self._cache._keys()
            except Exception:  # noqa: BLE001
                self._cache_dirty = True
        return self._primary._keys()

    def _iter_raw(self) -> list[tuple[str, Dict[str, Any]]]:
        if self._ensure_cache_warmed():
            try:
                return self._cache._iter_raw()
            except Exception:  # noqa: BLE001
                self._cache_dirty = True
        return self._primary._iter_raw()

    def _len(self) -> int:
        if self._ensure_cache_warmed():
            try:
                return self._cache._len()
            except Exception:  # noqa: BLE001
                self._cache_dirty = True
        return self._primary._len()

    def _clear(self) -> None:
        self._primary._clear()
        self._cache_clear_best_effort()
