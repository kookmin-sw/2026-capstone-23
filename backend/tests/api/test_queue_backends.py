import importlib
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("STATUS_CACHE_BACKEND", "none")
os.environ.setdefault("QUEUE_BACKEND", "memory")

from core.jobs.execution import worker_queue_routes
from infra.queue.memory import InMemoryQueue


def test_in_memory_queue_round_trip_and_requeue() -> None:
    queue = InMemoryQueue()

    queue.enqueue("ji_1", job_id="j_1", attempt=2, queued_at="2026-04-02T00:00:00Z")
    assert queue.size() == 1

    message = queue.dequeue_message()
    assert message is not None
    assert message.item_id == "ji_1"
    assert message.job_id == "j_1"
    assert message.attempt == 2
    assert message.queued_at == "2026-04-02T00:00:00Z"
    assert message.queue_route == "default"
    assert queue.size() == 0

    queue.nack(message, requeue=True)
    assert queue.size() == 1
    assert queue.dequeue() == "ji_1"
    assert queue.size() == 0


def test_in_memory_queue_nack_drop() -> None:
    queue = InMemoryQueue()

    queue.enqueue("ji_drop")
    message = queue.dequeue_message()
    assert message is not None

    queue.nack(message, requeue=False)
    assert queue.size() == 0


def test_memory_queue_supports_route_specific_enqueue_and_dequeue() -> None:
    queue = InMemoryQueue()
    queue.enqueue("ji_openai", queue_route="openai")
    queue.enqueue("ji_openrouter", queue_route="openrouter")
    queue.enqueue("ji_qwen", queue_route="qwen_gpu")

    assert queue.size(queue_route="openai") == 1
    assert queue.size(queue_route="openrouter") == 1
    assert queue.size(queue_route="qwen_gpu") == 1
    assert queue.dequeue(queue_route="openai") == "ji_openai"
    assert queue.dequeue(queue_route="openrouter") == "ji_openrouter"
    assert queue.dequeue(queue_route="qwen_gpu") == "ji_qwen"


def test_worker_modes_include_openrouter_routes_for_compatibility() -> None:
    assert worker_queue_routes("all") == ["openai", "openrouter", "qwen_gpu"]
    assert worker_queue_routes("openai") == ["openai", "openrouter"]
    assert worker_queue_routes("openrouter") == ["openrouter"]


def _install_fake_pika(monkeypatch):
    state: dict[str, object] = {"next_delivery_tag": 1}

    class FakeBasicProperties:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class FakeChannel:
        def __init__(self) -> None:
            self.is_closed = False
            self.channel_number = 7
            self.messages: list[bytes] = []
            self.published: list[dict[str, object]] = []
            self.acked: list[int] = []
            self.nacked: list[tuple[int, bool]] = []

        def queue_declare(self, queue: str, durable: bool, passive: bool = False):
            _ = (queue, durable, passive)
            return SimpleNamespace(
                method=SimpleNamespace(message_count=len(self.messages))
            )

        def basic_publish(
            self,
            exchange: str,
            routing_key: str,
            body: str,
            properties,
        ) -> None:
            self.published.append(
                {
                    "exchange": exchange,
                    "routing_key": routing_key,
                    "body": body,
                    "properties": properties,
                }
            )
            self.messages.append(body.encode("utf-8"))

        def basic_get(self, queue: str, auto_ack: bool):
            _ = (queue, auto_ack)
            if not self.messages:
                return None, None, None
            body = self.messages.pop(0)
            delivery_tag = int(state["next_delivery_tag"])
            state["next_delivery_tag"] = delivery_tag + 1
            return SimpleNamespace(delivery_tag=delivery_tag), None, body

        def basic_ack(self, delivery_tag: int) -> None:
            self.acked.append(delivery_tag)

        def basic_nack(self, delivery_tag: int, requeue: bool) -> None:
            self.nacked.append((delivery_tag, requeue))

    class FakeConnection:
        def __init__(self, params) -> None:
            self.params = params
            self.is_closed = False
            self._channel = FakeChannel()

        def channel(self) -> FakeChannel:
            return self._channel

        def close(self) -> None:
            self.is_closed = True

    def fake_url_parameters(url: str) -> str:
        return url

    def fake_blocking_connection(params) -> FakeConnection:
        connection = FakeConnection(params)
        state["connection"] = connection
        state["channel"] = connection.channel()
        return connection

    fake_pika = SimpleNamespace(
        URLParameters=fake_url_parameters,
        BlockingConnection=fake_blocking_connection,
        BasicProperties=FakeBasicProperties,
    )
    monkeypatch.setitem(sys.modules, "pika", fake_pika)
    return state


def test_rabbitmq_queue_publish_dequeue_and_ack(monkeypatch) -> None:
    state = _install_fake_pika(monkeypatch)

    import infra.queue.rabbitmq as rabbitmq_module

    rabbitmq_module = importlib.reload(rabbitmq_module)
    queue = rabbitmq_module.RabbitMQQueue("amqp://guest:guest@localhost:5672/%2F")
    channel = state["channel"]

    queue.enqueue(
        "ji_3",
        job_id="j_3",
        attempt=4,
        queued_at="2026-04-02T02:00:00Z",
        queue_route="qwen_gpu",
    )
    assert len(channel.published) == 1
    assert channel.published[0]["routing_key"] == "jobs.queue.qwen_gpu"
    assert channel.published[0]["properties"].kwargs["delivery_mode"] == 2

    message = queue.dequeue_message(queue_route="qwen_gpu")
    assert message is not None
    assert message.item_id == "ji_3"
    assert message.job_id == "j_3"
    assert message.attempt == 4
    assert message.queued_at == "2026-04-02T02:00:00Z"
    assert message.queue_route == "qwen_gpu"
    assert message.ack_token == {"deliveryTag": 1, "channelNumber": 7}

    queue.ack(message)
    assert channel.acked == [1]


def test_rabbitmq_queue_invalid_payload_is_nacked(monkeypatch) -> None:
    state = _install_fake_pika(monkeypatch)

    import infra.queue.rabbitmq as rabbitmq_module

    rabbitmq_module = importlib.reload(rabbitmq_module)
    queue = rabbitmq_module.RabbitMQQueue("amqp://guest:guest@localhost:5672/%2F")
    channel = state["channel"]
    channel.messages.append(b"{}")

    assert queue.dequeue_message(queue_route="openai") is None
    assert channel.nacked == [(1, False)]


def test_rabbitmq_queue_reconnects_and_retries_publish(monkeypatch) -> None:
    state = _install_fake_pika(monkeypatch)

    import infra.queue.rabbitmq as rabbitmq_module

    rabbitmq_module = importlib.reload(rabbitmq_module)
    queue = rabbitmq_module.RabbitMQQueue("amqp://guest:guest@localhost:5672/%2F")
    first_channel = state["channel"]

    original_basic_publish = first_channel.basic_publish
    fail_once = {"done": False}

    def flaky_basic_publish(
        exchange: str,
        routing_key: str,
        body: str,
        properties,
    ) -> None:
        if not fail_once["done"]:
            fail_once["done"] = True
            first_channel.is_closed = True
            raise RuntimeError("connection lost during publish")
        original_basic_publish(exchange, routing_key, body, properties)

    first_channel.basic_publish = flaky_basic_publish

    queue.enqueue(
        "ji_reconnect",
        job_id="j_reconnect",
        attempt=0,
        queued_at="2026-04-02T03:00:00Z",
        queue_route="openai",
    )

    second_channel = state["channel"]
    assert second_channel is not None
    assert len(second_channel.published) == 1
    assert second_channel.published[0]["routing_key"] == "jobs.queue.openai"
