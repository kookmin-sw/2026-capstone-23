from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from core.env import env_bool, env_str


class OnPremValidationError(RuntimeError):
    """Raised when on-prem deployment safety requirements are not met."""


_PLACEHOLDER_VALUES = {
    "",
    "admin",
    "password",
    "changeme",
    "change-me",
    "change-me-before-use",
    "change-me-admin-ui-secret",
    "change-me-document-encryption-key",
    "luminir-local-password",
    "replace-with-secret",
    "replace_with_secret",
}


def onprem_validation_enabled() -> bool:
    if not env_bool("ONPREM_DEPLOYMENT", False):
        return False
    return env_bool("ONPREM_STARTUP_VALIDATION", True)


def _value(name: str) -> str:
    return env_str(name, "", strip=True)


def _looks_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in _PLACEHOLDER_VALUES or normalized.startswith("change-me")


def _require_secret(errors: list[str], name: str, *, minimum_length: int = 16) -> None:
    value = _value(name)
    if not value:
        errors.append(f"{name} is required")
        return
    if _looks_placeholder(value):
        errors.append(f"{name} must not use a placeholder/default value")
        return
    if len(value) < minimum_length:
        errors.append(f"{name} must be at least {minimum_length} characters")


def _require_admin_identity(errors: list[str]) -> None:
    admin_id = _value("ADMIN_ID")
    admin_pw = _value("ADMIN_PW")
    if not admin_id or _looks_placeholder(admin_id):
        errors.append("ADMIN_ID must be set to a non-placeholder administrator id")
    if not admin_pw or _looks_placeholder(admin_pw):
        errors.append("ADMIN_PW must be set to a non-placeholder password")
    elif len(admin_pw) < 12:
        errors.append("ADMIN_PW must be at least 12 characters")


def _validate_auth_policy(errors: list[str]) -> None:
    if env_bool("AUTH_DISABLED", False):
        errors.append("AUTH_DISABLED must be false for on-prem deployment")
    if not env_bool("AUTH_REQUIRED", True):
        errors.append("AUTH_REQUIRED must be true for on-prem deployment")


def _rabbitmq_password_from_url() -> str:
    raw_url = _value("RABBITMQ_URL")
    if not raw_url:
        return ""
    try:
        parsed = urlparse(raw_url)
        return parsed.password or ""
    except Exception:  # noqa: BLE001
        return ""


def _validate_rabbitmq_secret(errors: list[str]) -> None:
    password = _value("RABBITMQ_PASSWORD") or _rabbitmq_password_from_url()
    if not password:
        errors.append("RABBITMQ_PASSWORD or RABBITMQ_URL password is required")
    elif _looks_placeholder(password):
        errors.append("RABBITMQ_PASSWORD must not use the local default value")
    elif len(password) < 16:
        errors.append("RABBITMQ_PASSWORD must be at least 16 characters")


def _validate_queue(errors: list[str]) -> None:
    backend = _value("QUEUE_BACKEND").lower() or "rabbitmq"
    if backend != "rabbitmq":
        errors.append("QUEUE_BACKEND must be rabbitmq for on-prem distributed workers")
    if env_bool("QUEUE_MEMORY_FALLBACK_ENABLED", False):
        errors.append("QUEUE_MEMORY_FALLBACK_ENABLED must be false for on-prem deployment")


def _validate_qwen_model(errors: list[str]) -> None:
    if not env_bool("ENABLE_LOCAL_QWEN_MODEL", False):
        return

    model_path = Path(_value("QWEN_VL_7B_MODEL_PATH") or "/models/Qwen2.5-VL-7B-Instruct")
    if not model_path.exists():
        errors.append(f"QWEN_VL_7B_MODEL_PATH does not exist: {model_path}")
        return
    if not model_path.is_dir():
        errors.append(f"QWEN_VL_7B_MODEL_PATH is not a directory: {model_path}")
        return

    required_files = ("config.json",)
    for filename in required_files:
        if not (model_path / filename).is_file():
            errors.append(f"Qwen model file is missing: {model_path / filename}")

    has_weights = any(model_path.glob("*.safetensors")) or any(model_path.glob("*.bin"))
    has_weight_index = any(model_path.glob("*.safetensors.index.json"))
    if not has_weights and not has_weight_index:
        errors.append(f"Qwen model weights were not found in: {model_path}")


def validate_onprem_runtime() -> None:
    if not onprem_validation_enabled():
        return

    errors: list[str] = []
    _require_admin_identity(errors)
    _require_secret(errors, "ADMIN_UI_SECRET_KEY", minimum_length=32)
    _require_secret(errors, "APP_SECRET_KEY", minimum_length=32)
    _validate_auth_policy(errors)
    _validate_rabbitmq_secret(errors)
    _validate_queue(errors)
    _validate_qwen_model(errors)

    if errors:
        detail = "\n".join(f"- {error}" for error in errors)
        raise OnPremValidationError(f"On-prem startup validation failed:\n{detail}")
