from typing import Optional

from core.env import env_str

from core.model_catalog import get_model_catalog


OPENAI_BACKEND = "openai"
OPENROUTER_BACKEND = "openrouter"
QWEN_GPU_BACKEND = "qwen_gpu"
AUTO_BACKEND = "auto"
DEFAULT_QUEUE_ROUTE = "default"
OPENAI_QUEUE_ROUTE = "openai"
OPENROUTER_QUEUE_ROUTE = "openrouter"
QWEN_GPU_QUEUE_ROUTE = "qwen_gpu"
QWEN_DOC_QUEUE_ROUTE = "qwen_doc"
QWEN_INFER_QUEUE_ROUTE = "qwen_infer"
QWEN_FINALIZE_QUEUE_ROUTE = "qwen_finalize"

SUPPORTED_EXECUTION_BACKENDS = {
    OPENAI_BACKEND,
    OPENROUTER_BACKEND,
    QWEN_GPU_BACKEND,
    AUTO_BACKEND,
}
SUPPORTED_WORKER_MODES = {
    OPENAI_BACKEND,
    OPENROUTER_BACKEND,
    QWEN_GPU_BACKEND,
    "qwen_doc",
    "qwen_infer",
    "qwen_finalize",
    "all",
}
KNOWN_QUEUE_ROUTES = (
    OPENAI_QUEUE_ROUTE,
    OPENROUTER_QUEUE_ROUTE,
    QWEN_GPU_QUEUE_ROUTE,
    QWEN_DOC_QUEUE_ROUTE,
    QWEN_INFER_QUEUE_ROUTE,
    QWEN_FINALIZE_QUEUE_ROUTE,
)


def normalize_execution_backend(value: Optional[str], *, default: str = AUTO_BACKEND) -> str:
    normalized = (value or default).strip().lower()
    if normalized not in SUPPORTED_EXECUTION_BACKENDS:
        return default
    return normalized


def normalize_worker_mode(value: Optional[str], *, default: str = "all") -> str:
    normalized = (value or default).strip().lower()
    if normalized not in SUPPORTED_WORKER_MODES:
        return default
    return normalized


def find_model_metadata(model_id: Optional[str] = None, model_code: Optional[str] = None) -> Optional[dict]:
    for model in get_model_catalog():
        if model_id and model.get("modelId") == model_id:
            return model
        if model_code and model.get("code") == model_code:
            return model
    return None


def supported_execution_backends_for_model(
    *,
    model_id: Optional[str] = None,
    model_code: Optional[str] = None,
) -> set[str]:
    model = find_model_metadata(model_id=model_id, model_code=model_code)
    if not model:
        return {OPENAI_BACKEND, QWEN_GPU_BACKEND}

    configured = {
        str(backend).strip().lower()
        for backend in model.get("supportedExecutionBackends", [])
        if str(backend).strip()
    }
    if configured:
        return configured

    provider = str(model.get("provider") or "").strip().lower()
    if provider == "local":
        return {QWEN_GPU_BACKEND}
    if provider == "openrouter":
        return {OPENROUTER_BACKEND}
    return {OPENAI_BACKEND}


def is_qwen_vl_model(*, model_id: Optional[str] = None, model_code: Optional[str] = None) -> bool:
    model = find_model_metadata(model_id=model_id, model_code=model_code)
    if model is None:
        code = (model_code or "").strip().lower()
        return code.startswith("qwen2.5-vl")
    code = str(model.get("code") or "").strip().lower()
    return code.startswith("qwen2.5-vl")


def queue_route_for_backend(
    execution_backend: str,
    *,
    model_id: Optional[str] = None,
    model_code: Optional[str] = None,
) -> str:
    normalized = normalize_execution_backend(execution_backend, default=OPENAI_BACKEND)
    if normalized == OPENROUTER_BACKEND:
        return OPENROUTER_QUEUE_ROUTE
    if normalized == QWEN_GPU_BACKEND:
        if is_qwen_vl_model(model_id=model_id, model_code=model_code):
            return QWEN_DOC_QUEUE_ROUTE
        return QWEN_GPU_QUEUE_ROUTE
    return OPENAI_QUEUE_ROUTE


def resolve_execution_backend(
    *,
    requested_backend: Optional[str],
    model_id: Optional[str] = None,
    model_code: Optional[str] = None,
) -> str:
    normalized = normalize_execution_backend(requested_backend)
    supported = supported_execution_backends_for_model(model_id=model_id, model_code=model_code)
    if normalized != AUTO_BACKEND:
        return normalized
    if supported == {QWEN_GPU_BACKEND}:
        return QWEN_GPU_BACKEND
    if supported == {OPENAI_BACKEND}:
        return OPENAI_BACKEND
    if supported == {OPENROUTER_BACKEND}:
        return OPENROUTER_BACKEND

    default_backend = normalize_execution_backend(env_str("DEFAULT_AUTO_EXECUTION_BACKEND", ""), default="")
    if default_backend in supported:
        return default_backend
    return OPENAI_BACKEND


def worker_queue_routes(worker_mode: Optional[str]) -> list[str]:
    normalized = normalize_worker_mode(worker_mode)
    if normalized == "all":
        return [OPENAI_QUEUE_ROUTE, OPENROUTER_QUEUE_ROUTE, QWEN_GPU_QUEUE_ROUTE]
    if normalized == OPENAI_BACKEND:
        # Compatibility: existing worker-openai deployments also execute OpenRouter
        # requests because both paths use the OpenAI-compatible VLM client.
        return [OPENAI_QUEUE_ROUTE, OPENROUTER_QUEUE_ROUTE]
    if normalized == OPENROUTER_BACKEND:
        return [OPENROUTER_QUEUE_ROUTE]
    if normalized == "qwen_doc":
        return [QWEN_DOC_QUEUE_ROUTE]
    if normalized == "qwen_infer":
        return [QWEN_INFER_QUEUE_ROUTE]
    if normalized == "qwen_finalize":
        return [QWEN_FINALIZE_QUEUE_ROUTE]
    return [queue_route_for_backend(normalized)]
