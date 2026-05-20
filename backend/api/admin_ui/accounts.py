from __future__ import annotations

from typing import Any, Optional

from starlette.requests import Request
from starlette_admin.exceptions import ActionFailed, FormValidationError
from starlette_admin.fields import BooleanField, EnumField, JSONField, PasswordField, StringField

from api.services.auth_service import hash_password, normalize_login_id, normalize_name, revoke_user_sessions
from infra.store import AUTH_SESSIONS, USERS

from .common import BaseStoreAdminView, JsonStoreAdminView, clone_value


class UsersAdminView(BaseStoreAdminView):
    identity = "users"
    label = "Users"
    name = "User"
    icon = "fa-solid fa-user-shield"
    pk_attr = "userId"
    search_builder = False
    page_size = 25
    searchable_fields = ("userId", "loginId", "name", "role")
    sortable_fields = ("userId", "loginId", "name", "role", "createdAt", "lastLoginAt")
    fields_default_sort = (("createdAt", False),)

    def __init__(self) -> None:
        self.fields = [
            StringField("userId", label="User ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            StringField("loginId", label="Login ID", required=True, maxlength=80),
            StringField("name", label="Name", required=True, maxlength=120),
            EnumField(
                "role",
                label="Role",
                choices=[("SUPERUSER", "SUPERUSER"), ("ADMIN", "ADMIN"), ("USER", "USER")],
                required=True,
            ),
            BooleanField("mustChangePassword", label="Must Change Password"),
            PasswordField(
                "temporaryPassword",
                label="Password / Reset Password",
                required=False,
                exclude_from_list=True,
                exclude_from_detail=True,
            ),
            StringField("createdAt", label="Created At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            StringField("lastLoginAt", label="Last Login At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            JSONField("payload", label="Payload", read_only=True, exclude_from_list=True, exclude_from_create=True, exclude_from_edit=True),
        ]
        super().__init__(store=USERS, allow_create=True, allow_edit=True, allow_delete=True)

    def _current_user(self, request: Request) -> dict[str, Any]:
        user = getattr(request.state, "user", None)
        if not user:
            raise ActionFailed("Missing authenticated admin user.")
        return user

    def _role_allowed(self, request: Request, target_role: str) -> None:
        current_user = self._current_user(request)
        if current_user.get("role") == "SUPERUSER":
            return
        if target_role == "SUPERUSER":
            raise FormValidationError({"role": "Only superuser can assign SUPERUSER role."})

    def build_record_data(self, key: str, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "userId": key,
            "loginId": raw.get("loginId"),
            "name": raw.get("name"),
            "role": raw.get("role"),
            "mustChangePassword": bool(raw.get("mustChangePassword", False)),
            "temporaryPassword": "",
            "createdAt": raw.get("createdAt"),
            "lastLoginAt": raw.get("lastLoginAt"),
            "payload": clone_value(raw),
        }

    def create_raw_record(self, request: Request, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        login_id = str(data.get("loginId") or "")
        name = str(data.get("name") or "")
        role = str(data.get("role") or "USER")
        password = str(data.get("temporaryPassword") or "")

        if len(password) < 8:
            raise FormValidationError({"temporaryPassword": "Password must be at least 8 characters."})

        self._role_allowed(request, role)
        from api.services.auth_service import create_user

        user = create_user(
            login_id=normalize_login_id(login_id),
            name=normalize_name(name),
            password=password,
            role=role,
            must_change_password=True,
        )
        return user["userId"], clone_value(user)

    def edit_raw_record(self, request: Request, key: str, data: dict[str, Any]) -> dict[str, Any]:
        raw = self._get_raw(key)
        current_user = self._current_user(request)
        target_role = str(raw.get("role") or "USER")

        if current_user.get("role") != "SUPERUSER" and target_role == "SUPERUSER":
            raise ActionFailed("Only superuser can edit another superuser.")

        login_id = normalize_login_id(str(data.get("loginId") or raw.get("loginId") or ""))
        name = normalize_name(str(data.get("name") or raw.get("name") or ""))
        role = str(data.get("role") or raw.get("role") or "USER")
        self._role_allowed(request, role)

        for user_id, user in USERS.items():
            if user_id == key:
                continue
            if str(user.get("loginId") or "").lower() == login_id:
                raise FormValidationError({"loginId": "loginId already exists."})

        raw["loginId"] = login_id
        raw["name"] = name
        raw["role"] = role
        raw["mustChangePassword"] = bool(data.get("mustChangePassword", raw.get("mustChangePassword", False)))

        new_password = str(data.get("temporaryPassword") or "").strip()
        if new_password:
            if len(new_password) < 8:
                raise FormValidationError({"temporaryPassword": "Password must be at least 8 characters."})
            raw["passwordHash"] = hash_password(new_password)
            raw["mustChangePassword"] = key != current_user.get("userId")
            keep_token = getattr(request.state, "access_token", None) if key == current_user.get("userId") else None
            revoke_user_sessions(key, keep_token=keep_token)

        return raw

    async def delete(self, request: Request, pks: list[Any]) -> Optional[int]:
        current_user = self._current_user(request)
        removed = 0
        for pk in pks:
            key = str(pk)
            if key == str(current_user.get("userId") or ""):
                raise ActionFailed("You cannot delete your own account.")
            raw = USERS.get(key)
            if raw is None:
                continue
            if raw.get("role") == "SUPERUSER":
                superusers = [user for user in USERS.values() if user.get("role") == "SUPERUSER"]
                if len(superusers) <= 1:
                    raise ActionFailed("Cannot delete the last SUPERUSER account.")
                if current_user.get("role") != "SUPERUSER":
                    raise ActionFailed("Only superuser can delete another superuser.")
            USERS.pop(key, None)
            revoke_user_sessions(key)
            removed += 1
        return removed


class AuthSessionsAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=AUTH_SESSIONS,
            identity="auth-sessions",
            label="Auth Sessions",
            icon="fa-solid fa-key",
            summary_fields=[
                StringField("kind", label="Kind", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("userId", label="User ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("createdAt", label="Created At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("expiresAt", label="Expires At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "kind", "userId"),
            sortable_fields=("recordKey", "kind", "createdAt", "expiresAt"),
            default_sort=(("createdAt", False),),
            allow_delete=True,
        )
