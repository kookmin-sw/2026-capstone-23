from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from api.common import fail, ok
from api.dependencies import get_config
from api.services.managed_files import (
    create_download_artifact,
    delete_managed_paths,
    get_output_preview,
    list_managed_files,
)


router = APIRouter(prefix="/workspace", tags=["workspace"])


class ManagedPathsRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)

    @field_validator("paths")
    @classmethod
    def normalize_paths(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item and item.strip()]
        if not normalized:
            raise ValueError("paths must not be empty")
        return normalized


@router.get(
    "/files",
    summary="작업공간 파일 목록 조회",
    description="입력 또는 출력 작업공간의 파일 목록을 반환합니다. 파일 탐색기 형태의 화면에서 사용합니다.",
)
def list_workspace_files(scope: Literal["input", "output"] = Query(default="output")):
    config = get_config()
    return ok({"scope": scope, "items": list_managed_files(config, scope)})


@router.get(
    "/files/content",
    summary="작업공간 파일 내용 조회",
    description="텍스트 또는 결과 파일의 미리보기 내용을 반환합니다. 결과 상세 패널이나 코드/텍스트 뷰어에 사용합니다.",
)
def get_workspace_file_content(path: str = Query(..., min_length=1)):
    config = get_config()
    try:
        return ok(get_output_preview(config, path))
    except FileNotFoundError:
        fail("NOT_FOUND", f"file not found: {path}", status=404)
    except PermissionError:
        fail("FORBIDDEN_PATH", f"path is outside managed roots: {path}", status=403)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc), status=422, details={"path": path})


@router.get(
    "/files/html-preview",
    summary="작업공간 HTML 미리보기 조회",
    description="출력 파일에서 HTML 섹션만 추출해 반환합니다. HTML 결과 렌더링 패널 전용 API입니다.",
)
def get_workspace_html_preview(path: str = Query(..., min_length=1)):
    config = get_config()
    try:
        preview = get_output_preview(config, path)
        return ok(
            {
                "path": preview["path"],
                "relativePath": preview["relativePath"],
                "hasHtml": preview["hasHtml"],
                "html": preview["htmlSection"],
            }
        )
    except FileNotFoundError:
        fail("NOT_FOUND", f"file not found: {path}", status=404)
    except PermissionError:
        fail("FORBIDDEN_PATH", f"path is outside managed roots: {path}", status=403)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc), status=422, details={"path": path})


@router.post(
    "/files/delete",
    summary="작업공간 파일 일괄 삭제",
    description="입력/출력 작업공간에 있는 여러 파일을 한 번에 삭제합니다. 다중 선택 삭제 UI에 사용합니다.",
)
def delete_workspace_files(req: ManagedPathsRequest):
    config = get_config()
    return ok(delete_managed_paths(config, req.paths))


@router.post(
    "/files/download",
    summary="작업공간 파일 일괄 다운로드",
    description="선택한 여러 파일을 묶어 다운로드 아티팩트로 생성한 뒤 내려줍니다. 다중 파일 다운로드 기능에 사용합니다.",
)
def download_workspace_files(req: ManagedPathsRequest):
    config = get_config()
    try:
        file_path = create_download_artifact(config, req.paths)
    except FileNotFoundError as exc:
        fail("NOT_FOUND", f"file not found: {exc}", status=404)
    except PermissionError as exc:
        fail("FORBIDDEN_PATH", f"path is outside managed roots: {exc}", status=403)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc), status=422)

    return FileResponse(path=file_path, filename=file_path.name)
