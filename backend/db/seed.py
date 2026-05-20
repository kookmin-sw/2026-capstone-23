from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.model_catalog_defaults import MANAGED_MODEL_CODES, default_model_catalog
from db.enums import ModelProvider
from db.models.parser import ParserModel


def _provider(value: str) -> ModelProvider:
    normalized = value.strip().lower()
    try:
        return ModelProvider(normalized)
    except ValueError:
        return ModelProvider.OPENAI


def seed_reference_data(session: Session) -> None:
    existing_models = session.scalars(select(ParserModel)).all()
    existing_by_code = {model.model_code: model for model in existing_models}
    existing_by_id = {model.model_id: model for model in existing_models}
    catalog = default_model_catalog()
    active_codes = {str(model["code"]) for model in catalog}

    for model in catalog:
        config_json = {
            "frontendModelId": model["modelId"],
            "code": model["code"],
            "defaultExecutionBackend": model["defaultExecutionBackend"],
            "supportedExecutionBackends": model["supportedExecutionBackends"],
            "managedBy": "default_model_catalog",
        }
        existing = existing_by_code.get(model["code"]) or existing_by_id.get(model["modelId"])
        if existing is None:
            session.add(
                ParserModel(
                    model_id=model["modelId"],
                    model_code=model["code"],
                    display_name=model["displayName"],
                    provider=_provider(model["provider"]),
                    default_execution_backend=model["defaultExecutionBackend"],
                    supported_execution_backends_json=list(
                        model["supportedExecutionBackends"]
                    ),
                    is_active=bool(model["isActive"]),
                    config_json=config_json,
                )
            )
            continue

        existing.model_code = model["code"]
        existing.display_name = model["displayName"]
        existing.provider = _provider(model["provider"])
        existing.default_execution_backend = model["defaultExecutionBackend"]
        existing.supported_execution_backends_json = list(
            model["supportedExecutionBackends"]
        )
        existing.is_active = bool(model["isActive"])
        existing.config_json = {**(existing.config_json or {}), **config_json}

    for model in existing_by_code.values():
        if (
            model.model_code in MANAGED_MODEL_CODES
            and model.model_code not in active_codes
        ):
            model.is_active = False

    session.commit()
