from typing import Any, Dict, MutableMapping, Optional

from core.config import SUPPORTED_EXTENSIONS
from core.env import env_csv, env_str
from infra.store_maps import (
    _BaseStoreMap,
    _HybridStoreMap,
    _MemoryStoreMap,
    _RedisStoreMap,
    _SqliteStoreMap,
)
from storage.sqlite_kv import describe_store_target


_DEFAULT_STATUS_CACHE_NAMESPACES = (
    "jobs,job_items,job_events,documents,document_cache,"
    "qwen_preprocess_tasks,qwen_infer_tasks,qwen_infer_results,qwen_finalize_tasks,worker_leases"
)


def _build_redis_map(
    namespace: str,
    *,
    prefix_env: str,
    default_prefix: str,
    strict_decode: bool,
) -> _RedisStoreMap:
    import redis  # type: ignore

    redis_url = env_str("REDIS_URL", "redis://localhost:6379/0")
    redis_prefix = env_str(prefix_env, default_prefix)
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.ping()
    redis_hash_key = f"{redis_prefix}:{namespace}"
    return _RedisStoreMap(client, redis_hash_key, strict_decode=strict_decode)


def _build_primary_store(namespace: str) -> _BaseStoreMap:
    backend = env_str("STORE_BACKEND", "sqlite", strip=True).lower() or "sqlite"

    if backend == "memory":
        print(f"[store] backend=memory, namespace={namespace}")
        return _MemoryStoreMap()

    if backend == "redis":
        try:
            store = _build_redis_map(
                namespace,
                prefix_env="REDIS_STORE_PREFIX",
                default_prefix="jobs:store",
                strict_decode=False,
            )
            print(
                f"[store] backend=redis, namespace={namespace}, "
                f"key={store._hash_key}, url={env_str('REDIS_URL', 'redis://localhost:6379/0')}"
            )
            return store
        except Exception as exc:  # noqa: BLE001
            print(f"[store] redis init failed ({exc}); fallback to sqlite store: {namespace}")

    path = describe_store_target()
    print(f"[store] backend=sqlite, namespace={namespace}, path={path}")
    return _SqliteStoreMap(namespace)


def _build_status_cache(namespace: str, primary: _BaseStoreMap) -> Optional[_RedisStoreMap]:
    cache_backend = env_str("STATUS_CACHE_BACKEND", "", strip=True).lower()
    if cache_backend != "redis":
        return None

    if not isinstance(primary, _SqliteStoreMap):
        return None

    cache_namespaces = env_csv("STATUS_CACHE_NAMESPACES", _DEFAULT_STATUS_CACHE_NAMESPACES)
    if namespace not in cache_namespaces:
        return None

    try:
        cache = _build_redis_map(
            namespace,
            prefix_env="REDIS_STATUS_PREFIX",
            default_prefix="jobs:status",
            strict_decode=True,
        )
        print(
            f"[store] status-cache=redis, namespace={namespace}, "
            f"key={cache._hash_key}, url={env_str('REDIS_URL', 'redis://localhost:6379/0')}"
        )
        return cache
    except Exception as exc:  # noqa: BLE001
        print(f"[store] status cache init failed ({exc}); cache disabled: {namespace}")
        return None


def _build_store(namespace: str) -> MutableMapping[str, Dict[str, Any]]:
    primary = _build_primary_store(namespace)
    cache = _build_status_cache(namespace, primary)
    if cache is None:
        return primary
    return _HybridStoreMap(primary, cache)


JOBS: MutableMapping[str, Dict[str, Any]] = _build_store("jobs")
JOB_ITEMS: MutableMapping[str, Dict[str, Any]] = _build_store("job_items")
JOB_EVENTS: MutableMapping[str, Dict[str, Any]] = _build_store("job_events")
DOCUMENTS: MutableMapping[str, Dict[str, Any]] = _build_store("documents")
DOCUMENT_CACHE: MutableMapping[str, Dict[str, Any]] = _build_store("document_cache")
QWEN_PREPROCESS_TASKS: MutableMapping[str, Dict[str, Any]] = _build_store("qwen_preprocess_tasks")
QWEN_INFER_TASKS: MutableMapping[str, Dict[str, Any]] = _build_store("qwen_infer_tasks")
QWEN_INFER_RESULTS: MutableMapping[str, Dict[str, Any]] = _build_store("qwen_infer_results")
QWEN_FINALIZE_TASKS: MutableMapping[str, Dict[str, Any]] = _build_store("qwen_finalize_tasks")
WORKER_LEASES: MutableMapping[str, Dict[str, Any]] = _build_store("worker_leases")
RAG_SESSIONS: MutableMapping[str, Dict[str, Any]] = _build_store("rag_sessions")
USERS: MutableMapping[str, Dict[str, Any]] = _build_store("users")
AUTH_SESSIONS: MutableMapping[str, Dict[str, Any]] = _build_store("auth_sessions")
ADMIN_SETTINGS: MutableMapping[str, Dict[str, Any]] = _build_store("admin_settings")
AUDIT_LOGS: MutableMapping[str, Dict[str, Any]] = _build_store("audit_logs")

SUPPORTED_TYPES = sorted(SUPPORTED_EXTENSIONS | {".tif", ".webp"})
