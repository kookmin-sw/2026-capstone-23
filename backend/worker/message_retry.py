from __future__ import annotations

from core.env import env_int
from core.time import now_iso
from infra.queue.job_queue import ack as ack_message
from infra.queue.job_queue import enqueue as enqueue_message
from infra.queue.message import QueueMessage


def requeue_missing_state_message(
    *,
    message: QueueMessage,
    worker_id: str,
    reason: str,
    log_prefix: str,
) -> None:
    max_attempts = env_int("WORKER_MISSING_STATE_MAX_RETRIES", 5, minimum=0)
    next_attempt = int(message.attempt or 0) + 1

    if next_attempt > max_attempts:
        print(
            f"[{log_prefix}] dropping stale message after bounded retries: "
            f"item_id={message.item_id}, route={message.queue_route}, worker_id={worker_id}, "
            f"attempt={next_attempt - 1}, reason={reason}"
        )
        ack_message(message)
        return

    enqueue_message(
        message.item_id,
        job_id=message.job_id,
        attempt=next_attempt,
        queued_at=now_iso(),
        queue_route=message.queue_route,
    )
    ack_message(message)
    print(
        f"[{log_prefix}] missing store state; requeued message: "
        f"item_id={message.item_id}, route={message.queue_route}, worker_id={worker_id}, "
        f"attempt={next_attempt}/{max_attempts}, reason={reason}"
    )
