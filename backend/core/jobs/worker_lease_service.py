from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Dict, Optional

from core.env import env_int
from core.jobs.service import parse_iso8601, utc_now
from core.time import now_iso
from infra.store import WORKER_LEASES


def qwen_worker_lease_ttl_seconds() -> int:
    return env_int("QWEN_WORKER_LEASE_TTL_SECONDS", 3600, minimum=1)


def qwen_worker_heartbeat_interval_seconds() -> int:
    ttl = qwen_worker_lease_ttl_seconds()
    suggested = max(5, min(60, ttl // 6))
    return env_int("QWEN_WORKER_HEARTBEAT_INTERVAL_SECONDS", suggested, minimum=1)


def lease_key(kind: str, target_id: str) -> str:
    return f"{kind}:{target_id}"


def _expires_at_iso(ttl_seconds: int) -> str:
    return (utc_now() + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def acquire_lease(
    *,
    kind: str,
    target_id: str,
    worker_id: str,
    job_id: Optional[str] = None,
    job_item_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    ttl_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    now = now_iso()
    ttl = ttl_seconds or qwen_worker_lease_ttl_seconds()
    lease_id = lease_key(kind, target_id)
    attempt_id = f"lease_{uuid.uuid4().hex[:12]}"
    WORKER_LEASES[lease_id] = {
        "leaseId": lease_id,
        "kind": kind,
        "targetId": target_id,
        "attemptId": attempt_id,
        "workerId": worker_id,
        "jobId": job_id,
        "jobItemId": job_item_id,
        "metadata": dict(metadata or {}),
        "createdAt": now,
        "lastHeartbeatAt": now,
        "leaseExpiresAt": _expires_at_iso(ttl),
        "updatedAt": now,
    }
    return WORKER_LEASES[lease_id]


def get_lease(kind: str, target_id: str) -> Optional[Dict[str, Any]]:
    return WORKER_LEASES.get(lease_key(kind, target_id))


def lease_is_active(lease: Optional[Dict[str, Any]], *, now=None) -> bool:
    if not lease:
        return False
    expires_at = parse_iso8601(str(lease.get("leaseExpiresAt") or ""))
    if expires_at is None:
        return False
    current_time = now or utc_now()
    return expires_at > current_time


def renew_lease(kind: str, target_id: str, attempt_id: str, *, ttl_seconds: Optional[int] = None) -> bool:
    lease = get_lease(kind, target_id)
    if not lease or str(lease.get("attemptId") or "") != attempt_id:
        return False

    now = now_iso()
    ttl = ttl_seconds or qwen_worker_lease_ttl_seconds()
    lease["lastHeartbeatAt"] = now
    lease["leaseExpiresAt"] = _expires_at_iso(ttl)
    lease["updatedAt"] = now
    return True


def release_lease(kind: str, target_id: str, attempt_id: str) -> bool:
    lease_id = lease_key(kind, target_id)
    lease = WORKER_LEASES.get(lease_id)
    if not lease or str(lease.get("attemptId") or "") != attempt_id:
        return False
    WORKER_LEASES.pop(lease_id, None)
    return True
