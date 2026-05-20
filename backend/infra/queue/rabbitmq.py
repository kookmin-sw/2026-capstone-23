import json
from threading import RLock
from typing import Optional

from core.jobs.execution import DEFAULT_QUEUE_ROUTE
from infra.queue.message import QueueMessage


class RabbitMQQueue:
    def __init__(
        self,
        amqp_url: str,
        queue_name: str = "jobs.queue",
        *,
        heartbeat_seconds: int = 0,
    ) -> None:
        self._amqp_url = amqp_url
        self._queue_name = queue_name
        self._heartbeat_seconds = max(0, int(heartbeat_seconds))
        self._connection = None
        self._channel = None
        self._lock = RLock()
        self._connect()

    def _route(self, queue_route: Optional[str]) -> str:
        return (queue_route or DEFAULT_QUEUE_ROUTE).strip() or DEFAULT_QUEUE_ROUTE

    def _queue_name_for_route(self, queue_route: Optional[str]) -> str:
        route = self._route(queue_route)
        if route == DEFAULT_QUEUE_ROUTE:
            return self._queue_name
        return f"{self._queue_name}.{route}"

    def _close_current(self) -> None:
        channel = self._channel
        connection = self._connection
        self._channel = None
        self._connection = None

        try:
            if channel is not None and not channel.is_closed:
                channel.close()
        except Exception:
            pass

        try:
            if connection is not None and not connection.is_closed:
                connection.close()
        except Exception:
            pass

    def _connect(self) -> None:
        import pika  # type: ignore

        parameters = pika.URLParameters(self._amqp_url)
        if hasattr(parameters, "heartbeat"):
            parameters.heartbeat = self._heartbeat_seconds
        self._connection = pika.BlockingConnection(parameters)
        self._channel = self._connection.channel()
        self._channel.queue_declare(queue=self._queue_name_for_route(DEFAULT_QUEUE_ROUTE), durable=True)

    def _ensure_channel(self) -> None:
        if self._connection is None or self._channel is None:
            self._connect()
            return
        if self._connection.is_closed or self._channel.is_closed:
            self._close_current()
            self._connect()

    def _recover_connection(self) -> None:
        self._close_current()
        self._connect()

    def enqueue(
        self,
        item_id: str,
        *,
        job_id: Optional[str] = None,
        attempt: int = 0,
        queued_at: Optional[str] = None,
        queue_route: Optional[str] = None,
    ) -> None:
        import pika  # type: ignore

        route = self._route(queue_route)
        queue_name = self._queue_name_for_route(route)
        with self._lock:
            self._ensure_channel()
            self._channel.queue_declare(queue=queue_name, durable=True)
            payload = json.dumps(
                {
                    "jobItemId": item_id,
                    "jobId": job_id,
                    "attempt": int(attempt),
                    "queuedAt": queued_at,
                    "queueRoute": route,
                }
            )
            try:
                self._channel.basic_publish(
                    exchange="",
                    routing_key=queue_name,
                    body=payload,
                    properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
                )
            except Exception:
                self._recover_connection()
                self._channel.queue_declare(queue=queue_name, durable=True)
                self._channel.basic_publish(
                    exchange="",
                    routing_key=queue_name,
                    body=payload,
                    properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
                )

    def dequeue(self, queue_route: Optional[str] = None) -> Optional[str]:
        queue_name = self._queue_name_for_route(queue_route)
        with self._lock:
            self._ensure_channel()
            self._channel.queue_declare(queue=queue_name, durable=True)
            try:
                method_frame, _header_frame, body = self._channel.basic_get(queue=queue_name, auto_ack=True)
            except Exception:
                self._recover_connection()
                self._channel.queue_declare(queue=queue_name, durable=True)
                method_frame, _header_frame, body = self._channel.basic_get(queue=queue_name, auto_ack=True)
            if method_frame is None:
                return None
            try:
                payload = json.loads(body.decode("utf-8"))
                item_id = str(payload.get("jobItemId") or "")
                return item_id or None
            except Exception:
                raw = body.decode("utf-8", errors="ignore")
                return raw or None

    def dequeue_message(self, queue_route: Optional[str] = None) -> Optional[QueueMessage]:
        route = self._route(queue_route)
        queue_name = self._queue_name_for_route(route)
        with self._lock:
            self._ensure_channel()
            self._channel.queue_declare(queue=queue_name, durable=True)
            try:
                method_frame, _header_frame, body = self._channel.basic_get(queue=queue_name, auto_ack=False)
            except Exception:
                self._recover_connection()
                self._channel.queue_declare(queue=queue_name, durable=True)
                method_frame, _header_frame, body = self._channel.basic_get(queue=queue_name, auto_ack=False)
            if method_frame is None:
                return None

            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {"jobItemId": body.decode("utf-8", errors="ignore")}

            item_id = str(payload.get("jobItemId") or "")
            if not item_id:
                self._channel.basic_nack(delivery_tag=method_frame.delivery_tag, requeue=False)
                return None

            return QueueMessage(
                item_id=item_id,
                job_id=payload.get("jobId"),
                attempt=int(payload.get("attempt", 0)),
                queued_at=payload.get("queuedAt"),
                queue_route=str(payload.get("queueRoute") or route),
                ack_token={
                    "deliveryTag": int(method_frame.delivery_tag),
                    "channelNumber": int(self._channel.channel_number),
                },
            )

    def ack(self, message: QueueMessage) -> None:
        token = message.ack_token or {}
        delivery_tag = token.get("deliveryTag")
        channel_number = token.get("channelNumber")
        if delivery_tag is None or channel_number is None:
            return
        with self._lock:
            # reconnect 이후 channel이 바뀐 경우 기존 delivery tag는 무효이므로 ack를 건너뛴다.
            if self._channel is None or self._channel.is_closed:
                return
            if int(self._channel.channel_number) != int(channel_number):
                return
            self._channel.basic_ack(delivery_tag=int(delivery_tag))

    def nack(self, message: QueueMessage, requeue: bool = True) -> None:
        token = message.ack_token or {}
        delivery_tag = token.get("deliveryTag")
        channel_number = token.get("channelNumber")
        if delivery_tag is None or channel_number is None:
            return
        with self._lock:
            if self._channel is None or self._channel.is_closed:
                return
            if int(self._channel.channel_number) != int(channel_number):
                return
            self._channel.basic_nack(delivery_tag=int(delivery_tag), requeue=bool(requeue))

    def size(self, queue_route: Optional[str] = None) -> int:
        queue_name = self._queue_name_for_route(queue_route)
        with self._lock:
            self._ensure_channel()
            try:
                result = self._channel.queue_declare(queue=queue_name, durable=True, passive=True)
            except Exception:
                self._recover_connection()
                result = self._channel.queue_declare(queue=queue_name, durable=True)
            return int(result.method.message_count)
