from importlib import import_module
from typing import Any


_ROUTER_EXPORTS = {
    "admin_router": ("api.routers.admin", "router"),
    "auth_router": ("api.routers.auth", "router"),
    "batch_router": ("api.routers.batch", "router"),
    "dashboard_router": ("api.routers.dashboard", "router"),
    "documents_router": ("api.routers.documents", "router"),
    "health_router": ("api.routers.health", "router"),
    "models_router": ("api.routers.models", "router"),
    "monitoring_router": ("api.routers.monitoring", "router"),
    "parser_router": ("api.routers.parser", "router"),
    "process_router": ("api.routers.process", "router"),
    "rag_router": ("api.routers.rag", "router"),
    "scheduler_router": ("api.routers.scheduler", "router"),
    "workspace_router": ("api.routers.workspace", "router"),
    "ws_router": ("api.routers.ws", "router"),
}

__all__ = list(_ROUTER_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _ROUTER_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
