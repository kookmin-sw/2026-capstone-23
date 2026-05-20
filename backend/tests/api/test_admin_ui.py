import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from api.admin_ui import mount_admin_ui
from api.services.auth_service import create_user
from infra.store import (
    ADMIN_SETTINGS,
    AUTH_SESSIONS,
    DOCUMENTS,
    DOCUMENT_CACHE,
    JOBS,
    JOB_EVENTS,
    JOB_ITEMS,
    QWEN_FINALIZE_TASKS,
    QWEN_INFER_RESULTS,
    QWEN_INFER_TASKS,
    QWEN_PREPROCESS_TASKS,
    RAG_SESSIONS,
    USERS,
    WORKER_LEASES,
)


def setup_function():
    for store in (
        USERS,
        AUTH_SESSIONS,
        ADMIN_SETTINGS,
        DOCUMENTS,
        DOCUMENT_CACHE,
        JOBS,
        JOB_ITEMS,
        JOB_EVENTS,
        RAG_SESSIONS,
        WORKER_LEASES,
        QWEN_PREPROCESS_TASKS,
        QWEN_INFER_TASKS,
        QWEN_INFER_RESULTS,
        QWEN_FINALIZE_TASKS,
    ):
        store.clear()


def make_client() -> TestClient:
    app = FastAPI()
    mount_admin_ui(app)
    return TestClient(app)


def test_admin_ui_login_and_list_page_renders():
    create_user(
        login_id="admin",
        name="System Admin",
        password="super-secret-123",
        role="SUPERUSER",
        must_change_password=False,
    )
    client = make_client()

    login_page = client.get("/admin/login")
    assert login_page.status_code == 200
    assert "Login to your account" in login_page.text

    login_res = client.post(
        "/admin/login",
        data={"username": "admin", "password": "super-secret-123", "remember_me": "on"},
        follow_redirects=False,
    )
    assert login_res.status_code in {302, 303}
    assert login_res.headers["location"] == "http://testserver/admin/"

    dashboard_res = client.get("/admin")
    assert dashboard_res.status_code == 200
    assert "Overview" in dashboard_res.text
    assert "Luminir Admin" in dashboard_res.text

    users_api_res = client.get("/admin/api/users?skip=0&limit=10")
    assert users_api_res.status_code == 200
    payload = users_api_res.json()
    assert payload["total"] == 1
    assert payload["items"][0]["name"] == "System Admin"


def test_admin_ui_rejects_non_admin_login():
    create_user(
        login_id="user01",
        name="Plain User",
        password="plain-user-pass",
        role="USER",
        must_change_password=False,
    )
    client = make_client()

    login_res = client.post(
        "/admin/login",
        data={"username": "user01", "password": "plain-user-pass"},
    )
    assert login_res.status_code == 400
    assert "Admin role required" in login_res.text
