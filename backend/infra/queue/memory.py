from collections import deque
from threading import Lock
from typing import Deque, Optional

from core.jobs.execution import DEFAULT_QUEUE_ROUTE
from infra.queue.message import QueueMessage


class InMemoryQueue:
    """Process-local FIFO queue (v1/default)."""

    def __init__(self) -> None:
        self._queues: dict[str, Deque[QueueMessage]] = {DEFAULT_QUEUE_ROUTE: deque()}
        self._lock = Lock()

    def _route(self, queue_route: Optional[str]) -> str:
        return (queue_route or DEFAULT_QUEUE_ROUTE).strip() or DEFAULT_QUEUE_ROUTE

    def enqueue(
        self,
        item_id: str,
        *,
        job_id: Optional[str] = None,
        attempt: int = 0,
        queued_at: Optional[str] = None,
        queue_route: Optional[str] = None,
    ) -> None:
        route = self._route(queue_route)
        with self._lock:
            self._queues.setdefault(route, deque()).append(
                QueueMessage(
                    item_id=item_id,
                    job_id=job_id,
                    attempt=int(attempt),
                    queued_at=queued_at,
                    queue_route=route,
                )
            )

    def dequeue(self, queue_route: Optional[str] = None) -> Optional[str]:
        message = self.dequeue_message(queue_route=queue_route)
        if message is None:
            return None
        return message.item_id

    def dequeue_message(self, queue_route: Optional[str] = None) -> Optional[QueueMessage]:
        route = self._route(queue_route)
        with self._lock:
            queue = self._queues.setdefault(route, deque())
            if not queue:
                return None
            return queue.popleft()

    def size(self, queue_route: Optional[str] = None) -> int:
        route = self._route(queue_route)
        with self._lock:
            return len(self._queues.setdefault(route, deque()))

    def ack(self, message: QueueMessage) -> None:
        _ = message

    def nack(self, message: QueueMessage, requeue: bool = True) -> None:
        if not requeue:
            return
        self.enqueue(
            message.item_id,
            job_id=message.job_id,
            attempt=message.attempt,
            queued_at=message.queued_at,
            queue_route=message.queue_route,
        )
