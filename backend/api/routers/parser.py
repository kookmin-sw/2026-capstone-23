import hashlib
import importlib
import sys
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile

from api.common import ErrorCode, fail, now_iso, ok
from api.dependencies import config, pipeline
from core.jobs.execution import (
    AUTO_BACKEND,
    queue_route_for_backend,
    resolve_execution_backend,
)
from core.jobs.service import (
    create_job,
    create_job_item,
    set_job_progress_publisher,
)
from api.services.audit_log import record_audit_event
from api.services.document_files import prepare_uploaded_document_assets
from core.documents.records import (
    build_document_record,
    normalize_filename,
    pick_storage_name,
)
from core.documents.storage import (
    cleanup_output_artifacts,
    persist_result_artifact,
    result_filename_for,
)
from api.services.upload_security import (
    UploadSecurityError,
    read_upload_file_secure,
    read_upload_file_secure_sync,
)
from api.schemas.parser import (
    ConvertResult,
    ConvertStartResult,
    QueueSubmitResult,
)
from api.routers.parser_jobs import router as parser_jobs_router
from api.security import current_user_id
from api.services.parser_helpers import (
    build_convert_item as _build_convert_item,
    cache_expiry as _cache_expiry,
    cache_key as _cache_key,
    cache_ttl_seconds as _cache_ttl_seconds,
    coerce_int as _coerce_int,
    coerce_progress_numbers as _coerce_progress_numbers,
    get_cached_document as _get_cached_document,
    percent as _percent,
    publish_sync_convert_progress,
    resolve_item_max_retries as _resolve_item_max_retries,
    resolve_item_timeout_seconds as _resolve_item_timeout_seconds,
    result_url as _result_url,
    validate_execution_backend as _validate_execution_backend,
    validate_execution_backend_for_model as _validate_execution_backend_for_model,
)
from core.model_catalog import find_model_code
from api.ws_hub import publish_job_progress
from infra.storage.file_assets import (
    materialize_record_asset,
)
from infra.storage.settings import get_configured_storage_path
from infra.queue.job_queue import (
    QueueUnavailableError,
    ensure_queue_available,
    queue_status,
)
from infra.store import DOCUMENTS, DOCUMENT_CACHE, JOBS


_PARSER_DOCUMENTS_MODULE = "api.routers.parser_documents"
if _PARSER_DOCUMENTS_MODULE in sys.modules:
    _parser_documents = importlib.reload(sys.modules[_PARSER_DOCUMENTS_MODULE])
else:
    _parser_documents = importlib.import_module(_PARSER_DOCUMENTS_MODULE)

router = APIRouter(prefix="/parser", tags=["parser"])
router.include_router(parser_jobs_router)
router.include_router(_parser_documents.router)
get_document_result = _parser_documents.get_document_result
set_job_progress_publisher(publish_job_progress)


def _publish_sync_convert_progress(*args: Any, **kwargs: Any) -> None:
    publish_sync_convert_progress(*args, **kwargs, publisher=publish_job_progress)


def _fail_queue_unavailable(exc: QueueUnavailableError | None = None) -> None:
    status = queue_status()
    message = str(exc or status.get("reason") or "queue unavailable")
    fail(
        ErrorCode.QUEUE_UNAVAILABLE,
        message,
        status=503,
        details=status,
    )


def _reject_upload(
    request: Request, upload: UploadFile, exc: UploadSecurityError, *, endpoint: str
) -> None:
    record_audit_event(
        action="DOCUMENT_UPLOAD_REJECTED",
        resource_type="upload",
        resource_id=normalize_filename(upload.filename or "unnamed"),
        outcome="DENIED",
        request=request,
        details={"code": exc.code, "endpoint": endpoint, **exc.details},
    )
    fail(exc.code, exc.message, status=exc.status, details=exc.details)


@router.post(
    "/jobs",
    summary="비동기 변환 Job 생성",
    description="파일들을 업로드해 비동기 Job과 Job Item을 생성하고 큐에 적재합니다. 프론트에서 대량 업로드 후 진행률을 조회할 때 기본으로 사용하는 엔드포인트입니다.",
)
def submit_job(
    request: Request,
    files: Annotated[List[UploadFile], File(...)],
    userId: str = Form("u-demo"),
    modelId: str = Form("m1"),
    parallelism: int = Form(1),
    executionBackend: str = Form(AUTO_BACKEND),
    language: str = Form("한국어"),
):
    # draft 단계: 인증 연동 전이라 userId는 인터페이스 호환용 파라미터만 유지
    owner_user_id = current_user_id(request, fallback_user_id=userId)
    if parallelism < 1:
        fail(ErrorCode.VALIDATION_ERROR, "parallelism must be >= 1", status=422)

    model_code = find_model_code(modelId, config.openai_model)
    requested_execution_backend = _validate_execution_backend(executionBackend)
    _validate_execution_backend_for_model(
        requested_backend=requested_execution_backend,
        model_id=modelId,
        model_code=model_code,
    )
    resolved_execution_backend = resolve_execution_backend(
        requested_backend=requested_execution_backend,
        model_id=modelId,
        model_code=model_code,
    )
    queue_route = queue_route_for_backend(
        resolved_execution_backend,
        model_id=modelId,
        model_code=model_code,
    )
    timeout_seconds = _resolve_item_timeout_seconds(
        execution_backend=resolved_execution_backend,
        model_code=model_code,
    )
    max_retries = _resolve_item_max_retries(
        execution_backend=resolved_execution_backend,
        model_code=model_code,
    )
    try:
        ensure_queue_available()
    except QueueUnavailableError as exc:
        _fail_queue_unavailable(exc)

    job_id = create_job(
        model_id=modelId,
        parallelism=parallelism,
        total_items=len(files),
        requested_execution_backend=requested_execution_backend,
        execution_backend=resolved_execution_backend,
        owner_user_id=owner_user_id,
    )

    for idx, upload in enumerate(files, start=1):
        try:
            original_name, file_bytes, upload_security = read_upload_file_secure_sync(
                upload
            )
        except UploadSecurityError as exc:
            _reject_upload(request, upload, exc, endpoint="parser.jobs")
        if not original_name:
            original_name = f"unnamed_{idx}"
        document_id = f"d_{uuid.uuid4().hex[:10]}"
        assets = prepare_uploaded_document_assets(
            document_id=document_id,
            filename=original_name,
            content=file_bytes,
            tmp_root=config.tmp_root,
        )

        try:
            item_id = create_job_item(
                job_id=job_id,
                document_id=document_id,
                file_name=assets["sourceFilename"],
                source_path=assets["sourceFilePath"],
                source_file_id=assets["sourceFileId"],
                source_filename=assets["sourceFilename"],
                language=language,
                execution_backend=resolved_execution_backend,
                queue_route=queue_route,
                model_code=model_code,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
                owner_user_id=owner_user_id,
            )
        except QueueUnavailableError as exc:
            _fail_queue_unavailable(exc)

        DOCUMENTS[document_id] = build_document_record(
            document_id,
            assets["originalFilename"],
            status="QUEUED",
            original_file_path=assets["originalFilePath"],
            original_file_id=assets["originalFileId"],
            original_storage_kind=assets["originalStorageKind"],
            source_filename=assets["sourceFilename"],
            source_file_path=assets["sourceFilePath"],
            source_file_id=assets["sourceFileId"],
            uploaded_at=now_iso(),
            job_id=job_id,
            job_item_id=item_id,
            model_code=model_code,
            execution_backend=resolved_execution_backend,
            owner_user_id=owner_user_id,
        )
        record_audit_event(
            action="DOCUMENT_UPLOADED",
            resource_type="document",
            resource_id=document_id,
            request=request,
            details={
                "filename": original_name,
                "sizeBytes": upload_security["sizeBytes"],
                "endpoint": "parser.jobs",
                "jobId": job_id,
            },
        )

    print(f"[JOB] queued: {job_id}, items={len(files)}")
    result = QueueSubmitResult(
        jobId=job_id,
        status="QUEUED",
        modelId=modelId,
        parallelism=parallelism,
        requestedExecutionBackend=requested_execution_backend,
        executionBackend=resolved_execution_backend,
        queueRoute=queue_route,
        totalItems=len(files),
        timeoutSeconds=timeout_seconds,
        maxRetries=max_retries,
    )
    return ok(result.model_dump())


def _run_started_convert_job(
    *,
    job_id: str,
    prepared_files: List[Dict[str, Any]],
    model_id: str,
    model_code: str,
    duplicate_policy: str,
    language: str,
    owner_user_id: str | None,
) -> None:
    items: List[Dict[str, Any]] = []
    ttl_seconds = _cache_ttl_seconds()
    try:
        pipeline.update_vlm_model(model_code)
        reserved_names = {
            str(doc.get("sourceFilename") or doc.get("originalFilename") or "")
            for doc in DOCUMENTS.values()
        }
        if config.input_root.exists():
            reserved_names.update(
                path.name for path in config.input_root.rglob("*") if path.is_file()
            )

        for idx, prepared in enumerate(prepared_files):
            if JOBS.get(job_id, {}).get("cancelRequested"):
                break

            original_name = normalize_filename(
                str(prepared.get("filename") or "unnamed")
            )
            document_id = f"d_{uuid.uuid4().hex[:10]}"
            _publish_sync_convert_progress(
                job_id=job_id,
                document_id=document_id,
                status="PROCESSING",
                event_type="STARTED",
                message=f"processing started: {original_name}",
                document_percent=0,
            )
            started_at = datetime.now(timezone.utc)
            file_bytes = bytes(prepared.get("content") or b"")
            file_sha256 = hashlib.sha256(file_bytes).hexdigest()
            cache_key = _cache_key(
                file_sha256, model_code, language, owner_user_id=owner_user_id
            )

            if ttl_seconds > 0:
                cached_doc = _get_cached_document(
                    cache_key, owner_user_id=owner_user_id
                )
                if cached_doc:
                    cached_document_id = str(cached_doc["documentId"])
                    items.append(
                        _build_convert_item(
                            document_id=cached_document_id,
                            original_name=str(
                                cached_doc.get("originalFilename")
                                or cached_doc.get("sourceFilename")
                                or original_name
                            ),
                            original_file_path=str(
                                cached_doc.get("originalFilePath") or ""
                            ),
                            output_txt_path=str(cached_doc.get("outputPath") or ""),
                            elapsed_ms=0,
                            status="COMPLETED",
                            error_info=None,
                            cache_hit=True,
                        )
                    )
                    job = JOBS.get(job_id, {})
                    job["completedDocuments"] = (
                        int(job.get("completedDocuments", 0)) + 1
                    )
                    job.setdefault("completedDocumentIds", []).append(
                        cached_document_id
                    )
                    remaining = len(prepared_files) - (idx + 1)
                    job["processingDocuments"] = 1 if remaining > 0 else 0
                    job["pendingDocuments"] = max(
                        0, remaining - job["processingDocuments"]
                    )
                    job["updatedAt"] = now_iso()
                    JOBS[job_id] = job
                    _publish_sync_convert_progress(
                        job_id=job_id,
                        document_id=cached_document_id,
                        status="COMPLETED",
                        event_type="COMPLETED",
                        message=f"cache hit completed: {original_name}",
                        document_percent=100,
                    )
                    continue

            stored_name = pick_storage_name(
                original_name,
                duplicate_policy=duplicate_policy,
                existing_names=reserved_names,
            )
            reserved_names.add(stored_name)
            assets = prepare_uploaded_document_assets(
                document_id=document_id,
                filename=stored_name,
                content=file_bytes,
                tmp_root=config.tmp_root,
            )
            DOCUMENTS[document_id] = build_document_record(
                document_id,
                assets["originalFilename"],
                status="PROCESSING",
                original_file_path=assets["originalFilePath"],
                original_file_id=assets["originalFileId"],
                original_storage_kind=assets["originalStorageKind"],
                source_filename=assets["sourceFilename"],
                source_file_path=assets["sourceFilePath"],
                source_file_id=assets["sourceFileId"],
                uploaded_at=now_iso(),
                job_id=job_id,
                processing_time_ms=None,
                model_code=model_code,
                file_sha256=file_sha256,
                cache_key=cache_key,
                cache_expires_at=_cache_expiry(ttl_seconds),
                meta={"processedPages": 0},
                owner_user_id=owner_user_id,
            )

            def _document_progress_callback(*args: Any) -> None:
                current_page, total_pages = _coerce_progress_numbers(args)
                document_percent = _percent(current_page, total_pages)
                doc = DOCUMENTS.get(document_id)
                if doc is not None:
                    meta = dict(doc.get("meta") or {})
                    meta["processedPages"] = current_page
                    if total_pages > 0:
                        meta["pageCount"] = total_pages
                        meta["totalPages"] = total_pages
                    doc["meta"] = meta
                    doc["latestStatus"] = "PROCESSING"
                    doc["updatedAt"] = now_iso()
                _publish_sync_convert_progress(
                    job_id=job_id,
                    document_id=document_id,
                    status="PROCESSING",
                    event_type="PROGRESS",
                    message=f"processing page {current_page}/{total_pages}: {assets['sourceFilename']}",
                    document_percent=document_percent,
                    current_page=current_page,
                    total_pages=total_pages,
                )

            status = "COMPLETED"
            output_txt_path = ""
            output_file_id: Optional[str] = None
            output_meta: Dict[str, Any] = {}
            error_info = None
            try:
                with materialize_record_asset(
                    {
                        "sourceFileId": assets["sourceFileId"],
                        "sourceFilename": assets["sourceFilename"],
                    },
                    file_id_key="sourceFileId",
                    filename_key="sourceFilename",
                    fallback_path_key="sourceFilePath",
                    tmp_root=config.tmp_root,
                    purpose="async_convert",
                    owner_id=document_id,
                ) as source_path:
                    out = pipeline.process_file(
                        source_path,
                        language=language,
                        progress_callback=_document_progress_callback,
                    )
                    stored_output = persist_result_artifact(
                        document_id=document_id,
                        output_path=out,
                        output_filename=result_filename_for(assets["sourceFilename"]),
                        display_source=assets["sourceFilename"],
                        display_pdf_path=assets["originalFilePath"]
                        if assets["fileType"] == "pdf"
                        else None,
                        storage_root=get_configured_storage_path(config.output_root),
                    )
                    output_txt_path = stored_output["storagePath"]
                    output_file_id = stored_output["fileId"]
                    output_meta = dict(stored_output["meta"])
                    cleanup_output_artifacts(out)
            except Exception as exc:  # noqa: BLE001
                status = "FAILED"
                error_info = {"code": "CONVERSION_FAIL", "message": str(exc)}

            elapsed_ms = int(
                (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
            )
            DOCUMENTS[document_id] = build_document_record(
                document_id,
                assets["originalFilename"],
                status=status,
                original_file_path=assets["originalFilePath"],
                original_file_id=assets["originalFileId"],
                original_storage_kind=assets["originalStorageKind"],
                source_filename=assets["sourceFilename"],
                source_file_path=assets["sourceFilePath"],
                source_file_id=assets["sourceFileId"],
                uploaded_at=now_iso(),
                job_id=job_id,
                processing_time_ms=elapsed_ms,
                model_code=model_code,
                file_sha256=file_sha256,
                cache_key=cache_key,
                cache_expires_at=_cache_expiry(ttl_seconds),
                meta=output_meta,
                output_path=output_txt_path,
                output_file_id=output_file_id,
                output_filename=result_filename_for(assets["sourceFilename"]),
                owner_user_id=owner_user_id,
                error=error_info,
            )

            if status == "COMPLETED" and ttl_seconds > 0 and output_txt_path:
                DOCUMENT_CACHE[cache_key] = {
                    "cacheKey": cache_key,
                    "fileSha256": file_sha256,
                    "documentId": document_id,
                    "modelCode": model_code,
                    "language": language,
                    "ownerUserId": owner_user_id,
                    "outputFileId": output_file_id,
                    "outputPath": output_txt_path,
                    "resultUrl": _result_url(document_id),
                    "createdAt": now_iso(),
                    "expiresAt": _cache_expiry(ttl_seconds),
                }

            item = _build_convert_item(
                document_id=document_id,
                original_name=assets["sourceFilename"],
                original_file_path=assets["originalFilePath"],
                output_txt_path=output_txt_path,
                elapsed_ms=elapsed_ms,
                status=status,
                error_info=error_info,
            )
            items.append(item)

            job = JOBS.get(job_id, {})
            if status == "COMPLETED":
                job["completedDocuments"] = int(job.get("completedDocuments", 0)) + 1
                job.setdefault("completedDocumentIds", []).append(document_id)
            else:
                job["failedDocuments"] = int(job.get("failedDocuments", 0)) + 1
            remaining = len(prepared_files) - (idx + 1)
            job["processingDocuments"] = 1 if remaining > 0 else 0
            job["pendingDocuments"] = max(0, remaining - job["processingDocuments"])
            job["updatedAt"] = now_iso()
            JOBS[job_id] = job
            _publish_sync_convert_progress(
                job_id=job_id,
                document_id=document_id,
                status=status,
                event_type=status,
                message=f"processing {status.lower()}: {assets['sourceFilename']}",
                document_percent=100 if status == "COMPLETED" else 0,
                current_page=_coerce_int(
                    output_meta.get("processedPages") or output_meta.get("pageCount")
                ),
                total_pages=_coerce_int(
                    output_meta.get("totalPages") or output_meta.get("pageCount")
                ),
                error=error_info,
            )

        completed = len([item for item in items if item["status"] == "COMPLETED"])
        failed = len([item for item in items if item["status"] == "FAILED"])
        canceled = len([item for item in items if item["status"] == "CANCELED"])
        if canceled > 0:
            job_status = "CANCELED"
        elif completed == 0 and failed > 0:
            job_status = "FAILED"
        else:
            job_status = "COMPLETED"
        job = JOBS.get(job_id, {})
        job.update(
            {
                "status": job_status,
                "completedDocuments": completed,
                "failedDocuments": failed,
                "canceledDocuments": canceled,
                "processingDocuments": 0,
                "pendingDocuments": 0,
                "items": items,
                "updatedAt": now_iso(),
            }
        )
        JOBS[job_id] = job
        _publish_sync_convert_progress(
            job_id=job_id,
            document_id=None,
            status=job_status,
            event_type=job_status,
            message=f"convert job {job_status.lower()}",
        )
    except Exception as exc:  # noqa: BLE001
        job = JOBS.get(job_id, {})
        job.update(
            {
                "status": "FAILED",
                "failedDocuments": max(1, int(job.get("failedDocuments", 0) or 0)),
                "processingDocuments": 0,
                "pendingDocuments": 0,
                "items": items,
                "updatedAt": now_iso(),
                "error": {"code": "CONVERSION_FAIL", "message": str(exc)},
            }
        )
        JOBS[job_id] = job
        _publish_sync_convert_progress(
            job_id=job_id,
            document_id=None,
            status="FAILED",
            event_type="FAILED",
            message="convert job failed",
            error={"code": "CONVERSION_FAIL", "message": str(exc)},
        )


@router.post(
    "/convert/start",
    summary="비동기 동기형 변환 시작",
    description="`/parser/convert`와 같은 변환 파라미터를 받되 jobId를 즉시 반환하고 실제 변환은 백그라운드에서 실행합니다.",
)
async def start_convert_documents(
    request: Request,
    background_tasks: BackgroundTasks,
    files: Annotated[List[UploadFile], File(...)],
    userId: str = Form("u-demo"),
    modelId: str = Form("m1"),
    duplicatePolicy: Literal["OVERWRITE", "KEEP_BOTH"] = Form("OVERWRITE"),
    parallelism: int = Form(1),
    language: str = Form("한국어"),
):
    owner_user_id = current_user_id(request, fallback_user_id=userId)
    if parallelism < 1:
        fail(ErrorCode.VALIDATION_ERROR, "parallelism must be >= 1", status=422)

    model_code = find_model_code(modelId, config.openai_model)
    job_id = f"j_{uuid.uuid4().hex[:10]}"
    if job_id in JOBS:
        fail(ErrorCode.CONFLICT, f"job already exists: {job_id}", status=409)

    prepared_files = []
    for upload in files:
        try:
            filename, content, security = await read_upload_file_secure(upload)
        except UploadSecurityError as exc:
            _reject_upload(request, upload, exc, endpoint="parser.convert.start")
        prepared_files.append(
            {"filename": filename, "content": content, "security": security}
        )
    now = now_iso()
    JOBS[job_id] = {
        "jobId": job_id,
        "ownerUserId": owner_user_id,
        "status": "PROCESSING",
        "cancelRequested": False,
        "canceledAt": None,
        "modelId": modelId,
        "modelCode": model_code,
        "duplicatePolicy": duplicatePolicy,
        "parallelism": parallelism,
        "totalDocuments": len(prepared_files),
        "completedDocuments": 0,
        "failedDocuments": 0,
        "canceledDocuments": 0,
        "processingDocuments": 1 if prepared_files else 0,
        "pendingDocuments": len(prepared_files) - 1 if prepared_files else 0,
        "completedDocumentIds": [],
        "items": [],
        "createdAt": now,
        "updatedAt": now,
    }
    _publish_sync_convert_progress(
        job_id=job_id,
        document_id=None,
        status="PROCESSING",
        event_type="CREATED",
        message="convert job created",
    )
    for prepared in prepared_files:
        record_audit_event(
            action="DOCUMENT_UPLOAD_ACCEPTED",
            resource_type="upload",
            resource_id=str(prepared["filename"]),
            request=request,
            details={
                "filename": prepared["filename"],
                "sizeBytes": prepared["security"]["sizeBytes"],
                "endpoint": "parser.convert.start",
                "jobId": job_id,
            },
        )
    background_tasks.add_task(
        _run_started_convert_job,
        job_id=job_id,
        prepared_files=prepared_files,
        model_id=modelId,
        model_code=model_code,
        duplicate_policy=duplicatePolicy,
        language=language,
        owner_user_id=owner_user_id,
    )
    result = ConvertStartResult(
        jobId=job_id,
        status="PROCESSING",
        modelId=modelId,
        duplicatePolicy=duplicatePolicy,
        parallelism=parallelism,
        totalDocuments=len(prepared_files),
    )
    return ok(result.model_dump())


@router.post(
    "/convert",
    summary="동기 문서 변환",
    description="파일들을 즉시 순차 처리하고 응답 안에 결과를 함께 반환합니다. 빠른 데모나 내부 검증용이며, 운영에서는 보통 `/parser/jobs` 비동기 API를 우선 사용합니다.",
)
def convert_documents(
    request: Request,
    files: Annotated[List[UploadFile], File(...)],
    userId: str = Form("u-demo"),
    modelId: str = Form("m1"),
    duplicatePolicy: Literal["OVERWRITE", "KEEP_BOTH"] = Form("OVERWRITE"),
    parallelism: int = Form(1),
    language: str = Form("한국어"),
):
    # 인증 연동 전 단계라 userId는 인터페이스 호환용으로만 받는다.
    owner_user_id = current_user_id(request, fallback_user_id=userId)
    if parallelism < 1:
        fail(ErrorCode.VALIDATION_ERROR, "parallelism must be >= 1", status=422)

    # 요청 modelId를 실제 파이프라인 모델 코드로 매핑한다.
    model_code = find_model_code(modelId, config.openai_model)
    pipeline.update_vlm_model(model_code)
    ttl_seconds = _cache_ttl_seconds()

    # 상태 조회가 가능하도록 Job 메타를 먼저 생성한다.
    job_id = f"j_{uuid.uuid4().hex[:10]}"

    if job_id in JOBS:
        fail(ErrorCode.CONFLICT, f"job already exists: {job_id}", status=409)

    print(f"[JOB] created: {job_id}")
    items: List[Dict[str, Any]] = []
    reserved_names = {
        str(doc.get("sourceFilename") or doc.get("originalFilename") or "")
        for doc in DOCUMENTS.values()
    }
    if config.input_root.exists():
        reserved_names.update(
            path.name for path in config.input_root.rglob("*") if path.is_file()
        )
    JOBS[job_id] = {
        "jobId": job_id,
        "ownerUserId": owner_user_id,
        "status": "PROCESSING",
        "cancelRequested": False,
        "canceledAt": None,
        "totalDocuments": len(files),
        "completedDocuments": 0,
        "failedDocuments": 0,
        "canceledDocuments": 0,
        "processingDocuments": 1 if files else 0,
        "pendingDocuments": len(files) - 1 if files else 0,
        "completedDocumentIds": [],
        "updatedAt": now_iso(),
    }
    _publish_sync_convert_progress(
        job_id=job_id,
        document_id=None,
        status="PROCESSING",
        event_type="CREATED",
        message="sync convert job created",
    )

    # 현재는 files를 순차 처리한다. (배치 입력은 지원, 병렬 실행은 미적용)
    for idx, upload in enumerate(files):
        if JOBS.get(job_id, {}).get("cancelRequested"):
            break

        try:
            original_name, file_bytes, upload_security = read_upload_file_secure_sync(
                upload
            )
        except UploadSecurityError as exc:
            _reject_upload(request, upload, exc, endpoint="parser.convert")
        document_id = f"d_{uuid.uuid4().hex[:10]}"
        _publish_sync_convert_progress(
            job_id=job_id,
            document_id=document_id,
            status="PROCESSING",
            event_type="STARTED",
            message=f"processing started: {original_name}",
            document_percent=0,
        )
        started_at = datetime.now(timezone.utc)
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        cache_key = _cache_key(
            file_sha256, model_code, language, owner_user_id=owner_user_id
        )

        if ttl_seconds > 0:
            cached_doc = _get_cached_document(cache_key, owner_user_id=owner_user_id)
            if cached_doc:
                cached_document_id = str(cached_doc["documentId"])
                items.append(
                    _build_convert_item(
                        document_id=cached_document_id,
                        original_name=str(
                            cached_doc.get("originalFilename")
                            or cached_doc.get("sourceFilename")
                            or original_name
                        ),
                        original_file_path=str(
                            cached_doc.get("originalFilePath") or ""
                        ),
                        output_txt_path=str(cached_doc.get("outputPath") or ""),
                        elapsed_ms=0,
                        status="COMPLETED",
                        error_info=None,
                        cache_hit=True,
                    )
                )
                job = JOBS.get(job_id, {})
                job["completedDocuments"] = int(job.get("completedDocuments", 0)) + 1
                job.setdefault("completedDocumentIds", []).append(cached_document_id)
                remaining = len(files) - (idx + 1)
                job["processingDocuments"] = 1 if remaining > 0 else 0
                job["pendingDocuments"] = max(0, remaining - job["processingDocuments"])
                job["updatedAt"] = now_iso()
                JOBS[job_id] = job
                _publish_sync_convert_progress(
                    job_id=job_id,
                    document_id=cached_document_id,
                    status="COMPLETED",
                    event_type="COMPLETED",
                    message=f"cache hit completed: {original_name}",
                    document_percent=100,
                )
                continue

        # 업로드 파일을 input_root에 저장한 뒤 변환 엔진에 전달한다.
        stored_name = pick_storage_name(
            original_name,
            duplicate_policy=duplicatePolicy,
            existing_names=reserved_names,
        )
        reserved_names.add(stored_name)
        assets = prepare_uploaded_document_assets(
            document_id=document_id,
            filename=stored_name,
            content=file_bytes,
            tmp_root=config.tmp_root,
        )
        DOCUMENTS[document_id] = build_document_record(
            document_id,
            assets["originalFilename"],
            status="PROCESSING",
            original_file_path=assets["originalFilePath"],
            original_file_id=assets["originalFileId"],
            original_storage_kind=assets["originalStorageKind"],
            source_filename=assets["sourceFilename"],
            source_file_path=assets["sourceFilePath"],
            source_file_id=assets["sourceFileId"],
            uploaded_at=now_iso(),
            job_id=job_id,
            processing_time_ms=None,
            model_code=model_code,
            file_sha256=file_sha256,
            cache_key=cache_key,
            cache_expires_at=_cache_expiry(ttl_seconds),
            meta={"processedPages": 0},
            owner_user_id=owner_user_id,
        )

        def _document_progress_callback(*args: Any) -> None:
            current_page, total_pages = _coerce_progress_numbers(args)
            document_percent = _percent(current_page, total_pages)
            doc = DOCUMENTS.get(document_id)
            if doc is not None:
                meta = dict(doc.get("meta") or {})
                meta["processedPages"] = current_page
                if total_pages > 0:
                    meta["pageCount"] = total_pages
                    meta["totalPages"] = total_pages
                doc["meta"] = meta
                doc["latestStatus"] = "PROCESSING"
                doc["updatedAt"] = now_iso()
            _publish_sync_convert_progress(
                job_id=job_id,
                document_id=document_id,
                status="PROCESSING",
                event_type="PROGRESS",
                message=f"processing page {current_page}/{total_pages}: {assets['sourceFilename']}",
                document_percent=document_percent,
                current_page=current_page,
                total_pages=total_pages,
            )

        status = "COMPLETED"
        output_txt_path = ""
        output_file_id: Optional[str] = None
        output_meta: Dict[str, Any] = {}
        error_info = None
        # 파일 단위 변환 실행. 실패는 item 단위로 기록한다.
        try:
            with materialize_record_asset(
                {
                    "sourceFileId": assets["sourceFileId"],
                    "sourceFilename": assets["sourceFilename"],
                },
                file_id_key="sourceFileId",
                filename_key="sourceFilename",
                fallback_path_key="sourceFilePath",
                tmp_root=config.tmp_root,
                purpose="sync_convert",
                owner_id=document_id,
            ) as source_path:
                out = pipeline.process_file(
                    source_path,
                    language=language,
                    progress_callback=_document_progress_callback,
                )
                stored_output = persist_result_artifact(
                    document_id=document_id,
                    output_path=out,
                    output_filename=result_filename_for(assets["sourceFilename"]),
                    display_source=assets["sourceFilename"],
                    display_pdf_path=assets["originalFilePath"]
                    if assets["fileType"] == "pdf"
                    else None,
                    storage_root=get_configured_storage_path(config.output_root),
                )
                output_txt_path = stored_output["storagePath"]
                output_file_id = stored_output["fileId"]
                output_meta = dict(stored_output["meta"])
                cleanup_output_artifacts(out)
        except Exception as exc:  # noqa: BLE001
            status = "FAILED"
            error_info = {"code": "CONVERSION_FAIL", "message": str(exc)}

        elapsed_ms = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )
        # 문서별 최신 상태/결과 경로 저장
        DOCUMENTS[document_id] = build_document_record(
            document_id,
            assets["originalFilename"],
            status=status,
            original_file_path=assets["originalFilePath"],
            original_file_id=assets["originalFileId"],
            original_storage_kind=assets["originalStorageKind"],
            source_filename=assets["sourceFilename"],
            source_file_path=assets["sourceFilePath"],
            source_file_id=assets["sourceFileId"],
            uploaded_at=now_iso(),
            job_id=job_id,
            processing_time_ms=elapsed_ms,
            model_code=model_code,
            file_sha256=file_sha256,
            cache_key=cache_key,
            cache_expires_at=_cache_expiry(ttl_seconds),
            meta=output_meta,
            output_path=output_txt_path,
            output_file_id=output_file_id,
            output_filename=result_filename_for(assets["sourceFilename"]),
            owner_user_id=owner_user_id,
            error=error_info,
        )
        record_audit_event(
            action="DOCUMENT_UPLOADED",
            resource_type="document",
            resource_id=document_id,
            request=request,
            details={
                "filename": original_name,
                "sizeBytes": upload_security["sizeBytes"],
                "endpoint": "parser.convert",
                "jobId": job_id,
            },
        )

        if status == "COMPLETED" and ttl_seconds > 0 and output_txt_path:
            DOCUMENT_CACHE[cache_key] = {
                "cacheKey": cache_key,
                "fileSha256": file_sha256,
                "documentId": document_id,
                "modelCode": model_code,
                "language": language,
                "ownerUserId": owner_user_id,
                "outputFileId": output_file_id,
                "outputPath": output_txt_path,
                "resultUrl": _result_url(document_id),
                "createdAt": now_iso(),
                "expiresAt": _cache_expiry(ttl_seconds),
            }

        # API 응답용 item 누적
        items.append(
            _build_convert_item(
                document_id=document_id,
                original_name=assets["sourceFilename"],
                original_file_path=assets["originalFilePath"],
                output_txt_path=output_txt_path,
                elapsed_ms=elapsed_ms,
                status=status,
                error_info=error_info,
            )
        )

        # 집계 카운터(completed/failed/pending) 갱신
        job = JOBS.get(job_id, {})
        if status == "COMPLETED":
            job["completedDocuments"] = int(job.get("completedDocuments", 0)) + 1
            job.setdefault("completedDocumentIds", []).append(document_id)
        else:
            job["failedDocuments"] = int(job.get("failedDocuments", 0)) + 1
        remaining = len(files) - (idx + 1)
        job["processingDocuments"] = 1 if remaining > 0 else 0
        job["pendingDocuments"] = max(0, remaining - job["processingDocuments"])
        job["updatedAt"] = now_iso()
        JOBS[job_id] = job
        _publish_sync_convert_progress(
            job_id=job_id,
            document_id=document_id,
            status=status,
            event_type=status,
            message=f"processing {status.lower()}: {assets['sourceFilename']}",
            document_percent=100 if status == "COMPLETED" else 0,
            current_page=_coerce_int(
                output_meta.get("processedPages") or output_meta.get("pageCount")
            ),
            total_pages=_coerce_int(
                output_meta.get("totalPages") or output_meta.get("pageCount")
            ),
            error=error_info,
        )

    # 취소 요청 시 미처리 파일을 CANCELED로 마킹한다.
    job = JOBS.get(job_id, {})
    processed_count = len(items)
    if job.get("cancelRequested"):
        for upload in files[processed_count:]:
            try:
                original_name, file_bytes, upload_security = (
                    read_upload_file_secure_sync(upload)
                )
            except UploadSecurityError as exc:
                _reject_upload(request, upload, exc, endpoint="parser.convert.cancel")
            document_id = f"d_{uuid.uuid4().hex[:10]}"
            canceled_error = {"code": "JOB_CANCELED", "message": "job canceled by user"}
            assets = prepare_uploaded_document_assets(
                document_id=document_id,
                filename=original_name,
                content=file_bytes,
                tmp_root=config.tmp_root,
            )

            # 취소된 항목도 조회 일관성을 위해 DOCUMENTS에 남긴다.
            DOCUMENTS[document_id] = build_document_record(
                document_id,
                assets["originalFilename"],
                status="CANCELED",
                original_file_path=assets["originalFilePath"],
                original_file_id=assets["originalFileId"],
                original_storage_kind=assets["originalStorageKind"],
                source_filename=assets["sourceFilename"],
                source_file_path=assets["sourceFilePath"],
                source_file_id=assets["sourceFileId"],
                uploaded_at=now_iso(),
                job_id=job_id,
                processing_time_ms=0,
                model_code=model_code,
                owner_user_id=owner_user_id,
                error=canceled_error,
            )
            record_audit_event(
                action="DOCUMENT_UPLOADED",
                resource_type="document",
                resource_id=document_id,
                request=request,
                details={
                    "filename": original_name,
                    "sizeBytes": upload_security["sizeBytes"],
                    "endpoint": "parser.convert.cancel",
                    "jobId": job_id,
                    "status": "CANCELED",
                },
            )

            # API 응답용 item 누적
            items.append(
                {
                    "documentId": document_id,
                    "fileName": assets["sourceFilename"],
                    "originalFilePath": assets["originalFilePath"],
                    "txt": {
                        "artifactId": f"a_txt_{uuid.uuid4().hex[:8]}",
                        "path": "",
                        "preview": "",
                    },
                    "htmlPreview": None,
                    "markdown": None,
                    "imageDescriptions": [],
                    "meta": {
                        "totalPages": None,
                        "processedPages": 0,
                        "processingTimeMs": 0,
                        "completedAt": now_iso(),
                    },
                    "status": "CANCELED",
                    "error": canceled_error,
                }
            )
            job = JOBS.get(job_id, {})
            job["canceledDocuments"] = int(job.get("canceledDocuments", 0)) + 1
            job["updatedAt"] = now_iso()
            JOBS[job_id] = job
            _publish_sync_convert_progress(
                job_id=job_id,
                document_id=document_id,
                status="CANCELED",
                event_type="CANCELED",
                message=f"processing canceled: {assets['sourceFilename']}",
                document_percent=0,
                error=canceled_error,
            )

    completed = len([item for item in items if item["status"] == "COMPLETED"])
    failed = len([item for item in items if item["status"] == "FAILED"])
    canceled = len([item for item in items if item["status"] == "CANCELED"])

    # item 집계 기준으로 최종 Job 상태 결정
    if canceled > 0:
        job_status = "CANCELED"
    elif completed == 0 and failed > 0:
        job_status = "FAILED"
    else:
        job_status = "COMPLETED"

    job.update(
        {
            "status": job_status,
            "completedDocuments": completed,
            "failedDocuments": failed,
            "canceledDocuments": canceled,
            "processingDocuments": 0,
            "pendingDocuments": 0,
            "updatedAt": now_iso(),
        }
    )
    JOBS[job_id] = job
    _publish_sync_convert_progress(
        job_id=job_id,
        document_id=None,
        status=job_status,
        event_type=job_status,
        message=f"sync convert job {job_status.lower()}",
    )

    result = ConvertResult(
        jobId=job_id,
        status=job_status,
        modelId=modelId,
        duplicatePolicy=duplicatePolicy,
        parallelism=parallelism,
        items=items,
    )
    return ok(result.model_dump())
