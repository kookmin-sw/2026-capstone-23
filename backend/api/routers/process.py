from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile

from api.common import fail
from api.dependencies import config, pipeline
from api.services.audit_log import record_audit_event
from api.services.upload_security import UploadSecurityError, read_upload_file_secure


router = APIRouter(tags=["process"])


@router.post(
    "/process/file",
    summary="단일 파일 즉시 변환",
    description="업로드한 파일 1개를 즉시 처리하고 결과 파일 경로를 반환합니다. Job 큐를 거치지 않는 동기 처리 API입니다.",
)
async def process_file_api(request: Request, file: UploadFile = File(...), language: str = Form("한국어")):
    try:
        filename, content, security = await read_upload_file_secure(file)
    except UploadSecurityError as exc:
        record_audit_event(
            action="DOCUMENT_UPLOAD_REJECTED",
            resource_type="upload",
            resource_id=file.filename,
            outcome="DENIED",
            request=request,
            details={"code": exc.code, "endpoint": "process.file", **exc.details},
        )
        fail(exc.code, exc.message, status=exc.status, details=exc.details)

    dest = config.input_root / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    record_audit_event(
        action="DOCUMENT_UPLOADED",
        resource_type="upload",
        resource_id=filename,
        request=request,
        details={"filename": filename, "sizeBytes": security["sizeBytes"], "endpoint": "process.file"},
    )
    out = pipeline.process_file(dest, language=language)
    return {"output": str(out)}


@router.post(
    "/process/dir",
    summary="디렉터리 즉시 변환",
    description="서버 내부 디렉터리 경로를 받아 해당 폴더의 문서를 일괄 처리합니다. 운영용보다는 내부 관리/실험 성격의 API입니다.",
)
async def process_dir_api(path: str = Form(...), language: str = Form("한국어")):
    root = Path(path)
    results = pipeline.process_directory(root, language=language)
    return {"outputs": [str(p) for p in results]}
