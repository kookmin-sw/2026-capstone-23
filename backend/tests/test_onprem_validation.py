from __future__ import annotations

import pytest

from core.onprem_validation import OnPremValidationError, validate_onprem_runtime


def _make_model_dir(tmp_path):
    model_dir = tmp_path / "Qwen2.5-VL-7B-Instruct"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model-00001-of-00001.safetensors").write_bytes(b"weights")
    return model_dir


def _set_valid_onprem_env(monkeypatch, model_dir) -> None:
    monkeypatch.setenv("ONPREM_DEPLOYMENT", "1")
    monkeypatch.setenv("ONPREM_STARTUP_VALIDATION", "1")
    monkeypatch.setenv("ADMIN_ID", "luminir-admin")
    monkeypatch.setenv("ADMIN_PW", "secure-admin-password")
    monkeypatch.setenv("ADMIN_UI_SECRET_KEY", "a" * 32)
    monkeypatch.setenv("APP_SECRET_KEY", "b" * 32)
    monkeypatch.setenv("AUTH_DISABLED", "0")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("QUEUE_BACKEND", "rabbitmq")
    monkeypatch.setenv("QUEUE_MEMORY_FALLBACK_ENABLED", "0")
    monkeypatch.setenv("RABBITMQ_PASSWORD", "rabbitmq-password-32")
    monkeypatch.setenv("ENABLE_LOCAL_QWEN_MODEL", "1")
    monkeypatch.setenv("QWEN_VL_7B_MODEL_PATH", str(model_dir))


def test_onprem_validation_is_disabled_unless_onprem_mode(monkeypatch) -> None:
    monkeypatch.setenv("ONPREM_DEPLOYMENT", "0")
    monkeypatch.setenv("ADMIN_ID", "change-me-before-use")
    monkeypatch.setenv("ADMIN_PW", "change-me-before-use")

    validate_onprem_runtime()


def test_onprem_validation_accepts_secure_runtime_env(tmp_path, monkeypatch) -> None:
    model_dir = _make_model_dir(tmp_path)
    _set_valid_onprem_env(monkeypatch, model_dir)

    validate_onprem_runtime()


def test_onprem_validation_rejects_placeholders_and_unsafe_auth(tmp_path, monkeypatch) -> None:
    model_dir = _make_model_dir(tmp_path)
    _set_valid_onprem_env(monkeypatch, model_dir)
    monkeypatch.setenv("ADMIN_ID", "change-me-before-use")
    monkeypatch.setenv("ADMIN_PW", "change-me-before-use")
    monkeypatch.setenv("ADMIN_UI_SECRET_KEY", "change-me-admin-ui-secret")
    monkeypatch.setenv("APP_SECRET_KEY", "short")
    monkeypatch.setenv("AUTH_DISABLED", "1")
    monkeypatch.setenv("RABBITMQ_PASSWORD", "luminir-local-password")

    with pytest.raises(OnPremValidationError) as exc_info:
        validate_onprem_runtime()

    message = str(exc_info.value)
    assert "ADMIN_ID" in message
    assert "ADMIN_PW" in message
    assert "ADMIN_UI_SECRET_KEY" in message
    assert "APP_SECRET_KEY" in message
    assert "AUTH_DISABLED" in message
    assert "RABBITMQ_PASSWORD" in message
