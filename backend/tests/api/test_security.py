import os

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("ENABLE_INLINE_EXEC_WORKER", "false")
os.environ.setdefault("ENABLE_INLINE_RECOVERY_WORKER", "false")

from api import create_app
from api.services.auth_service import create_access_session, create_user
from infra.store import AUTH_SESSIONS, DOCUMENTS, JOB_EVENTS, JOB_ITEMS, JOBS, USERS


def setup_function():
    USERS.clear()
    AUTH_SESSIONS.clear()
    DOCUMENTS.clear()
    JOB_EVENTS.clear()
    JOB_ITEMS.clear()
    JOBS.clear()


def make_client() -> TestClient:
    return TestClient(create_app())


def make_token(login_id: str, *, role: str = "USER") -> tuple[str, dict]:
    user = create_user(
        login_id=login_id,
        name=f"{login_id} user",
        password="test-password",
        role=role,
        must_change_password=False,
    )
    return create_access_session(user)["accessToken"], user


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_global_auth_requires_bearer_but_leaves_bootstrap_and_health_public():
    client = make_client()

    health_res = client.get("/v1/health")
    assert health_res.status_code == 200
    assert health_res.json()["success"] is True
    assert health_res.json()["data"]["status"] == "ok"

    ready_res = client.get("/v1/health/ready")
    assert ready_res.status_code in {200, 503}
    assert ready_res.json()["success"] is True
    assert client.get("/v1/auth/bootstrap/status").status_code == 200

    res = client.get("/v1/documents")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def test_corrupt_access_session_returns_unauthorized_instead_of_500():
    AUTH_SESSIONS["bad-token"] = {
        "kind": "access",
        "token": "bad-token",
        "createdAt": "2026-05-10T00:00:00Z",
        "expiresAt": "2099-01-01T00:00:00Z",
    }
    client = make_client()

    res = client.get("/v1/monitoring/errors/summary", headers=auth_header("bad-token"))

    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def test_admin_only_api_rejects_normal_users_but_dashboard_monitoring_is_user_accessible():
    user_token, _ = make_token("normal")
    admin_token, _ = make_token("admin", role="ADMIN")
    client = make_client()

    user_res = client.get("/v1/admin/storage", headers=auth_header(user_token))
    assert user_res.status_code == 403
    assert user_res.json()["error"]["code"] == "FORBIDDEN"

    admin_res = client.get("/v1/admin/storage", headers=auth_header(admin_token))
    assert admin_res.status_code == 200
    assert admin_res.json()["success"] is True

    monitoring_user_res = client.get("/v1/monitoring/system", headers=auth_header(user_token))
    assert monitoring_user_res.status_code == 200
    assert monitoring_user_res.json()["success"] is True

    summary_user_res = client.get("/v1/monitoring/errors/summary", headers=auth_header(user_token))
    assert summary_user_res.status_code == 200
    assert summary_user_res.json()["success"] is True

    monitoring_admin_res = client.get("/v1/monitoring/system", headers=auth_header(admin_token))
    assert monitoring_admin_res.status_code == 200


def test_monitoring_errors_are_filtered_to_authenticated_owner():
    user_token, user = make_token("normal")
    other_token, other = make_token("other")
    admin_token, _ = make_token("admin", role="ADMIN")
    client = make_client()

    JOBS["j_owner"] = {
        "jobId": "j_owner",
        "ownerUserId": user["userId"],
        "status": "FAILED",
        "modelId": "m1",
    }
    JOB_ITEMS["ji_owner"] = {
        "jobItemId": "ji_owner",
        "jobId": "j_owner",
        "documentId": "d_owner",
        "ownerUserId": user["userId"],
        "fileName": "owner.pdf",
        "sourcePath": "data/inputs/owner.pdf",
        "status": "FAILED",
        "retryCount": 0,
        "lastError": {"code": "OWNER_FAIL", "message": "owner failure"},
        "updatedAt": "2026-05-10T00:00:00Z",
    }
    JOB_EVENTS["e_owner"] = {
        "eventId": "e_owner",
        "jobId": "j_owner",
        "jobItemId": "ji_owner",
        "eventType": "FAILED",
        "status": "FAILED",
        "retryCount": 0,
        "error": {"code": "OWNER_FAIL", "message": "owner failure"},
        "createdAt": "2026-05-10T00:00:01Z",
    }
    JOBS["j_other"] = {
        "jobId": "j_other",
        "ownerUserId": other["userId"],
        "status": "FAILED",
        "modelId": "m1",
    }
    JOB_ITEMS["ji_other"] = {
        "jobItemId": "ji_other",
        "jobId": "j_other",
        "documentId": "d_other",
        "ownerUserId": other["userId"],
        "fileName": "other.pdf",
        "sourcePath": "data/inputs/other.pdf",
        "status": "FAILED",
        "retryCount": 0,
        "lastError": {"code": "OTHER_FAIL", "message": "other failure"},
        "updatedAt": "2026-05-10T00:01:00Z",
    }
    JOB_EVENTS["e_other"] = {
        "eventId": "e_other",
        "jobId": "j_other",
        "jobItemId": "ji_other",
        "eventType": "FAILED",
        "status": "FAILED",
        "retryCount": 0,
        "error": {"code": "OTHER_FAIL", "message": "other failure"},
        "createdAt": "2026-05-10T00:01:01Z",
    }

    owner_summary = client.get("/v1/monitoring/errors/summary", headers=auth_header(user_token))
    assert owner_summary.status_code == 200
    assert owner_summary.json()["data"]["totalErrors"] == 1
    assert owner_summary.json()["data"]["recent"][0]["type"] == "OWNER_FAIL"

    other_summary = client.get("/v1/monitoring/errors/summary", headers=auth_header(other_token))
    assert other_summary.status_code == 200
    assert other_summary.json()["data"]["totalErrors"] == 1
    assert other_summary.json()["data"]["recent"][0]["type"] == "OTHER_FAIL"

    admin_summary = client.get("/v1/monitoring/errors/summary", headers=auth_header(admin_token))
    assert admin_summary.status_code == 200
    assert admin_summary.json()["data"]["totalErrors"] == 2


def test_document_owner_filtering_and_forbidden_cross_owner_read():
    user_token, user = make_token("owner")
    other_token, other = make_token("other")
    admin_token, _ = make_token("supervisor", role="ADMIN")
    client = make_client()

    DOCUMENTS["d_owner"] = {
        "documentId": "d_owner",
        "ownerUserId": user["userId"],
        "originalFilename": "owner.pdf",
        "latestStatus": "COMPLETED",
        "updatedAt": "2026-01-01T00:00:00Z",
        "uploadedAt": "2026-01-01T00:00:00Z",
        "outputPath": "",
        "meta": {},
    }
    DOCUMENTS["d_other"] = {
        "documentId": "d_other",
        "ownerUserId": other["userId"],
        "originalFilename": "other.pdf",
        "latestStatus": "COMPLETED",
        "updatedAt": "2026-01-02T00:00:00Z",
        "uploadedAt": "2026-01-02T00:00:00Z",
        "outputPath": "",
        "meta": {},
    }

    owner_list = client.get("/v1/documents", headers=auth_header(user_token))
    assert owner_list.status_code == 200
    assert [item["documentId"] for item in owner_list.json()["data"]["items"]] == ["d_owner"]

    admin_list = client.get("/v1/documents", headers=auth_header(admin_token))
    assert admin_list.status_code == 200
    assert {item["documentId"] for item in admin_list.json()["data"]["items"]} == {"d_owner", "d_other"}

    own_result = client.get("/v1/documents/d_owner/result", headers=auth_header(user_token))
    assert own_result.status_code == 200

    cross_owner_result = client.get("/v1/documents/d_other/result", headers=auth_header(user_token))
    assert cross_owner_result.status_code == 403
    assert cross_owner_result.json()["detail"]["error"]["code"] == "FORBIDDEN"

    other_result = client.get("/v1/documents/d_other/result", headers=auth_header(other_token))
    assert other_result.status_code == 200


def test_websocket_jobs_allow_demo_anonymous_but_system_requires_admin():
    user_token, _ = make_token("ws_user")
    admin_token, _ = make_token("ws_admin", role="ADMIN")
    client = make_client()

    with client.websocket_connect("/v1/ws/jobs") as websocket:
        anonymous_connected = websocket.receive_json()
    assert anonymous_connected["success"] is True

    with client.websocket_connect(f"/v1/ws/jobs?accessToken={user_token}") as websocket:
        connected = websocket.receive_json()
    assert connected["success"] is True

    try:
        with client.websocket_connect(f"/v1/ws/system?accessToken={user_token}"):
            raise AssertionError("system websocket should reject non-admin users")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008

    with client.websocket_connect(f"/v1/ws/system?accessToken={admin_token}") as websocket:
        metrics = websocket.receive_json()
    assert metrics["success"] is True
