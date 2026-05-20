from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from core.time import now_iso
from infra.store import ADMIN_SETTINGS


STORAGE_SETTINGS_KEY = "storage"


def disk_usage(path: Path) -> dict[str, Any]:
    target = path.expanduser()
    while not target.exists() and target != target.parent:
        target = target.parent

    total, used, free = shutil.disk_usage(target)
    return {
        "path": str(path),
        "exists": path.exists(),
        "totalBytes": total,
        "usedBytes": used,
        "freeBytes": free,
        "usagePercent": round((used / total) * 100, 2) if total else 0,
    }


def get_configured_storage_path(output_root: Path) -> Path:
    saved = ADMIN_SETTINGS.get(STORAGE_SETTINGS_KEY, {})
    return Path(str(saved.get("storagePath") or output_root)).expanduser()


def build_storage_payload(config: Any) -> dict[str, Any]:
    storage_path = get_configured_storage_path(config.output_root)
    return {
        "storagePath": str(storage_path),
        "inputRoot": str(config.input_root),
        "outputRoot": str(config.output_root),
        "tmpRoot": str(config.tmp_root),
        "updatedAt": ADMIN_SETTINGS.get(STORAGE_SETTINGS_KEY, {}).get("updatedAt"),
        "usage": disk_usage(storage_path),
    }


def save_storage_path(storage_path: Path) -> None:
    ADMIN_SETTINGS[STORAGE_SETTINGS_KEY] = {
        "storagePath": str(storage_path),
        "updatedAt": now_iso(),
    }
