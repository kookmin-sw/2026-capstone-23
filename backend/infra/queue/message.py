from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class QueueMessage:
    item_id: str
    job_id: Optional[str] = None
    attempt: int = 0
    queued_at: Optional[str] = None
    queue_route: Optional[str] = None
    ack_token: Any = None
