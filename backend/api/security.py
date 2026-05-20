from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from fastapi import HTTPException, Request, WebSocket
from starlette.responses import JSONResponse

from api.common import error_payload, fail
from api.services.auth_service import ADMIN_ROLES, authenticate_access_token
from core.env import env_bool
from core.version import API_PREFIX, PUBLIC_API_PREFIX


AUTH_DISABLED_ENV = "AUTH_DISABLED"
AUTH_REQUIRED_ENV = "AUTH_REQUIRED"

_PUBLIC_API_EXACT = {
    "/health",
    "/health/ready",
    "/auth/bootstrap/status",
    "/auth/login",
    "/auth/bootstrap/superuser",
}
_PASSWORD_CHANGE_ALLOWED = {
    "/auth/me",
    "/auth/password/change",
}
_ADMIN_API_EXACT = {
    "/auth/users",
    "/parser/queue/stats",
}
_USER_MONITORING_API_EXACT = {
    "/monitoring/system",
    "/monitoring/errors",
    "/monitoring/errors/summary",
}
_USER_MONITORING_API_PREFIXES = (
    "/monitoring/errors",
)
_ADMIN_API_PREFIXES = (
    "/admin",
    "/monitoring",
    "/workspace",
    "/batch",
    "/scheduler",
    "/process",
)
_ADMIN_ROLES = {role.upper() for role in ADMIN_ROLES}


@dataclass(frozen=True)
class AuthPolicy:
    api_path: str | None
    require_auth: bool
    require_admin: bool = False

    @property
    def is_public(self) -> bool:
        return not self.require_auth


def auth_is_enabled() -> bool:
    if env_bool(AUTH_DISABLED_ENV, False):
        return False
    return env_bool(AUTH_REQUIRED_ENV, True)


def normalize_api_path(path: str) -> str | None:
    normalized = path.rstrip("/") or "/"
    for prefix in (PUBLIC_API_PREFIX, API_PREFIX):
        if normalized == prefix:
            return "/"
        if normalized.startswith(f"{prefix}/"):
            return normalized[len(prefix) :] or "/"
    return None


def resolve_http_auth_policy(path: str) -> AuthPolicy:
    api_path = normalize_api_path(path)
    if api_path is None:
        return AuthPolicy(api_path=None, require_auth=False)
    if api_path in _PUBLIC_API_EXACT:
        return AuthPolicy(api_path=api_path, require_auth=False)
    if api_path in _USER_MONITORING_API_EXACT or any(
        api_path.startswith(f"{prefix}/") for prefix in _USER_MONITORING_API_PREFIXES
    ):
        return AuthPolicy(api_path=api_path, require_auth=True, require_admin=False)

    require_admin = api_path in _ADMIN_API_EXACT or any(
        api_path == prefix or api_path.startswith(f"{prefix}/") for prefix in _ADMIN_API_PREFIXES
    )
    return AuthPolicy(api_path=api_path, require_auth=True, require_admin=require_admin)


def bearer_token_from_authorization(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def is_admin_user(user: dict[str, Any] | None) -> bool:
    return bool(user and str(user.get("role") or "").upper() in _ADMIN_ROLES)


def record_owner_id(record: dict[str, Any] | None) -> str | None:
    if not record:
        return None
    for key in ("ownerUserId", "userId", "createdBy", "requesterUserId"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def get_request_user(request: Request | None) -> dict[str, Any] | None:
    if request is None:
        return None
    return getattr(request.state, "current_user", None)


def current_user_id(request: Request | None, *, fallback_user_id: str | None = None) -> str | None:
    user = get_request_user(request)
    if user:
        return str(user.get("userId") or "")
    return fallback_user_id


def request_auth_was_enforced(request: Request | None) -> bool:
    if request is None:
        return False
    return bool(getattr(request.state, "auth_checked", False))


def require_record_access(
    request: Request | None,
    record: dict[str, Any],
    *,
    resource: str,
    resource_id: str,
) -> None:
    user = get_request_user(request)
    if user is None:
        if request_auth_was_enforced(request):
            fail("UNAUTHORIZED", "missing authenticated user", status=401)
        return

    if is_admin_user(user):
        return

    owner_id = record_owner_id(record)
    if owner_id and owner_id == str(user.get("userId") or ""):
        return

    fail(
        "FORBIDDEN",
        f"{resource} access denied: {resource_id}",
        status=403,
        details={"resource": resource, "id": resource_id},
    )


def filter_records_for_user(request: Request | None, records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    user = get_request_user(request)
    items = list(records)
    if user is None or is_admin_user(user):
        return items
    user_id = str(user.get("userId") or "")
    return [item for item in items if record_owner_id(item) == user_id]


def _auth_error_response(
    code: str,
    message: str,
    *,
    status_code: int,
    details: Optional[dict[str, Any]] = None,
) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code=code, message=message, details=details),
        headers=headers,
    )


async def auth_middleware(request: Request, call_next):
    request.state.auth_checked = False
    request.state.current_user = None

    if not auth_is_enabled():
        return await call_next(request)

    policy = resolve_http_auth_policy(request.url.path)
    if policy.is_public:
        return await call_next(request)

    token = bearer_token_from_authorization(request.headers.get("authorization"))
    if token is None:
        return _auth_error_response("UNAUTHORIZED", "missing bearer token", status_code=401)

    try:
        user = authenticate_access_token(token)
    except HTTPException as exc:
        payload = exc.detail if isinstance(exc.detail, dict) else None
        if payload and payload.get("error"):
            return JSONResponse(status_code=exc.status_code, content=payload, headers={"WWW-Authenticate": "Bearer"})
        return _auth_error_response("UNAUTHORIZED", "invalid bearer token", status_code=401)

    request.state.current_user = user
    request.state.auth_checked = True

    if user.get("mustChangePassword") and policy.api_path not in _PASSWORD_CHANGE_ALLOWED:
        return _auth_error_response(
            "PASSWORD_CHANGE_REQUIRED",
            "change password before using this API",
            status_code=403,
        )

    if policy.require_admin and not is_admin_user(user):
        return _auth_error_response("FORBIDDEN", "admin role required", status_code=403)

    return await call_next(request)


def websocket_token(websocket: WebSocket) -> str | None:
    token = websocket.query_params.get("accessToken") or websocket.query_params.get("token")
    if token:
        return token.strip()
    return bearer_token_from_authorization(websocket.headers.get("authorization"))


async def authenticate_websocket(
    websocket: WebSocket,
    *,
    require_admin: bool = False,
    record: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    token = websocket_token(websocket)
    if token is None:
        await websocket.close(code=1008)
        return None

    try:
        user = authenticate_access_token(token)
    except HTTPException:
        await websocket.close(code=1008)
        return None

    if user.get("mustChangePassword"):
        await websocket.close(code=1008)
        return None

    if require_admin and not is_admin_user(user):
        await websocket.close(code=1008)
        return None

    if record is not None and not is_admin_user(user):
        owner_id = record_owner_id(record)
        if not owner_id or owner_id != str(user.get("userId") or ""):
            await websocket.close(code=1008)
            return None

    return user
