from __future__ import annotations

from threading import RLock
from typing import Any, Optional

from core.env import env_bool, env_int, env_str

from core.jobs.execution import KNOWN_QUEUE_ROUTES
from infra.queue.memory import InMemoryQueue
from infra.queue.message import QueueMessage

_QUEUE_LOCK = RLock()
_FALLBACK_REASON: str | None = None


class QueueUnavailableError(RuntimeError):
    """Raised when the configured distributed queue cannot accept work."""


class UnavailableQueue:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def enqueue(self, *args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)
        raise QueueUnavailableError(f"queue unavailable: {self.reason}")

    def dequeue(self, queue_route: Optional[str] = None) -> Optional[str]:
        _ = queue_route
        return None

    def dequeue_message(self, queue_route: Optional[str] = None) -> Optional[QueueMessage]:
        _ = queue_route
        return None

    def ack(self, message: QueueMessage) -> None:
        _ = message

    def nack(self, message: QueueMessage, requeue: bool = True) -> None:
        _ = (message, requeue)

    def size(self, queue_route: Optional[str] = None) -> int:
        _ = queue_route
        return 0


def _configured_backend() -> str:
    return env_str("QUEUE_BACKEND", "rabbitmq", strip=True).lower()


def memory_fallback_enabled() -> bool:
    return env_bool("QUEUE_MEMORY_FALLBACK_ENABLED", False)


def _unavailable_from(exc: Exception) -> UnavailableQueue:
    return UnavailableQueue(f"{exc.__class__.__name__}: {exc}")


def _build_rabbitmq_queue():
    from infra.queue.rabbitmq import RabbitMQQueue

    amqp_url = env_str(
        "RABBITMQ_URL",
        "amqp://luminir:luminir-local-password@localhost:5672/%2F",
    )
    queue_name = env_str("RABBITMQ_QUEUE", "jobs.queue")
    heartbeat_seconds = env_int("RABBITMQ_HEARTBEAT_SECONDS", 0)
    queue = RabbitMQQueue(
        amqp_url=amqp_url,
        queue_name=queue_name,
        heartbeat_seconds=heartbeat_seconds,
    )
    print(
        f"[queue] backend=rabbitmq, queue={queue_name}, url={amqp_url}, "
        f"heartbeat={heartbeat_seconds}s"
    )
    return queue


def _build_queue():
    global _FALLBACK_REASON
    backend = _configured_backend()
    if backend == "memory":
        _FALLBACK_REASON = None
        print("[queue] backend=memory (explicit local/test mode)")
        return InMemoryQueue()

    if backend not in {"", "rabbitmq"}:
        print(f"[queue] backend={backend!r} is deprecated; using rabbitmq")

    try:
        _FALLBACK_REASON = None
        return _build_rabbitmq_queue()
    except Exception as exc:  # noqa: BLE001
        if memory_fallback_enabled():
            _FALLBACK_REASON = f"rabbitmq init failed: {exc!r}"
            print(f"[queue] rabbitmq init failed ({exc!r}); fallback to memory queue")
            return InMemoryQueue()
        _FALLBACK_REASON = None
        print(f"[queue] rabbitmq init failed ({exc!r}); queue unavailable")
        return _unavailable_from(exc)


QUEUE = _build_queue()


def _switch_to_memory(exc: Exception) -> None:
    global QUEUE, _FALLBACK_REASON
    with _QUEUE_LOCK:
        if isinstance(QUEUE, InMemoryQueue):
            return
        if not memory_fallback_enabled():
            QUEUE = _unavailable_from(exc)
            raise QueueUnavailableError(f"queue unavailable: {exc}") from exc
        _FALLBACK_REASON = f"rabbitmq operation failed: {exc!r}"
        print(f"[queue] rabbitmq operation failed ({exc!r}); fallback to memory queue")
        QUEUE = InMemoryQueue()


def queue_backend_name() -> str:
    return QUEUE.__class__.__name__


def queue_status() -> dict[str, Any]:
    queue = QUEUE
    unavailable = isinstance(queue, UnavailableQueue)
    configured_backend = _configured_backend()
    local_memory = isinstance(queue, InMemoryQueue)
    degraded = unavailable or (local_memory and configured_backend != "memory")
    reason = queue.reason if unavailable else _FALLBACK_REASON
    return {
        "backend": queue_backend_name(),
        "configuredBackend": configured_backend or "rabbitmq",
        "available": not unavailable,
        "degraded": degraded,
        "memoryFallbackEnabled": memory_fallback_enabled(),
        "reason": reason,
    }


def ensure_queue_available() -> None:
    status = queue_status()
    if not status["available"]:
        raise QueueUnavailableError(str(status.get("reason") or "queue unavailable"))


def queue_size(queue_route: Optional[str] = None) -> int:
    queue = QUEUE
    try:
        return queue.size(queue_route=queue_route)
    except Exception as exc:
        if isinstance(queue, InMemoryQueue):
            raise
        try:
            _switch_to_memory(exc)
        except QueueUnavailableError:
            return 0
        return QUEUE.size(queue_route=queue_route)


def queue_sizes() -> dict[str, int]:
    return {route: queue_size(route) for route in KNOWN_QUEUE_ROUTES}


def enqueue(
    item_id: str,
    *,
    job_id: Optional[str] = None,
    attempt: int = 0,
    queued_at: Optional[str] = None,
    queue_route: Optional[str] = None,
) -> None:
    ensure_queue_available()
    queue = QUEUE
    try:
        queue.enqueue(
            item_id,
            job_id=job_id,
            attempt=attempt,
            queued_at=queued_at,
            queue_route=queue_route,
        )
    except Exception as exc:
        if isinstance(queue, InMemoryQueue):
            raise
        _switch_to_memory(exc)
        ensure_queue_available()
        QUEUE.enqueue(
            item_id,
            job_id=job_id,
            attempt=attempt,
            queued_at=queued_at,
            queue_route=queue_route,
        )


def dequeue(queue_route: Optional[str] = None) -> Optional[str]:
    queue = QUEUE
    try:
        return queue.dequeue(queue_route=queue_route)
    except Exception as exc:
        if isinstance(queue, InMemoryQueue):
            raise
        try:
            _switch_to_memory(exc)
        except QueueUnavailableError:
            return None
        return QUEUE.dequeue(queue_route=queue_route)


def dequeue_message(queue_route: Optional[str] = None) -> Optional[QueueMessage]:
    queue = QUEUE
    try:
        return queue.dequeue_message(queue_route=queue_route)
    except Exception as exc:
        if isinstance(queue, InMemoryQueue):
            raise
        try:
            _switch_to_memory(exc)
        except QueueUnavailableError:
            return None
        return QUEUE.dequeue_message(queue_route=queue_route)


def ack(message: QueueMessage) -> None:
    queue = QUEUE
    try:
        queue.ack(message)
    except Exception as exc:
        if isinstance(queue, InMemoryQueue):
            raise
        try:
            _switch_to_memory(exc)
        except QueueUnavailableError:
            return
        QUEUE.ack(message)


def nack(message: QueueMessage, requeue: bool = True) -> None:
    queue = QUEUE
    try:
        queue.nack(message, requeue=requeue)
    except Exception as exc:
        if isinstance(queue, InMemoryQueue):
            raise
        try:
            _switch_to_memory(exc)
        except QueueUnavailableError:
            return
        QUEUE.nack(message, requeue=requeue)
