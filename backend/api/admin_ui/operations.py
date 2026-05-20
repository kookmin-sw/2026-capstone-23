from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette_admin.actions import link_row_action, row_action
from starlette_admin.exceptions import ActionFailed
from starlette_admin.fields import BooleanField, IntegerField, StringField

from core.jobs.service import request_cancel
from infra.store import DOCUMENTS, DOCUMENT_CACHE, JOBS, JOB_EVENTS, JOB_ITEMS

from .common import ADMIN_API_PREFIX, FINAL_JOB_STATES, JsonStoreAdminView


class DocumentsAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=DOCUMENTS,
            identity="documents",
            label="Documents",
            icon="fa-solid fa-file-lines",
            summary_fields=[
                StringField("latestStatus", label="Status", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("originalFilename", label="Original Filename", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("sourceFilename", label="Source Filename", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("fileType", label="File Type", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("jobId", label="Job ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("jobItemId", label="Job Item ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("updatedAt", label="Updated At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "latestStatus", "originalFilename", "sourceFilename", "jobId", "jobItemId"),
            sortable_fields=("recordKey", "latestStatus", "updatedAt"),
            default_sort=(("updatedAt", False),),
        )

    @link_row_action(name="preview", text="Preview", icon_class="fa-solid fa-up-right-from-square")
    def preview_row_action(self, request: Request, pk: Any) -> str:
        return f"{ADMIN_API_PREFIX}/documents/{pk}/original"

    @link_row_action(name="download", text="Download", icon_class="fa-solid fa-download")
    def download_row_action(self, request: Request, pk: Any) -> str:
        return f"{ADMIN_API_PREFIX}/documents/{pk}/download"

    @link_row_action(name="result", text="Result", icon_class="fa-solid fa-file-code")
    def result_row_action(self, request: Request, pk: Any) -> str:
        return f"{ADMIN_API_PREFIX}/parser/documents/{pk}/result"


class JobsAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=JOBS,
            identity="jobs",
            label="Jobs",
            icon="fa-solid fa-list-check",
            summary_fields=[
                StringField("status", label="Status", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("executionBackend", label="Execution Backend", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                IntegerField("totalItems", label="Total Items", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                IntegerField("queuedItems", label="Queued", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                IntegerField("processingItems", label="Processing", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                IntegerField("completedItems", label="Completed", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                IntegerField("failedItems", label="Failed", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                IntegerField("canceledItems", label="Canceled", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                BooleanField("cancelRequested", label="Cancel Requested", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("updatedAt", label="Updated At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "status", "executionBackend"),
            sortable_fields=("recordKey", "status", "updatedAt"),
            default_sort=(("updatedAt", False),),
        )

    @row_action(
        name="cancel",
        text="Cancel",
        confirmation="Cancel this job and mark queued items as canceled?",
        icon_class="fa-solid fa-ban",
        submit_btn_class="btn-warning",
        action_btn_class="btn-warning",
    )
    async def cancel_row_action(self, request: Request, pk: Any) -> str:
        key = str(pk)
        job = JOBS.get(key)
        if not job:
            raise ActionFailed(f"Missing job: {key}")
        if str(job.get("status") or "") in FINAL_JOB_STATES:
            raise ActionFailed(f"Job is already finalized: {job.get('status')}")
        canceled_items = request_cancel(key)
        return f"Cancel requested. Applied to {canceled_items} queued items."

    async def is_row_action_allowed(self, request: Request, name: str) -> bool:
        if name == "cancel":
            job = JOBS.get(str(request.path_params.get("pk") or ""))
            return bool(job) and str(job.get("status") or "") not in FINAL_JOB_STATES
        return await super().is_row_action_allowed(request, name)


class JobItemsAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=JOB_ITEMS,
            identity="job-items",
            label="Job Items",
            icon="fa-solid fa-bars-progress",
            summary_fields=[
                StringField("status", label="Status", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("stage", label="Stage", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("jobId", label="Job ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("documentId", label="Document ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("queueRoute", label="Queue Route", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                IntegerField("retryCount", label="Retry Count", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("workerId", label="Worker ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("updatedAt", label="Updated At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "status", "stage", "jobId", "documentId", "workerId"),
            sortable_fields=("recordKey", "status", "stage", "updatedAt"),
            default_sort=(("updatedAt", False),),
        )


class JobEventsAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=JOB_EVENTS,
            identity="job-events",
            label="Job Events",
            icon="fa-solid fa-clock-rotate-left",
            summary_fields=[
                StringField("jobId", label="Job ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("jobItemId", label="Job Item ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("eventType", label="Event Type", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("status", label="Status", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("createdAt", label="Created At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "jobId", "jobItemId", "eventType", "status"),
            sortable_fields=("recordKey", "createdAt"),
            default_sort=(("createdAt", False),),
        )


class DocumentCacheAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=DOCUMENT_CACHE,
            identity="document-cache",
            label="Document Cache",
            icon="fa-solid fa-database",
            summary_fields=[
                StringField("documentId", label="Document ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("modelCode", label="Model Code", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("language", label="Language", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("expiresAt", label="Expires At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "documentId", "modelCode", "language"),
            sortable_fields=("recordKey", "expiresAt"),
            default_sort=(("expiresAt", False),),
            allow_delete=True,
        )
