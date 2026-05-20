from __future__ import annotations

import hmac
from typing import Literal, Optional

from fastapi import APIRouter, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from api.common import fail, now_iso, ok
from api.services.auth_service import (
    ADMIN_ROLES,
    authenticate_bearer,
    create_access_session,
    create_bootstrap_session,
    create_user as create_user_record,
    find_user_by_login_id,
    get_session,
    has_superuser,
    hash_password,
    normalize_login_id,
    normalize_name,
    public_user,
    verify_password,
)
from core.env import env_str
from infra.store import AUTH_SESSIONS, USERS


router = APIRouter(prefix="/auth", tags=["auth"])
bearer_scheme = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    loginId: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("loginId")
    @classmethod
    def normalize_login_id_value(cls, value: str) -> str:
        return normalize_login_id(value)


class BootstrapSuperuserRequest(BaseModel):
    bootstrapToken: str = Field(min_length=16, max_length=160)
    name: str = Field(min_length=1, max_length=120)
    loginId: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=256)

    @field_validator("loginId")
    @classmethod
    def normalize_login_id_value(cls, value: str) -> str:
        return normalize_login_id(value)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return normalize_name(value)


class UserCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    loginId: str = Field(min_length=1, max_length=80)
    temporaryPassword: str = Field(min_length=8, max_length=256)
    role: Literal["ADMIN", "USER"] = "USER"

    @field_validator("loginId")
    @classmethod
    def normalize_login_id_value(cls, value: str) -> str:
        return normalize_login_id(value)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return normalize_name(value)


class PasswordChangeRequest(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=256)
    newPassword: str = Field(min_length=8, max_length=256)


@router.get("/bootstrap/status", summary="Bootstrap status")
def bootstrap_status():
    return ok(
        {
            "bootstrapRequired": not has_superuser(),
            "adminEnvConfigured": bool(env_str("ADMIN_ID") and env_str("ADMIN_PW")),
        }
    )


@router.post("/login", summary="Login")
def login(req: LoginRequest):
    if not has_superuser():
        admin_id = env_str("ADMIN_ID", "", strip=True).lower()
        admin_pw = env_str("ADMIN_PW", "")
        if admin_id and admin_pw and req.loginId == admin_id and hmac.compare_digest(req.password, admin_pw):
            return ok(create_bootstrap_session())

    user = find_user_by_login_id(req.loginId)
    if not user or not verify_password(req.password, user.get("passwordHash", "")):
        fail("INVALID_CREDENTIALS", "invalid loginId or password", status=401)

    user["lastLoginAt"] = now_iso()
    USERS[user["userId"]] = user
    return ok(create_access_session(user))


@router.post("/bootstrap/superuser", summary="Create bootstrap superuser")
def create_bootstrap_superuser(req: BootstrapSuperuserRequest):
    if has_superuser():
        fail("CONFLICT", "superuser already exists", status=409)
    get_session(req.bootstrapToken, expected_kind="bootstrap")
    user = create_user_record(
        login_id=req.loginId,
        name=req.name,
        password=req.password,
        role="SUPERUSER",
        must_change_password=False,
    )
    AUTH_SESSIONS.pop(req.bootstrapToken, None)
    return ok(create_access_session(user))


@router.post("/users", summary="Create user")
def create_user(
    req: UserCreateRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
):
    current_user = authenticate_bearer(credentials)
    if current_user.get("role") not in ADMIN_ROLES:
        fail("FORBIDDEN", "admin role required", status=403)
    if current_user.get("mustChangePassword"):
        fail("PASSWORD_CHANGE_REQUIRED", "change password before creating users", status=403)

    user = create_user_record(
        login_id=req.loginId,
        name=req.name,
        password=req.temporaryPassword,
        role=req.role,
        must_change_password=True,
    )
    return ok({"user": public_user(user)})


@router.post("/password/change", summary="Change password")
def change_password(
    req: PasswordChangeRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
):
    user = authenticate_bearer(credentials)
    if not verify_password(req.currentPassword, user.get("passwordHash", "")):
        fail("INVALID_CREDENTIALS", "current password is invalid", status=401)
    if hmac.compare_digest(req.currentPassword, req.newPassword):
        fail("VALIDATION_ERROR", "new password must be different from current password", status=422)

    user["passwordHash"] = hash_password(req.newPassword)
    user["mustChangePassword"] = False
    USERS[user["userId"]] = user
    return ok({"user": public_user(user)})


@router.get("/me", summary="Current user")
def me(credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme)):
    user = authenticate_bearer(credentials)
    return ok({"user": public_user(user)})
