from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from core.time import now_iso as _now_iso


class ErrorCode:
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONFLICT = "CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"


class ApiError(BaseModel):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


class ApiResponse(BaseModel):
    success: bool
    data: Optional[Any] = None
    error: Optional[ApiError] = None


def ok(data: Any = None) -> Dict[str, Any]:
    return ApiResponse(success=True, data=data, error=None).model_dump()


def error_payload(
    code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return ApiResponse(
        success=False,
        data=None,
        error=ApiError(code=code, message=message, details=details),
    ).model_dump()


def fail(code: str, message: str, status: int = 400, details: Optional[Dict[str, Any]] = None):
    payload = error_payload(code=code, message=message, details=details)
    raise HTTPException(status_code=status, detail=payload)


def fail_not_found(resource: str, resource_id: str):
    fail(
        code=ErrorCode.NOT_FOUND,
        message=f"{resource} not found: {resource_id}",
        status=404,
        details={"resource": resource, "id": resource_id},
    )


def now_iso() -> str:
    return _now_iso()
