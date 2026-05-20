from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from core.jobs.worker_lease_service import (
    acquire_lease,
    qwen_worker_heartbeat_interval_seconds,
    qwen_worker_lease_ttl_seconds,
    release_lease,
    renew_lease,
)


class LeaseHeartbeat:
    def __init__(
        self,
        *,
        kind: str,
        target_id: str,
        worker_id: str,
        job_id: Optional[str] = None,
        job_item_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
        heartbeat_interval_seconds: Optional[int] = None,
    ) -> None:
        self.kind = kind
        self.target_id = target_id
        self.worker_id = worker_id
        self.job_id = job_id
        self.job_item_id = job_item_id
        self.metadata = dict(metadata or {})
        self.ttl_seconds = ttl_seconds or qwen_worker_lease_ttl_seconds()
        self.heartbeat_interval_seconds = heartbeat_interval_seconds or qwen_worker_heartbeat_interval_seconds()
        self.attempt_id = ""
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "LeaseHeartbeat":
        lease = acquire_lease(
            kind=self.kind,
            target_id=self.target_id,
            worker_id=self.worker_id,
            job_id=self.job_id,
            job_item_id=self.job_item_id,
            metadata=self.metadata,
            ttl_seconds=self.ttl_seconds,
        )
        self.attempt_id = str(lease["attemptId"])
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(1.0, float(self.heartbeat_interval_seconds)))
        if self.attempt_id:
            release_lease(self.kind, self.target_id, self.attempt_id)

    def _run(self) -> None:
        while not self._stop_event.wait(self.heartbeat_interval_seconds):
            if not renew_lease(self.kind, self.target_id, self.attempt_id, ttl_seconds=self.ttl_seconds):
                return
