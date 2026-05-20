from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette_admin import BaseAdmin, DropDown

from .accounts import AuthSessionsAdminView, UsersAdminView
from .auth import AdminUIAuthProvider, admin_secret_key
from .dashboard import AdminDashboardView
from .data_views import AdminSettingsView, RagSessionsAdminView
from .operations import DocumentCacheAdminView, DocumentsAdminView, JobEventsAdminView, JobItemsAdminView, JobsAdminView
from .runtime import (
    QwenFinalizeTasksAdminView,
    QwenInferResultsAdminView,
    QwenInferTasksAdminView,
    QwenPreprocessTasksAdminView,
    WorkerLeasesAdminView,
)


def build_admin_ui(*, base_url: str = "/admin") -> BaseAdmin:
    admin = BaseAdmin(
        title="Luminir Admin",
        base_url=base_url,
        route_name="admin_ui",
        index_view=AdminDashboardView(),
        auth_provider=AdminUIAuthProvider(),
        middlewares=[
            Middleware(
                SessionMiddleware,
                secret_key=admin_secret_key(),
                same_site="lax",
                max_age=60 * 60 * 24 * 30,
            )
        ],
        templates_dir=str(Path(__file__).resolve().parent / "templates"),
    )

    admin.add_view(
        DropDown(
            "Accounts",
            icon="fa-solid fa-users",
            views=[UsersAdminView(), AuthSessionsAdminView()],
        )
    )
    admin.add_view(
        DropDown(
            "Operations",
            icon="fa-solid fa-list-check",
            views=[DocumentsAdminView(), JobsAdminView(), JobItemsAdminView(), JobEventsAdminView(), DocumentCacheAdminView()],
        )
    )
    admin.add_view(
        DropDown(
            "Runtime",
            icon="fa-solid fa-microchip",
            views=[
                WorkerLeasesAdminView(),
                QwenPreprocessTasksAdminView(),
                QwenInferTasksAdminView(),
                QwenInferResultsAdminView(),
                QwenFinalizeTasksAdminView(),
            ],
        )
    )
    admin.add_view(
        DropDown(
            "Data",
            icon="fa-solid fa-database",
            views=[RagSessionsAdminView(), AdminSettingsView()],
        )
    )
    return admin


def mount_admin_ui(app: Starlette, *, base_url: str = "/admin") -> None:
    if getattr(app.state, "admin_ui_mounted", False):
        return
    admin = build_admin_ui(base_url=base_url)
    admin.mount_to(app)
    app.state.admin_ui = admin
    app.state.admin_ui_mounted = True
