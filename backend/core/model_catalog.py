from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

from sqlalchemy import select

from core.model_catalog_defaults import default_model_catalog


MODEL_CATALOG = default_model_catalog()


def _to_catalog_item(model: Any) -> dict[str, Any]:
    return {
        "modelId": model.model_id,
        "code": model.model_code,
        "displayName": model.display_name,
        "provider": str(model.provider.value if hasattr(model.provider, "value") else model.provider),
        "defaultExecutionBackend": model.default_execution_backend,
        "supportedExecutionBackends": list(model.supported_execution_backends_json or []),
        "isActive": bool(model.is_active),
    }


def get_model_catalog(*, active_only: bool = True) -> list[dict[str, Any]]:
    try:
        from db.models.parser import ParserModel
        from db.session import SessionLocal, init_db

        init_db()
        with SessionLocal() as session:
            query = select(ParserModel).order_by(ParserModel.model_id)
            if active_only:
                query = query.where(ParserModel.is_active.is_(True))
            rows = session.scalars(query).all()
            if rows:
                return [_to_catalog_item(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        print(f"[model-catalog] database catalog unavailable; using defaults: {exc}")

    return default_model_catalog()


def find_model_code(model_id: Optional[str], fallback: str) -> str:
    if not model_id:
        return fallback
    for model in get_model_catalog():
        if model["modelId"] == model_id:
            return str(model["code"])
    return fallback


def refresh_model_catalog_cache() -> list[dict[str, Any]]:
    global MODEL_CATALOG
    MODEL_CATALOG = get_model_catalog()
    return deepcopy(MODEL_CATALOG)
