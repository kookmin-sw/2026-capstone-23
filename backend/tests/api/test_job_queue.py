import importlib
import os

import pytest

os.environ.setdefault("QUEUE_BACKEND", "memory")

import api.job_queue as job_queue_module


def test_rabbitmq_init_failure_is_unavailable_without_memory_fallback(monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_BACKEND", "rabbitmq")
    monkeypatch.setenv("QUEUE_MEMORY_FALLBACK_ENABLED", "0")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:0/%2F")

    module = importlib.reload(job_queue_module)

    assert module.queue_backend_name() == "UnavailableQueue"
    status = module.queue_status()
    assert status["available"] is False
    assert status["degraded"] is True
    assert status["memoryFallbackEnabled"] is False
    with pytest.raises(module.QueueUnavailableError):
        module.enqueue("ji_unavailable", job_id="j_unavailable", queue_route="openai")


def test_rabbitmq_init_failure_falls_back_to_memory_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_BACKEND", "rabbitmq")
    monkeypatch.setenv("QUEUE_MEMORY_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:0/%2F")

    module = importlib.reload(job_queue_module)

    assert module.queue_backend_name() == "InMemoryQueue"
    status = module.queue_status()
    assert status["available"] is True
    assert status["degraded"] is True
    assert status["memoryFallbackEnabled"] is True
    module.enqueue("ji_fallback", job_id="j_fallback", queue_route="openai")
    message = module.dequeue_message(queue_route="openai")
    assert message is not None
    assert message.item_id == "ji_fallback"
    assert message.job_id == "j_fallback"


def test_runtime_rabbitmq_failure_switches_to_memory(monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_BACKEND", "rabbitmq")
    monkeypatch.setenv("QUEUE_MEMORY_FALLBACK_ENABLED", "1")
    module = importlib.reload(job_queue_module)

    class FailingRabbitQueue:
        def enqueue(self, *args, **kwargs) -> None:
            _ = (args, kwargs)
            raise RuntimeError("rabbitmq is down")

    module.QUEUE = FailingRabbitQueue()

    module.enqueue("ji_runtime_fallback", job_id="j_runtime", queue_route="openai")

    assert module.queue_backend_name() == "InMemoryQueue"
    message = module.dequeue_message(queue_route="openai")
    assert message is not None
    assert message.item_id == "ji_runtime_fallback"
    assert message.job_id == "j_runtime"
