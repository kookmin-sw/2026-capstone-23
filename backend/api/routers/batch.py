from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from api.common import fail, ok
from api.dependencies import get_config
from api.services.batch_jobs import (
    get_batch_status,
    get_server_spec_summary,
    resume_background_batch,
    start_background_batch,
    stop_background_batch,
)


router = APIRouter(prefix="/batch", tags=["batch"])


class BatchStartRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=500)
    language: str = Field(default="한국어", min_length=1, max_length=40)
    vlmModel: str = Field(default="openrouter/qwen3-vl-32b", min_length=1, max_length=120)
    parallel: int = Field(default=1, ge=1, le=16)
    maxRetries: int = Field(default=1, ge=0, le=3)

    @field_validator("paths")
    @classmethod
    def normalize_paths(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


class BatchResumeRequest(BaseModel):
    language: Optional[str] = Field(default=None, min_length=1, max_length=40)
    vlmModel: Optional[str] = Field(default=None, min_length=1, max_length=120)
    parallel: Optional[int] = Field(default=None, ge=1, le=16)
    maxRetries: Optional[int] = Field(default=None, ge=0, le=3)


@router.get(
    "/status",
    summary="배치 작업 상태 조회",
    description="현재 실행 중인 백그라운드 배치 작업의 상태와 진행 정보를 반환합니다. 배치 처리 전용 관리 화면에서 사용합니다.",
)
def batch_status():
    return ok(get_batch_status(get_config()))


@router.get(
    "/server-spec",
    summary="배치 서버 사양 조회",
    description="배치 작업 실행 서버의 기본 사양 요약을 반환합니다. 병렬도 설정이나 운영 점검에 참고할 수 있습니다.",
)
def batch_server_spec():
    return ok(get_server_spec_summary())


@router.post(
    "/start",
    summary="배치 작업 시작",
    description="여러 입력 경로를 받아 백그라운드 배치 처리를 시작합니다. 대량 파일 처리나 예약 작업의 수동 시작에 사용합니다.",
)
def batch_start(req: BatchStartRequest):
    try:
        result = start_background_batch(
            get_config(),
            paths=req.paths,
            language=req.language,
            vlm_model=req.vlmModel,
            parallel=req.parallel,
            max_retries=req.maxRetries,
        )
        return ok(result)
    except FileNotFoundError as exc:
        fail("NOT_FOUND", f"file not found: {exc}", status=404)
    except PermissionError as exc:
        fail("FORBIDDEN_PATH", f"path is outside input root: {exc}", status=403)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc), status=422)
    except RuntimeError as exc:
        fail("CONFLICT", str(exc), status=409)


@router.post(
    "/resume",
    summary="중단된 배치 작업 재개",
    description="이전 상태를 바탕으로 중단된 배치 작업을 다시 시작합니다. 서버 재기동 후 복구나 운영 재시도 흐름에 사용합니다.",
)
def batch_resume(req: BatchResumeRequest):
    try:
        result = resume_background_batch(
            get_config(),
            language=req.language,
            vlm_model=req.vlmModel,
            parallel=req.parallel,
            max_retries=req.maxRetries,
        )
        return ok(result)
    except FileNotFoundError as exc:
        fail("NOT_FOUND", str(exc), status=404)
    except ValueError as exc:
        fail("CONFLICT", str(exc), status=409)
    except RuntimeError as exc:
        fail("CONFLICT", str(exc), status=409)


@router.post(
    "/stop",
    summary="배치 작업 중지",
    description="현재 실행 중인 백그라운드 배치 작업을 중지 요청합니다. 장시간 작업을 운영자가 수동으로 멈출 때 사용합니다.",
)
def batch_stop():
    try:
        return ok(stop_background_batch(get_config()))
    except FileNotFoundError as exc:
        fail("NOT_FOUND", str(exc), status=404)
