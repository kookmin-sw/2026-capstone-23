from __future__ import annotations

import json
import os
import socket
from threading import RLock
from typing import Any

from core.env import env_bool, env_str


DEFAULT_PROGRESS_CHANNEL = "jobs:progress"
PROGRESS_ORIGIN_ID = f"{socket.gethostname()}:{os.getpid()}"

_redis_client: Any | None = None
_redis_lock = RLock()


def progress_channel() -> str:
    return env_str("JOB_PROGRESS_REDIS_CHANNEL", DEFAULT_PROGRESS_CHANNEL, strip=True)


def progress_pubsub_enabled() -> bool:
    return env_bool("JOB_PROGRESS_PUBSUB_ENABLED", True)


def progress_origin_id() -> str:
    return PROGRESS_ORIGIN_ID


def _get_redis_client() -> Any | None:
    if not progress_pubsub_enabled():
        return None

    global _redis_client
    with _redis_lock:
        if _redis_client is not None:
            return _redis_client

        try:
            import redis  # type: ignore

            redis_url = env_str("REDIS_URL", "redis://localhost:6379/0")
            client = redis.Redis.from_url(redis_url, decode_responses=True)
            client.ping()
        except Exception:  # noqa: BLE001
            return None

        _redis_client = client
        return _redis_client


def publish_progress_event(payload: dict[str, Any]) -> bool:
    client = _get_redis_client()
    if client is None:
        return False

    event_payload = dict(payload)
    event_payload.setdefault("_progressOriginId", PROGRESS_ORIGIN_ID)
    try:
        client.publish(
            progress_channel(),
            json.dumps(event_payload, ensure_ascii=False, separators=(",", ":")),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def create_progress_pubsub() -> Any | None:
    client = _get_redis_client()
    if client is None:
        return None
    return client.pubsub(ignore_subscribe_messages=True)
