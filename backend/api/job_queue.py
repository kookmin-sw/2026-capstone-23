from __future__ import annotations

import importlib
from typing import Optional

from infra.queue.message import QueueMessage

if "_impl" in globals():
    _impl = importlib.reload(globals()["_impl"])
else:
    from infra.queue import job_queue as _impl

InMemoryQueue = _impl.InMemoryQueue
QUEUE = _impl.QUEUE
QueueUnavailableError = _impl.QueueUnavailableError


def _sync_to_impl() -> None:
    _impl.QUEUE = QUEUE


def _sync_from_impl() -> None:
    global QUEUE
    QUEUE = _impl.QUEUE


def queue_backend_name() -> str:
    _sync_to_impl()
    try:
        return _impl.queue_backend_name()
    finally:
        _sync_from_impl()


def queue_status() -> dict:
    _sync_to_impl()
    try:
        return _impl.queue_status()
    finally:
        _sync_from_impl()


def ensure_queue_available() -> None:
    _sync_to_impl()
    try:
        _impl.ensure_queue_available()
    finally:
        _sync_from_impl()


def memory_fallback_enabled() -> bool:
    return _impl.memory_fallback_enabled()


def queue_size(queue_route: Optional[str] = None) -> int:
    _sync_to_impl()
    try:
        return _impl.queue_size(queue_route=queue_route)
    finally:
        _sync_from_impl()


def queue_sizes() -> dict[str, int]:
    _sync_to_impl()
    try:
        return _impl.queue_sizes()
    finally:
        _sync_from_impl()


def enqueue(
    item_id: str,
    *,
    job_id: Optional[str] = None,
    attempt: int = 0,
    queued_at: Optional[str] = None,
    queue_route: Optional[str] = None,
) -> None:
    _sync_to_impl()
    try:
        _impl.enqueue(
            item_id,
            job_id=job_id,
            attempt=attempt,
            queued_at=queued_at,
            queue_route=queue_route,
        )
    finally:
        _sync_from_impl()


def dequeue(queue_route: Optional[str] = None) -> Optional[str]:
    _sync_to_impl()
    try:
        return _impl.dequeue(queue_route=queue_route)
    finally:
        _sync_from_impl()


def dequeue_message(queue_route: Optional[str] = None) -> Optional[QueueMessage]:
    _sync_to_impl()
    try:
        return _impl.dequeue_message(queue_route=queue_route)
    finally:
        _sync_from_impl()


def ack(message: QueueMessage) -> None:
    _sync_to_impl()
    try:
        _impl.ack(message)
    finally:
        _sync_from_impl()


def nack(message: QueueMessage, requeue: bool = True) -> None:
    _sync_to_impl()
    try:
        _impl.nack(message, requeue=requeue)
    finally:
        _sync_from_impl()


__all__ = [
    "InMemoryQueue",
    "QUEUE",
    "QueueMessage",
    "QueueUnavailableError",
    "ack",
    "dequeue",
    "dequeue_message",
    "enqueue",
    "ensure_queue_available",
    "memory_fallback_enabled",
    "nack",
    "queue_backend_name",
    "queue_size",
    "queue_sizes",
    "queue_status",
]
