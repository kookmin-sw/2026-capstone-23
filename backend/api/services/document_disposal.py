from __future__ import annotations

from pathlib import Path
from typing import Any

from infra.storage.file_assets import delete_binary_asset
from infra.store import DOCUMENT_CACHE, DOCUMENTS


def _delete_legacy_path(path_value: str, *, delete_meta: bool = False) -> bool:
    if not path_value:
        return False
    file_path = Path(path_value)
    if file_path.exists() and not file_path.is_file():
        return False

    existed = file_path.exists()
    if existed:
        file_path.unlink()

    if delete_meta:
        meta_path = file_path.with_suffix(".meta.json")
        if meta_path.exists() and meta_path.is_file():
            meta_path.unlink()

    parent_dir = file_path.parent
    if parent_dir.exists() and parent_dir.is_dir():
        try:
            next(parent_dir.iterdir())
        except StopIteration:
            parent_dir.rmdir()
    return existed


def purge_document_cache(document_id: str) -> int:
    removed = 0
    for cache_key, entry in list(DOCUMENT_CACHE.items()):
        if str(entry.get("documentId") or "") != document_id:
            continue
        DOCUMENT_CACHE.pop(cache_key, None)
        removed += 1
    return removed


def dispose_document_record(document_id: str, item: dict[str, Any]) -> dict[str, Any]:
    deleted_any = False
    deleted_file_ids: list[str] = []

    file_ids = {
        str(item.get("sourceFileId") or ""),
        str(item.get("originalFileId") or ""),
        str(item.get("outputFileId") or ""),
    }
    for file_id in {file_id for file_id in file_ids if file_id}:
        if delete_binary_asset(file_id):
            deleted_any = True
            deleted_file_ids.append(file_id)

    deleted_any = _delete_legacy_path(str(item.get("originalFilePath") or "")) or deleted_any
    deleted_any = _delete_legacy_path(str(item.get("sourceFilePath") or "")) or deleted_any
    deleted_any = _delete_legacy_path(str(item.get("outputPath") or ""), delete_meta=True) or deleted_any
    purged_cache_entries = purge_document_cache(document_id)

    DOCUMENTS.pop(document_id, None)
    return {
        "documentId": document_id,
        "fileExisted": deleted_any,
        "deletedFileIds": deleted_file_ids,
        "purgedCacheEntries": purged_cache_entries,
    }
