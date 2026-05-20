from __future__ import annotations

import uuid
from typing import Any, Iterable

from fastapi import Request

from api.common import now_iso
from api.security import get_request_user
from infra.store import AUDIT_LOGS


def _client_host(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def _request_details(request: Request | None) -> dict[str, Any]:
    if request is None:
        return {}
    return {
        "method": request.method,
        "path": request.url.path,
        "clientHost": _client_host(request),
        "userAgent": request.headers.get("user-agent"),
    }


def record_audit_event(
    *,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    outcome: str = "SUCCESS",
    request: Request | None = None,
    actor_user_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user = get_request_user(request)
    event_id = f"aud_{uuid.uuid4().hex[:16]}"
    payload = {
        "auditId": event_id,
        "action": action,
        "resourceType": resource_type,
        "resourceId": resource_id,
        "outcome": outcome,
        "actorUserId": actor_user_id or (str(user.get("userId")) if user else None),
        "actorRole": str(user.get("role")) if user else None,
        "details": details or {},
        "request": _request_details(request),
        "createdAt": now_iso(),
    }
    AUDIT_LOGS[event_id] = payload
    return payload


def list_audit_events(
    *,
    limit: int,
    offset: int = 0,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    outcome: str | None = None,
    actor_user_id: str | None = None,
) -> dict[str, Any]:
    events: Iterable[dict[str, Any]] = AUDIT_LOGS.values()
    if action:
        events = [event for event in events if event.get("action") == action]
    if resource_type:
        events = [event for event in events if event.get("resourceType") == resource_type]
    if resource_id:
        events = [event for event in events if event.get("resourceId") == resource_id]
    if outcome:
        events = [event for event in events if event.get("outcome") == outcome]
    if actor_user_id:
        events = [event for event in events if event.get("actorUserId") == actor_user_id]

    items = list(events)
    items.sort(key=lambda item: (str(item.get("createdAt") or ""), str(item.get("auditId") or "")), reverse=True)
    return {"items": items[offset : offset + limit], "total": len(items), "limit": limit, "offset": offset}
