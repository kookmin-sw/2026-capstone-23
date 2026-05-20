from __future__ import annotations

from typing import Any, MutableMapping

from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates
from starlette_admin import CustomView

from api.dependencies import get_config
from infra.queue.job_queue import queue_backend_name, queue_sizes
from infra.storage.settings import build_storage_payload
from infra.store import AUTH_SESSIONS, DOCUMENTS, JOBS, JOB_ITEMS, USERS, WORKER_LEASES

from .common import clone_value


def _recent_entries(store: MutableMapping[str, dict[str, Any]], sort_key: str, limit: int = 8) -> list[dict[str, Any]]:
    entries = [clone_value(value) for value in store.values()]
    entries.sort(key=lambda item: str(item.get(sort_key) or ""), reverse=True)
    return entries[:limit]


class AdminDashboardView(CustomView):
    def __init__(self) -> None:
        super().__init__(label="Overview", icon="fa-solid fa-gauge", path="/", template_path="dashboard.html", add_to_menu=False)

    async def render(self, request: Request, templates: Jinja2Templates) -> Response:
        counts = [
            {"label": "Users", "count": len(USERS)},
            {"label": "Documents", "count": len(DOCUMENTS)},
            {"label": "Jobs", "count": len(JOBS)},
            {"label": "Job Items", "count": len(JOB_ITEMS)},
            {"label": "Sessions", "count": len(AUTH_SESSIONS)},
            {"label": "Worker Leases", "count": len(WORKER_LEASES)},
        ]
        warnings: list[str] = []
        user = getattr(request.state, "user", None)
        if user and user.get("mustChangePassword"):
            warnings.append("Current admin account still has mustChangePassword=true.")
        if not any(str(lease.get("status") or "").upper() == "ACTIVE" for lease in WORKER_LEASES.values()):
            warnings.append("No active worker lease is currently registered.")
        return templates.TemplateResponse(
            request=request,
            name=self.template_path,
            context={
                "title": "Overview",
                "counts": counts,
                "queue_info": {"backend": queue_backend_name(), "queues": queue_sizes()},
                "storage": build_storage_payload(get_config()),
                "recent_jobs": _recent_entries(JOBS, "updatedAt"),
                "recent_documents": _recent_entries(DOCUMENTS, "updatedAt"),
                "warnings": warnings,
            },
        )
