import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from api.routers.ws import router as ws_router
from api.services.auth_service import create_access_session, create_user
from infra.store import AUTH_SESSIONS, USERS
from api.ws_hub import publish_job_progress


def setup_function():
    USERS.clear()
    AUTH_SESSIONS.clear()


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(ws_router, prefix="/api/v1")
    return TestClient(app)


def make_token(*, role: str = "USER") -> str:
    user = create_user(
        login_id=f"{role.lower()}_ws",
        name=f"{role.title()} WS",
        password="test-password",
        role=role,
        must_change_password=False,
    )
    return create_access_session(user)["accessToken"]


def test_ws_jobs_connected_event():
    client = make_client()
    with client.websocket_connect("/api/v1/ws/jobs") as websocket:
        payload = websocket.receive_json()

    assert payload["success"] is True
    data = payload["data"]
    assert data["type"] == "job.item.progress"
    assert data["status"] == "CONNECTED"
    assert "timestamp" in data


def test_ws_jobs_receives_runtime_progress_event():
    client = make_client()
    with client.websocket_connect("/api/v1/ws/jobs") as websocket:
        _connected = websocket.receive_json()

        publish_job_progress(
            {
                "type": "job.item.progress",
                "jobId": "j_1",
                "jobItemId": "ji_1",
                "documentId": "d_1",
                "status": "PROCESSING",
                "eventType": "STARTED",
                "workerId": "worker-test",
                "retryCount": 0,
                "error": None,
                "timestamp": "2026-03-29T00:00:00Z",
            }
        )
        payload = websocket.receive_json()

    assert payload["success"] is True
    data = payload["data"]
    assert data["jobId"] == "j_1"
    assert data["jobItemId"] == "ji_1"
    assert data["status"] == "PROCESSING"
    assert data["eventType"] == "STARTED"


def test_ws_system_metrics_event():
    client = make_client()
    token = make_token(role="ADMIN")
    with client.websocket_connect(f"/api/v1/ws/system?accessToken={token}") as websocket:
        payload = websocket.receive_json()

    assert payload["success"] is True
    data = payload["data"]
    assert data["type"] == "system.metrics"
    assert "memory" in data
    assert "timestamp" in data


def test_ws_jobs_allows_missing_token():
    client = make_client()
    with client.websocket_connect("/api/v1/ws/jobs") as websocket:
        payload = websocket.receive_json()

    assert payload["success"] is True
    assert payload["data"]["status"] == "CONNECTED"
