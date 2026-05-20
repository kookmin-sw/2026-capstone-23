from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from core.document_crypto import ENCRYPTION_METADATA_KEY, decrypt_content, encrypt_content
from core.database import resolve_database_url, resolve_store_sqlite_path
from db.models import StoredFile
from db.session import SessionLocal, init_db


_DB_LOCK = RLock()


def _resolve_store_path() -> Path:
    return resolve_store_sqlite_path()


def describe_file_store_target() -> str:
    database_url = resolve_database_url()
    if not database_url.startswith("sqlite"):
        return database_url
    return str(_resolve_store_path())


def _to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value or "")


def save_file_blob(
    *,
    file_id: str,
    category: str,
    filename: str,
    media_type: str,
    content: bytes,
    sha256: str,
    size_bytes: int | None = None,
    document_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    stored_metadata = dict(metadata or {})
    associated_data = f"stored-file:{file_id}:{category}".encode("utf-8")
    stored_content, encryption_metadata = encrypt_content(content, associated_data=associated_data)
    if encryption_metadata:
        stored_metadata[ENCRYPTION_METADATA_KEY] = encryption_metadata
    payload = json.dumps(stored_metadata, ensure_ascii=False, separators=(",", ":"))
    resolved_size_bytes = len(content) if size_bytes is None else int(size_bytes)
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            row = session.get(StoredFile, file_id)
            if row is None:
                session.add(
                    StoredFile(
                        file_id=file_id,
                        category=category,
                        document_id=document_id,
                        filename=filename,
                        media_type=media_type,
                        size_bytes=resolved_size_bytes,
                        sha256=sha256,
                        metadata_json=payload,
                        payload=stored_content,
                    )
                )
            else:
                row.category = category
                row.document_id = document_id
                row.filename = filename
                row.media_type = media_type
                row.size_bytes = resolved_size_bytes
                row.sha256 = sha256
                row.metadata_json = payload
                row.payload = stored_content
            session.commit()


def load_file_blob(file_id: str) -> dict[str, Any] | None:
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            row = session.get(StoredFile, file_id)
            if row is None:
                return None
            metadata = json.loads(row.metadata_json or "{}")
            associated_data = f"stored-file:{row.file_id}:{row.category}".encode("utf-8")
            content = decrypt_content(bytes(row.payload), metadata, associated_data=associated_data)
            public_metadata = dict(metadata)
            public_metadata.pop(ENCRYPTION_METADATA_KEY, None)
            return {
                "fileId": row.file_id,
                "category": row.category,
                "documentId": row.document_id,
                "filename": row.filename,
                "mediaType": row.media_type,
                "sizeBytes": int(row.size_bytes),
                "sha256": row.sha256,
                "metadata": public_metadata,
                "encryption": metadata.get(ENCRYPTION_METADATA_KEY),
                "content": content,
                "createdAt": _to_iso(row.created_at),
                "updatedAt": _to_iso(row.updated_at),
            }


def delete_file_blob(file_id: str) -> bool:
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            row = session.get(StoredFile, file_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True


def clear_file_blobs() -> None:
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            session.query(StoredFile).delete()
            session.commit()
