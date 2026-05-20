from __future__ import annotations

from enum import Enum


class ModelProvider(str, Enum):
    OPENAI = "openai"
    LOCAL = "local"
    OPENROUTER = "openrouter"
