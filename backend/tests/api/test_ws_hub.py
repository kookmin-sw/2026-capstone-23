import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from api.ws_hub import JobWebSocketHub


class FakeWebSocket:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(payload)


def test_job_ws_hub_publish_without_connections_is_noop() -> None:
    # 연결된 클라이언트가 없을 때 publish가 예외 없이 무시되는지 검증
    hub = JobWebSocketHub()
    hub.publish({"type": "job.item.progress", "status": "QUEUED"})


def test_job_ws_hub_broadcasts_and_prunes_disconnected_clients() -> None:
    # 허브가 활성 클라이언트에는 이벤트를 보내고 끊긴 클라이언트는 연결 목록에서 제거하는지 검증
    async def scenario() -> None:
        hub = JobWebSocketHub()
        active_socket = FakeWebSocket()
        disconnected_socket = FakeWebSocket(fail=True)

        await hub.register(active_socket)
        await hub.register(disconnected_socket)

        hub.publish({"type": "job.item.progress", "jobId": "j_1", "status": "PROCESSING"})
        await asyncio.sleep(0.05)

        assert len(active_socket.sent) == 1
        assert active_socket.sent[0]["success"] is True
        assert active_socket.sent[0]["data"]["jobId"] == "j_1"
        assert disconnected_socket not in hub._connections

        await hub.unregister(active_socket)
        assert not hub._connections

    asyncio.run(scenario())
