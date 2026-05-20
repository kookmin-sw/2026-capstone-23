from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, make_prefixed_id
from db.enums import ModelProvider
from db.models.common import enum_column


class ParserModel(Base):
    __tablename__ = "parser_models"

    model_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: make_prefixed_id("mdl"))
    model_code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[ModelProvider] = mapped_column(enum_column(ModelProvider, "parser_model_provider"), nullable=False)
    default_execution_backend: Mapped[str] = mapped_column(String(80), default="openai", nullable=False)
    supported_execution_backends_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
