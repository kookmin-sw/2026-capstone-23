from __future__ import annotations

from starlette_admin.fields import IntegerField, StringField

from infra.store import (
    QWEN_FINALIZE_TASKS,
    QWEN_INFER_RESULTS,
    QWEN_INFER_TASKS,
    QWEN_PREPROCESS_TASKS,
    WORKER_LEASES,
)

from .common import JsonStoreAdminView


class WorkerLeasesAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=WORKER_LEASES,
            identity="worker-leases",
            label="Worker Leases",
            icon="fa-solid fa-microchip",
            summary_fields=[
                StringField("workerId", label="Worker ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("queueRoute", label="Queue Route", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("leaseExpiresAt", label="Lease Expires At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("updatedAt", label="Updated At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "workerId", "queueRoute"),
            sortable_fields=("recordKey", "leaseExpiresAt", "updatedAt"),
            default_sort=(("updatedAt", False),),
            allow_delete=True,
        )


class QwenPreprocessTasksAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=QWEN_PREPROCESS_TASKS,
            identity="qwen-preprocess-tasks",
            label="Qwen Preprocess Tasks",
            icon="fa-solid fa-gears",
            summary_fields=[
                StringField("jobItemId", label="Job Item ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("status", label="Status", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("workerId", label="Worker ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("updatedAt", label="Updated At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "jobItemId", "status", "workerId"),
            sortable_fields=("recordKey", "updatedAt"),
            default_sort=(("updatedAt", False),),
            allow_delete=True,
        )


class QwenInferTasksAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=QWEN_INFER_TASKS,
            identity="qwen-infer-tasks",
            label="Qwen Infer Tasks",
            icon="fa-solid fa-server",
            summary_fields=[
                StringField("jobItemId", label="Job Item ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                IntegerField("pageNumber", label="Page", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("status", label="Status", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("workerId", label="Worker ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("updatedAt", label="Updated At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "jobItemId", "status", "workerId"),
            sortable_fields=("recordKey", "pageNumber", "updatedAt"),
            default_sort=(("updatedAt", False),),
            allow_delete=True,
        )


class QwenInferResultsAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=QWEN_INFER_RESULTS,
            identity="qwen-infer-results",
            label="Qwen Infer Results",
            icon="fa-solid fa-square-poll-vertical",
            summary_fields=[
                StringField("jobItemId", label="Job Item ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                IntegerField("pageNumber", label="Page", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("status", label="Status", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("updatedAt", label="Updated At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "jobItemId", "status"),
            sortable_fields=("recordKey", "pageNumber", "updatedAt"),
            default_sort=(("updatedAt", False),),
            allow_delete=True,
        )


class QwenFinalizeTasksAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=QWEN_FINALIZE_TASKS,
            identity="qwen-finalize-tasks",
            label="Qwen Finalize Tasks",
            icon="fa-solid fa-flag-checkered",
            summary_fields=[
                StringField("jobItemId", label="Job Item ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("status", label="Status", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("workerId", label="Worker ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("updatedAt", label="Updated At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "jobItemId", "status", "workerId"),
            sortable_fields=("recordKey", "updatedAt"),
            default_sort=(("updatedAt", False),),
            allow_delete=True,
        )
