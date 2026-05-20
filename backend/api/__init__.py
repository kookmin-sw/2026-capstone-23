import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from core.env import env_bool, env_int, env_str
from core.version import API_PREFIX, APP_TITLE, APP_VERSION

from api.dependencies import set_runtime_services
from api.security import auth_middleware, resolve_http_auth_policy

API_DESCRIPTION = """
Document job creation, status lookup, batch processing, RAG queries, workspace file
management, and monitoring endpoints for the Luminir backend.
""".strip()

OPENAPI_TAGS = [
    {"name": "health", "description": "Health checks and runtime readiness."},
    {"name": "auth", "description": "Bootstrap and account/session endpoints."},
    {"name": "models", "description": "Available model catalog endpoints."},
    {"name": "files", "description": "Frontend-compatible upload and download endpoints."},
    {"name": "process", "description": "Immediate single-file processing endpoints."},
    {"name": "parser", "description": "Async job creation, status, cancel, and result endpoints."},
    {"name": "documents", "description": "Stored document metadata and result lookup endpoints."},
    {"name": "dashboard", "description": "Dashboard summary and recent activity endpoints."},
    {"name": "monitoring", "description": "Monitoring and log lookup endpoints."},
    {"name": "admin", "description": "Admin settings and management endpoints."},
    {"name": "rag", "description": "Document-grounded RAG query endpoints."},
    {"name": "workspace", "description": "Workspace file browsing and download endpoints."},
    {"name": "batch", "description": "Background batch processing endpoints."},
    {"name": "scheduler", "description": "Scheduled processing configuration endpoints."},
    {"name": "ws", "description": "WebSocket progress streaming endpoints."},
]


def _load_routers():
    from api.routers.admin import router as admin_router
    from api.routers.auth import router as auth_router
    from api.routers.batch import router as batch_router
    from api.routers.dashboard import router as dashboard_router
    from api.routers.documents import router as documents_router
    from api.routers.files import router as files_router
    from api.routers.health import router as health_router
    from api.routers.models import router as models_router
    from api.routers.monitoring import router as monitoring_router
    from api.routers.parser import router as parser_router
    from api.routers.process import router as process_router
    from api.routers.rag import router as rag_router
    from api.routers.scheduler import router as scheduler_router
    from api.routers.workspace import router as workspace_router
    from api.routers.ws import router as ws_router

    return [
        health_router,
        auth_router,
        process_router,
        models_router,
        files_router,
        parser_router,
        documents_router,
        dashboard_router,
        monitoring_router,
        admin_router,
        rag_router,
        ws_router,
        workspace_router,
        batch_router,
        scheduler_router,
    ]


def register_api_routes(
    target_app: FastAPI,
    auto_processor=None,
    config=None,
    pipeline=None,
) -> FastAPI:
    set_runtime_services(
        config_override=config,
        pipeline_override=pipeline,
        auto_processor_override=auto_processor,
    )

    for router in _load_routers():
        target_app.include_router(router, prefix=API_PREFIX)
    return target_app


def _normalize_binary_schema(node):
    if isinstance(node, dict):
        media_type = node.get("contentMediaType")
        if node.get("type") == "string" and media_type == "application/octet-stream":
            node.pop("contentMediaType", None)
            node["format"] = "binary"
        for value in node.values():
            _normalize_binary_schema(value)
    elif isinstance(node, list):
        for value in node:
            _normalize_binary_schema(value)


def _install_openapi(target_app: FastAPI) -> None:
    def custom_openapi():
        if target_app.openapi_schema:
            return target_app.openapi_schema
        schema = get_openapi(
            title=target_app.title,
            version=APP_VERSION,
            description=API_DESCRIPTION,
            routes=target_app.routes,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque-session-token",
        }
        for path, path_item in schema.get("paths", {}).items():
            policy = resolve_http_auth_policy(path)
            if policy.is_public:
                continue
            for operation in path_item.values():
                if isinstance(operation, dict):
                    operation.setdefault("security", [{"BearerAuth": []}])
        _normalize_binary_schema(schema)
        target_app.openapi_schema = schema
        return target_app.openapi_schema

    target_app.openapi = custom_openapi


def _install_auth_middleware(target_app: FastAPI) -> None:
    if getattr(target_app.state, "auth_middleware_installed", False):
        return
    target_app.middleware("http")(auth_middleware)
    target_app.state.auth_middleware_installed = True


def _startup_inline_workers(target_app: FastAPI) -> None:
    from api.ws_hub import start_job_progress_bridge

    start_job_progress_bridge()

    if env_bool("ENABLE_INLINE_EXEC_WORKER", True):
        from api.queue_worker import run_worker

        exec_stop_event = threading.Event()
        exec_thread = threading.Thread(
            target=run_worker,
            kwargs={
                "worker_id": "worker-inline-1",
                "stop_event": exec_stop_event,
                "worker_mode": env_str("INLINE_WORKER_MODE", env_str("WORKER_MODE", "all")),
            },
            daemon=True,
        )
        exec_thread.start()
        target_app.state.exec_worker_stop_event = exec_stop_event
        target_app.state.exec_worker_thread = exec_thread
        print("[worker] inline exec worker enabled")

    if env_bool("ENABLE_INLINE_RECOVERY_WORKER", True):
        from worker.recovery import run_recovery_worker

        recovery_stop_event = threading.Event()
        recovery_thread = threading.Thread(
            target=run_recovery_worker,
            kwargs={"worker_id": "recovery-inline-1", "stop_event": recovery_stop_event},
            daemon=True,
        )
        recovery_thread.start()
        target_app.state.recovery_worker_stop_event = recovery_stop_event
        target_app.state.recovery_worker_thread = recovery_thread
        print("[worker] inline recovery worker enabled")


def _shutdown_inline_workers(target_app: FastAPI) -> None:
    from api.ws_hub import stop_job_progress_bridge

    for key in ("exec_worker_stop_event", "recovery_worker_stop_event"):
        stop_event = getattr(target_app.state, key, None)
        if stop_event is not None:
            stop_event.set()
    stop_job_progress_bridge()


@asynccontextmanager
async def _lifespan(target_app: FastAPI):
    from core.onprem_validation import validate_onprem_runtime

    validate_onprem_runtime()
    _startup_inline_workers(target_app)
    try:
        yield
    finally:
        _shutdown_inline_workers(target_app)


def create_app(*, auto_processor=None, config=None, pipeline=None) -> FastAPI:
    target_app = FastAPI(
        title=APP_TITLE,
        description=API_DESCRIPTION,
        root_path="/api",
        version=APP_VERSION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=_lifespan,
    )
    register_api_routes(
        target_app,
        auto_processor=auto_processor,
        config=config,
        pipeline=pipeline,
    )
    _install_auth_middleware(target_app)
    try:
        from api.admin_ui import mount_admin_ui
    except ModuleNotFoundError as exc:
        print(f"[WARNING] Admin UI mount skipped: missing optional dependency {exc.name}")
    else:
        mount_admin_ui(target_app)
    _install_openapi(target_app)
    return target_app


_app_instance: FastAPI | None = None


def get_app() -> FastAPI:
    global _app_instance
    if _app_instance is None:
        _app_instance = create_app()
    return _app_instance


class _LazyApp:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_app(), name)

    async def __call__(self, scope, receive, send) -> None:
        await get_app()(scope, receive, send)


app = _LazyApp()


if __name__ == "__main__":
    import uvicorn

    host = env_str("UVICORN_HOST", "127.0.0.1", strip=True) or "127.0.0.1"
    port = env_int("UVICORN_PORT", 8000, minimum=1, maximum=65535)
    uvicorn.run(get_app(), host=host, port=port)
