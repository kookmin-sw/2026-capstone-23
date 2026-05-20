from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator

from api.common import fail, ok
from api.dependencies import get_config
from api.services.audit_log import list_audit_events
from infra.storage.settings import build_storage_payload, save_storage_path


router = APIRouter(prefix="/admin", tags=["admin"])


class StorageUpdateRequest(BaseModel):
    storagePath: str = Field(min_length=1, max_length=2048)

    @field_validator("storagePath")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("storagePath must not be empty")
        return normalized


@router.get(
    "/storage",
    summary="스토리지 설정 조회",
    description="관리자 설정 화면에서 사용할 저장 경로와 현재 디스크 사용량을 반환합니다.",
)
def get_storage_settings():
    return ok(build_storage_payload(get_config()))


@router.put(
    "/storage",
    summary="스토리지 경로 설정 저장",
    description="관리자 설정 화면에서 입력한 저장 경로를 저장합니다. 기존 런타임 입출력 루트는 즉시 변경하지 않습니다.",
)
def update_storage_settings(req: StorageUpdateRequest):
    storage_path = Path(req.storagePath).expanduser()
    if not storage_path.is_absolute():
        fail("VALIDATION_ERROR", "storagePath must be absolute", status=422)

    save_storage_path(storage_path)
    return ok(build_storage_payload(get_config()))


@router.get(
    "/audit-logs",
    summary="감사 로그 조회",
    description="관리자 전용 감사 로그를 최신순으로 조회합니다. 업로드, 조회, 다운로드, 폐기 이벤트 추적에 사용합니다.",
)
def get_audit_logs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None, max_length=120),
    resourceType: str | None = Query(default=None, max_length=80),
    resourceId: str | None = Query(default=None, max_length=160),
    outcome: str | None = Query(default=None, max_length=40),
    actorUserId: str | None = Query(default=None, max_length=120),
):
    return ok(
        list_audit_events(
            limit=limit,
            offset=offset,
            action=action,
            resource_type=resourceType,
            resource_id=resourceId,
            outcome=outcome,
            actor_user_id=actorUserId,
        )
    )
