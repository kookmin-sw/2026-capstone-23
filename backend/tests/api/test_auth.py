import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from api.routers.auth import router
from infra.store import AUTH_SESSIONS, USERS


def setup_function():
    USERS.clear()
    AUTH_SESSIONS.clear()


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_env_admin_bootstrap_creates_superuser_and_blocks_env_login(monkeypatch):
    monkeypatch.setenv("ADMIN_ID", "root")
    monkeypatch.setenv("ADMIN_PW", "temporary-admin-pass")
    client = make_client()

    status_res = client.get("/api/v1/auth/bootstrap/status")
    assert status_res.status_code == 200
    assert status_res.json()["data"]["bootstrapRequired"] is True
    assert status_res.json()["data"]["adminEnvConfigured"] is True

    bootstrap_login_res = client.post(
        "/api/v1/auth/login",
        json={"loginId": "root", "password": "temporary-admin-pass"},
    )
    assert bootstrap_login_res.status_code == 200
    bootstrap_data = bootstrap_login_res.json()["data"]
    assert bootstrap_data["bootstrapRequired"] is True
    assert bootstrap_data["next"] == "SIGNUP_SUPERUSER"

    superuser_res = client.post(
        "/api/v1/auth/bootstrap/superuser",
        json={
            "bootstrapToken": bootstrap_data["bootstrapToken"],
            "name": "System Admin",
            "loginId": "admin",
            "password": "real-admin-pass",
        },
    )
    assert superuser_res.status_code == 200
    superuser_data = superuser_res.json()["data"]
    assert superuser_data["tokenType"] == "bearer"
    assert superuser_data["user"]["role"] == "SUPERUSER"
    assert superuser_data["user"]["loginId"] == "admin"

    blocked_env_login_res = client.post(
        "/api/v1/auth/login",
        json={"loginId": "root", "password": "temporary-admin-pass"},
    )
    assert blocked_env_login_res.status_code == 401
    assert blocked_env_login_res.json()["detail"]["error"]["code"] == "INVALID_CREDENTIALS"


def test_admin_creates_user_and_first_login_requires_password_change(monkeypatch):
    monkeypatch.setenv("ADMIN_ID", "root")
    monkeypatch.setenv("ADMIN_PW", "temporary-admin-pass")
    client = make_client()

    bootstrap_login_res = client.post(
        "/api/v1/auth/login",
        json={"loginId": "root", "password": "temporary-admin-pass"},
    )
    bootstrap_token = bootstrap_login_res.json()["data"]["bootstrapToken"]
    superuser_res = client.post(
        "/api/v1/auth/bootstrap/superuser",
        json={
            "bootstrapToken": bootstrap_token,
            "name": "System Admin",
            "loginId": "admin",
            "password": "real-admin-pass",
        },
    )
    admin_token = superuser_res.json()["data"]["accessToken"]

    create_user_res = client.post(
        "/api/v1/auth/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "Normal User", "loginId": "user01", "temporaryPassword": "temporary-pass"},
    )
    assert create_user_res.status_code == 200
    created_user = create_user_res.json()["data"]["user"]
    assert created_user["loginId"] == "user01"
    assert created_user["mustChangePassword"] is True

    user_login_res = client.post(
        "/api/v1/auth/login",
        json={"loginId": "user01", "password": "temporary-pass"},
    )
    assert user_login_res.status_code == 200
    user_login = user_login_res.json()["data"]
    assert user_login["user"]["mustChangePassword"] is True

    change_password_res = client.post(
        "/api/v1/auth/password/change",
        headers={"Authorization": f"Bearer {user_login['accessToken']}"},
        json={"currentPassword": "temporary-pass", "newPassword": "new-user-pass"},
    )
    assert change_password_res.status_code == 200
    assert change_password_res.json()["data"]["user"]["mustChangePassword"] is False

    old_password_res = client.post(
        "/api/v1/auth/login",
        json={"loginId": "user01", "password": "temporary-pass"},
    )
    assert old_password_res.status_code == 401

    new_password_res = client.post(
        "/api/v1/auth/login",
        json={"loginId": "user01", "password": "new-user-pass"},
    )
    assert new_password_res.status_code == 200
    assert new_password_res.json()["data"]["user"]["mustChangePassword"] is False
