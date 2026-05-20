from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field


class ConvertResult(BaseModel):
    jobId: str
    status: Literal["PROCESSING", "COMPLETED", "FAILED", "CANCELED"]
    modelId: str
    duplicatePolicy: str
    parallelism: int
    items: List[Dict[str, Any]]


class QueueSubmitResult(BaseModel):
    jobId: str
    status: Literal["QUEUED"]
    modelId: str
    parallelism: int
    requestedExecutionBackend: Literal["auto", "openai", "openrouter", "qwen_gpu"]
    executionBackend: Literal["openai", "openrouter", "qwen_gpu"]
    queueRoute: str
    totalItems: int
    timeoutSeconds: int
    maxRetries: int


class ConvertStartResult(BaseModel):
    jobId: str
    status: Literal["PROCESSING"]
    modelId: str
    duplicatePolicy: str
    parallelism: int
    totalDocuments: int


class DocumentRetryRequest(BaseModel):
    modelId: str = Field(default="m1", min_length=1, max_length=120)
    language: str = Field(default="한국어", min_length=1, max_length=40)
