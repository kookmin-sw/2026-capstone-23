from __future__ import annotations

import hashlib
import mimetypes
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from core.documents.records import normalize_filename
from storage.sqlite_files import delete_file_blob, load_file_blob, save_file_blob


DB_STORAGE_ROOT = "db/stored_files"


def build_db_storage_path(file_id: str, filename: str) -> str:
    return f"{DB_STORAGE_ROOT}/{file_id}/{normalize_filename(filename)}"


def guess_media_type(filename: str, *, default: str = "application/octet-stream") -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".txt":
        return "text/plain; charset=utf-8"
    if suffix == ".json":
        return "application/json; charset=utf-8"
    return mimetypes.guess_type(filename)[0] or default


def store_binary_asset(
    *,
    category: str,
    filename: str,
    content: bytes,
    document_id: str | None = None,
    file_id: str | None = None,
    media_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_filename = normalize_filename(filename)
    resolved_file_id = file_id or f"sf_{uuid.uuid4().hex[:12]}"
    resolved_media_type = media_type or guess_media_type(normalized_filename)
    sha256 = hashlib.sha256(content).hexdigest()
    save_file_blob(
        file_id=resolved_file_id,
        category=category,
        filename=normalized_filename,
        media_type=resolved_media_type,
        content=content,
        sha256=sha256,
        document_id=document_id,
        metadata=metadata,
    )
    return {
        "fileId": resolved_file_id,
        "filename": normalized_filename,
        "fileType": Path(normalized_filename).suffix.lower().lstrip(".") or "unknown",
        "mediaType": resolved_media_type,
        "sizeBytes": len(content),
        "sha256": sha256,
        "storagePath": build_db_storage_path(resolved_file_id, normalized_filename),
        "metadata": metadata or {},
    }


def get_binary_asset(file_id: str) -> dict[str, Any] | None:
    asset = load_file_blob(file_id)
    if asset is None:
        return None
    asset["storagePath"] = build_db_storage_path(asset["fileId"], asset["filename"])
    return asset


def read_binary_asset(file_id: str) -> dict[str, Any]:
    asset = get_binary_asset(file_id)
    if asset is None:
        raise FileNotFoundError(f"stored asset is missing: {file_id}")
    return asset


def delete_binary_asset(file_id: str) -> bool:
    return delete_file_blob(file_id)


def _write_temp_file(*, filename: str, content: bytes, tmp_root: Path, purpose: str, owner_id: str) -> Path:
    temp_dir = tmp_root / "document_storage" / purpose / owner_id / uuid.uuid4().hex[:8]
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / normalize_filename(filename)
    temp_path.write_bytes(content)
    return temp_path


@contextmanager
def materialize_binary_asset(
    *,
    file_id: str,
    filename: str,
    tmp_root: Path,
    purpose: str,
    owner_id: str,
) -> Iterator[Path]:
    asset = read_binary_asset(file_id)
    temp_path = _write_temp_file(
        filename=filename,
        content=bytes(asset["content"]),
        tmp_root=tmp_root,
        purpose=purpose,
        owner_id=owner_id,
    )
    try:
        yield temp_path
    finally:
        shutil.rmtree(temp_path.parent, ignore_errors=True)


@contextmanager
def materialize_record_asset(
    record: dict[str, Any],
    *,
    file_id_key: str,
    filename_key: str,
    fallback_path_key: str,
    tmp_root: Path,
    purpose: str,
    owner_id: str,
) -> Iterator[Path]:
    file_id = str(record.get(file_id_key) or "")
    if file_id:
        filename = str(record.get(filename_key) or record.get("originalFilename") or file_id)
        with materialize_binary_asset(
            file_id=file_id,
            filename=filename,
            tmp_root=tmp_root,
            purpose=purpose,
            owner_id=owner_id,
        ) as temp_path:
            yield temp_path
        return

    if file_id_key != "originalFileId":
        fallback_file_id = str(record.get("originalFileId") or "")
        if fallback_file_id:
            filename = str(record.get(filename_key) or record.get("originalFilename") or fallback_file_id)
            with materialize_binary_asset(
                file_id=fallback_file_id,
                filename=filename,
                tmp_root=tmp_root,
                purpose=purpose,
                owner_id=owner_id,
            ) as temp_path:
                yield temp_path
            return

    fallback_value = str(record.get(fallback_path_key) or record.get("originalFilePath") or "")
    fallback_path = Path(fallback_value)
    if not fallback_path.exists() or not fallback_path.is_file():
        raise FileNotFoundError(f"fallback file is missing: {fallback_path}")
    yield fallback_path


def load_record_asset(record: dict[str, Any], *, file_id_key: str, filename_key: str) -> dict[str, Any] | None:
    file_id = str(record.get(file_id_key) or "")
    if not file_id:
        return None
    filename = str(record.get(filename_key) or file_id)
    asset = get_binary_asset(file_id)
    if asset is None:
        return None
    asset["filename"] = filename
    asset["storagePath"] = build_db_storage_path(file_id, filename)
    return asset


def load_output_text(record: dict[str, Any]) -> str:
    output_path_value = str(record.get("outputPath") or "").strip()
    if output_path_value:
        output_path = Path(output_path_value)
        if output_path.exists() and output_path.is_file():
            return output_path.read_text(encoding="utf-8", errors="ignore")

    output_file_id = str(record.get("outputFileId") or "")
    if output_file_id:
        asset = read_binary_asset(output_file_id)
        return bytes(asset["content"]).decode("utf-8", errors="ignore")

    return ""
