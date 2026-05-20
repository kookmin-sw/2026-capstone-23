from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi.security import HTTPAuthorizationCredentials

from api.common import fail, now_iso
from infra.store import AUTH_SESSIONS, USERS

SESSION_TTL_DAYS = 30
BOOTSTRAP_TTL_MINUTES = 15
PBKDF2_ITERATIONS = 210_000
ADMIN_ROLES = {"SUPERUSER", "ADMIN"}


def normalize_login_id(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("loginId must not be empty")
    return normalized


def normalize_name(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("name must not be empty")
    return normalized


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_text.encode("ascii"))
        expected = base64.b64decode(digest_text.encode("ascii"))
    except Exception:
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "userId": user["userId"],
        "loginId": user["loginId"],
        "name": user["name"],
        "role": user.get("role", "USER"),
        "mustChangePassword": bool(user.get("mustChangePassword", False)),
        "createdAt": user["createdAt"],
        "lastLoginAt": user.get("lastLoginAt"),
    }


def find_user_by_login_id(login_id: str) -> Optional[dict[str, Any]]:
    normalized = normalize_login_id(login_id)
    for user in USERS.values():
        if str(user.get("loginId", "")).lower() == normalized:
            return user
    return None


def has_superuser() -> bool:
    return any(user.get("role") == "SUPERUSER" for user in USERS.values())


def create_access_session(user: dict[str, Any]) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    expires_at = (utc_now() + timedelta(days=SESSION_TTL_DAYS)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    AUTH_SESSIONS[token] = {
        "kind": "access",
        "token": token,
        "userId": user["userId"],
        "createdAt": now_iso(),
        "expiresAt": expires_at,
    }
    return {
        "accessToken": token,
        "tokenType": "bearer",
        "expiresAt": expires_at,
        "user": public_user(user),
    }


def create_bootstrap_session() -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    expires_at = (utc_now() + timedelta(minutes=BOOTSTRAP_TTL_MINUTES)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    AUTH_SESSIONS[token] = {
        "kind": "bootstrap",
        "token": token,
        "createdAt": now_iso(),
        "expiresAt": expires_at,
    }
    return {
        "bootstrapRequired": True,
        "bootstrapToken": token,
        "expiresAt": expires_at,
        "next": "SIGNUP_SUPERUSER",
    }


def get_session(token: str, expected_kind: str = "access") -> dict[str, Any]:
    session = AUTH_SESSIONS.get(token)
    if not session or session.get("kind") != expected_kind:
        fail("UNAUTHORIZED", "invalid bearer token", status=401)
    expires_at = parse_iso(session.get("expiresAt"))
    if expires_at and expires_at < utc_now():
        AUTH_SESSIONS.pop(token, None)
        fail("UNAUTHORIZED", "expired bearer token", status=401)
    return session


def authenticate_access_token(token: str) -> dict[str, Any]:
    session = get_session(token, expected_kind="access")
    user_id = session.get("userId")
    if not user_id:
        AUTH_SESSIONS.pop(token, None)
        fail("UNAUTHORIZED", "invalid bearer token", status=401)
    user = USERS.get(str(user_id))
    if not user:
        AUTH_SESSIONS.pop(token, None)
        fail("UNAUTHORIZED", "session user not found", status=401)
    return user


def authenticate_bearer(credentials: Optional[HTTPAuthorizationCredentials]) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        fail("UNAUTHORIZED", "missing bearer token", status=401)
    return authenticate_access_token(credentials.credentials.strip())


def create_user(*, login_id: str, name: str, password: str, role: str, must_change_password: bool) -> dict[str, Any]:
    if find_user_by_login_id(login_id):
        fail("CONFLICT", "user already exists", status=409)

    now = now_iso()
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    user = {
        "userId": user_id,
        "loginId": normalize_login_id(login_id),
        "name": normalize_name(name),
        "role": role,
        "passwordHash": hash_password(password),
        "mustChangePassword": must_change_password,
        "createdAt": now,
        "lastLoginAt": None,
    }
    USERS[user_id] = user
    return user


def revoke_access_token(token: str) -> None:
    AUTH_SESSIONS.pop(token, None)


def revoke_user_sessions(user_id: str, *, keep_token: str | None = None) -> int:
    removed = 0
    for token, session in list(AUTH_SESSIONS.items()):
        if session.get("kind") != "access":
            continue
        if str(session.get("userId") or "") != user_id:
            continue
        if keep_token and token == keep_token:
            continue
        AUTH_SESSIONS.pop(token, None)
        removed += 1
    return removed


def require_admin_role(user: dict[str, Any]) -> None:
    if user.get("role") not in ADMIN_ROLES:
        fail("FORBIDDEN", "admin role required", status=403)
