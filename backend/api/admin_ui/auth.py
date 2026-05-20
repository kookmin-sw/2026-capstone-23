from __future__ import annotations

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette_admin.auth import AdminConfig, AdminUser, AuthProvider
from starlette_admin.exceptions import LoginFailed

from api.common import now_iso
from api.services.auth_service import (
    ADMIN_ROLES,
    authenticate_access_token,
    create_access_session,
    find_user_by_login_id,
    revoke_access_token,
    verify_password,
)
from core.env import env_str
from infra.store import USERS


def admin_secret_key() -> str:
    return env_str("ADMIN_UI_SECRET_KEY", "change-me-admin-ui-secret")


class AdminUIAuthProvider(AuthProvider):
    async def login(
        self,
        username: str,
        password: str,
        remember_me: bool,
        request: Request,
        response: Response,
    ) -> Response:
        _ = remember_me
        user = find_user_by_login_id(username)
        if not user or not verify_password(password, str(user.get("passwordHash") or "")):
            raise LoginFailed("Invalid username or password")
        if user.get("role") not in ADMIN_ROLES:
            raise LoginFailed("Admin role required")

        user["lastLoginAt"] = now_iso()
        USERS[user["userId"]] = user
        access_session = create_access_session(user)
        request.session.clear()
        request.session.update({"adminAccessToken": access_session["accessToken"]})
        request.state.user = user
        request.state.access_token = access_session["accessToken"]
        return response

    async def is_authenticated(self, request: Request) -> bool:
        token = request.session.get("adminAccessToken")
        if not token:
            return False
        try:
            user = authenticate_access_token(str(token))
        except HTTPException:
            request.session.clear()
            return False

        if user.get("role") not in ADMIN_ROLES:
            request.session.clear()
            return False

        request.state.user = user
        request.state.access_token = str(token)
        return True

    def get_admin_config(self, request: Request) -> AdminConfig:
        user = getattr(request.state, "user", None)
        suffix = f" [{user.get('role')}]" if user else ""
        return AdminConfig(app_title=f"Luminir Admin{suffix}")

    def get_admin_user(self, request: Request) -> AdminUser:
        user = getattr(request.state, "user", None) or {}
        username = str(user.get("name") or user.get("loginId") or "admin")
        return AdminUser(username=username, photo_url=None)

    async def logout(self, request: Request, response: Response) -> Response:
        token = request.session.get("adminAccessToken")
        if token:
            revoke_access_token(str(token))
        request.session.clear()
        return response
