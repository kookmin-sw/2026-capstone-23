from __future__ import annotations

from starlette_admin.fields import StringField

from infra.store import ADMIN_SETTINGS, RAG_SESSIONS

from .common import JsonStoreAdminView


class AdminSettingsView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=ADMIN_SETTINGS,
            identity="admin-settings",
            label="Admin Settings",
            icon="fa-solid fa-sliders",
            summary_fields=[
                StringField("updatedAt", label="Updated At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "updatedAt"),
            sortable_fields=("recordKey", "updatedAt"),
            default_sort=(("updatedAt", False),),
            allow_create=True,
            allow_edit=True,
            allow_delete=True,
        )


class RagSessionsAdminView(JsonStoreAdminView):
    def __init__(self) -> None:
        super().__init__(
            store=RAG_SESSIONS,
            identity="rag-sessions",
            label="RAG Sessions",
            icon="fa-solid fa-comments",
            summary_fields=[
                StringField("userId", label="User ID", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("title", label="Title", read_only=True, exclude_from_create=True, exclude_from_edit=True),
                StringField("updatedAt", label="Updated At", read_only=True, exclude_from_create=True, exclude_from_edit=True),
            ],
            searchable_fields=("recordKey", "userId", "title"),
            sortable_fields=("recordKey", "updatedAt"),
            default_sort=(("updatedAt", False),),
            allow_delete=True,
        )
