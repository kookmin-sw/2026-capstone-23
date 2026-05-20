import asyncio
import json
import threading
import time
from threading import Lock
from typing import Any, Dict, Optional

from fastapi import WebSocket

from api.common import ok
from infra.progress_events import create_progress_pubsub, progress_channel, progress_origin_id


class JobWebSocketHub:
    def __init__(self) -> None:
        self._connections: Dict[WebSocket, Optional[str]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = Lock()

    async def register(self, websocket: WebSocket, job_id: Optional[str] = None) -> None:
        with self._lock:
            self._connections[websocket] = job_id
            current_loop = asyncio.get_running_loop()
            if self._loop is None or self._loop.is_closed():
                self._loop = current_loop

    async def unregister(self, websocket: WebSocket) -> None:
        with self._lock:
            self._connections.pop(websocket, None)

    def publish(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            loop = self._loop
            if loop is None or loop.is_closed() or not self._connections:
                return
            payload_job_id = payload.get("jobId")
            targets = [
                websocket
                for websocket, subscribed_job_id in self._connections.items()
                if subscribed_job_id is None or subscribed_job_id == payload_job_id
            ]
            if not targets:
                return
        asyncio.run_coroutine_threadsafe(self._broadcast(targets, payload), loop)

    async def _broadcast(self, targets: list[WebSocket], payload: Dict[str, Any]) -> None:
        disconnected: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_json(ok(payload))
            except Exception:  # noqa: BLE001
                disconnected.append(websocket)
        if disconnected:
            with self._lock:
                for websocket in disconnected:
                    self._connections.pop(websocket, None)


JOB_WS_HUB = JobWebSocketHub()


def publish_job_progress(payload: Dict[str, Any]) -> None:
    JOB_WS_HUB.publish(payload)


class JobProgressRedisBridge:
    def __init__(self, hub: JobWebSocketHub) -> None:
        self._hub = hub
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pubsub: Any | None = None
        self._lock = Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="job-progress-redis-bridge",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        pubsub = self._pubsub
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:  # noqa: BLE001
                pass
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        channel = progress_channel()
        while not self._stop_event.is_set():
            pubsub = create_progress_pubsub()
            if pubsub is None:
                time.sleep(2.0)
                continue

            self._pubsub = pubsub
            try:
                pubsub.subscribe(channel)
                while not self._stop_event.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if not message or message.get("type") != "message":
                        continue
                    payload = self._decode_payload(message.get("data"))
                    if payload is not None:
                        if payload.get("_progressOriginId") == progress_origin_id():
                            continue
                        payload.pop("_progressOriginId", None)
                        self._hub.publish(payload)
            except Exception:  # noqa: BLE001
                if not self._stop_event.is_set():
                    time.sleep(1.0)
            finally:
                try:
                    pubsub.close()
                except Exception:  # noqa: BLE001
                    pass
                self._pubsub = None

    @staticmethod
    def _decode_payload(raw: Any) -> Dict[str, Any] | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None


JOB_PROGRESS_REDIS_BRIDGE = JobProgressRedisBridge(JOB_WS_HUB)


def start_job_progress_bridge() -> None:
    JOB_PROGRESS_REDIS_BRIDGE.start()


def stop_job_progress_bridge() -> None:
    JOB_PROGRESS_REDIS_BRIDGE.stop()
