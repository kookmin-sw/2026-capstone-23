from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

_BASE_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "modelId": "m1",
        "code": "openrouter/openai/gpt-5.2",
        "displayName": "GPT-5.2 (OpenRouter)",
        "provider": "openrouter",
        "defaultExecutionBackend": "openrouter",
        "supportedExecutionBackends": ["openrouter"],
        "isActive": True,
    },
    {
        "modelId": "m2",
        "code": "openrouter/openai/gpt-5-mini",
        "displayName": "GPT-5 Mini (OpenRouter)",
        "provider": "openrouter",
        "defaultExecutionBackend": "openrouter",
        "supportedExecutionBackends": ["openrouter"],
        "isActive": True,
    },
    {
        "modelId": "m4",
        "code": "openrouter/qwen3-vl-8b",
        "displayName": "Qwen3-VL 8B (OpenRouter)",
        "provider": "openrouter",
        "defaultExecutionBackend": "openrouter",
        "supportedExecutionBackends": ["openrouter"],
        "isActive": True,
    },
    {
        "modelId": "m5",
        "code": "openrouter/qwen3-vl-32b",
        "displayName": "Qwen3-VL 32B (OpenRouter)",
        "provider": "openrouter",
        "defaultExecutionBackend": "openrouter",
        "supportedExecutionBackends": ["openrouter"],
        "isActive": True,
    },
]

_LOCAL_QWEN_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "modelId": "m3",
        "code": "qwen2.5-vl-7b",
        "displayName": "Qwen2.5-VL 7B (Local)",
        "provider": "local",
        "defaultExecutionBackend": "qwen_gpu",
        "supportedExecutionBackends": ["qwen_gpu"],
        "isActive": True,
    },
]

_LEGACY_MANAGED_MODEL_CODES = {
    "gpt-5.2",
    "gpt-5-mini",
}

MANAGED_MODEL_CODES = {
    model["code"] for model in [*_BASE_MODEL_CATALOG, *_LOCAL_QWEN_MODEL_CATALOG]
} | _LEGACY_MANAGED_MODEL_CODES


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def default_model_catalog(
    *,
    include_local_qwen: bool | None = None,
) -> list[dict[str, Any]]:
    include_qwen = (
        _env_bool("ENABLE_LOCAL_QWEN_MODEL", False)
        if include_local_qwen is None
        else include_local_qwen
    )
    catalog = deepcopy(_BASE_MODEL_CATALOG)
    if include_qwen:
        catalog[2:2] = deepcopy(_LOCAL_QWEN_MODEL_CATALOG)
    return catalog
