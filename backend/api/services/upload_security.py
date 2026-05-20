from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from core.config import SUPPORTED_EXTENSIONS
from core.documents.records import normalize_filename
from core.env import env_csv, env_int


DEFAULT_ALLOWED_EXTENSIONS = SUPPORTED_EXTENSIONS | {".tif", ".webp", ".txt"}
TEXT_EXTENSIONS = {".csv", ".txt"}
ZIP_CONTAINER_EXTENSIONS = {".xlsx", ".hwpx"}
OLE_CONTAINER_EXTENSIONS = {".hwp", ".xls"}
MAX_UPLOAD_BYTES_DEFAULT = 100 * 1024 * 1024


class UploadSecurityError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 422,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def max_upload_bytes() -> int:
    return env_int("MAX_UPLOAD_BYTES", MAX_UPLOAD_BYTES_DEFAULT, minimum=1)


def allowed_upload_extensions() -> set[str]:
    configured = env_csv("UPLOAD_ALLOWED_EXTENSIONS", "")
    if not configured:
        return {suffix.lower() for suffix in DEFAULT_ALLOWED_EXTENSIONS}
    return {
        suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
        for suffix in configured
        if suffix.strip()
    }


def _is_zip_with_any(content: bytes, prefixes: tuple[str, ...]) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        return False
    return any(name.startswith(prefix) for name in names for prefix in prefixes)


def _is_text_like(content: bytes) -> bool:
    if b"\x00" in content:
        return False
    sample = content[:4096]
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            sample.decode("cp949")
            return True
        except UnicodeDecodeError:
            return False


def _signature_matches(suffix: str, content: bytes) -> bool:
    if suffix == ".pdf":
        return content.startswith(b"%PDF-")
    if suffix in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".bmp":
        return content.startswith(b"BM")
    if suffix in {".tif", ".tiff"}:
        return content.startswith((b"II*\x00", b"MM\x00*"))
    if suffix == ".webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    if suffix == ".xlsx":
        return _is_zip_with_any(content, ("xl/",))
    if suffix == ".hwpx":
        return _is_zip_with_any(content, ("Contents/", "Preview/"))
    if suffix in OLE_CONTAINER_EXTENSIONS:
        return content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or b"HWP Document File" in content[:128]
    if suffix in TEXT_EXTENSIONS:
        return _is_text_like(content)
    return True


def validate_upload_content(
    *,
    filename: str,
    content: bytes,
    declared_media_type: str | None = None,
) -> dict[str, Any]:
    normalized = normalize_filename(filename)
    suffix = Path(normalized).suffix.lower()
    allowed = allowed_upload_extensions()
    size_limit = max_upload_bytes()

    if not suffix or suffix not in allowed:
        raise UploadSecurityError(
            "UNSUPPORTED_FILE_TYPE",
            f"unsupported file type: {suffix or 'unknown'}",
            status=415,
            details={"filename": normalized, "allowedExtensions": sorted(allowed)},
        )
    if not content:
        raise UploadSecurityError(
            "EMPTY_UPLOAD",
            "uploaded file is empty",
            status=422,
            details={"filename": normalized},
        )
    if len(content) > size_limit:
        raise UploadSecurityError(
            "FILE_TOO_LARGE",
            f"uploaded file exceeds size limit: {len(content)} > {size_limit}",
            status=413,
            details={"filename": normalized, "sizeBytes": len(content), "maxBytes": size_limit},
        )
    if not _signature_matches(suffix, content):
        raise UploadSecurityError(
            "INVALID_FILE_SIGNATURE",
            "file content does not match its extension",
            status=415,
            details={"filename": normalized, "extension": suffix, "declaredMediaType": declared_media_type},
        )

    return {
        "filename": normalized,
        "extension": suffix,
        "sizeBytes": len(content),
        "declaredMediaType": declared_media_type,
    }


async def read_upload_file_secure(upload: UploadFile) -> tuple[str, bytes, dict[str, Any]]:
    filename = normalize_filename(upload.filename or "unnamed")
    content = await upload.read()
    security = validate_upload_content(
        filename=filename,
        content=content,
        declared_media_type=upload.content_type,
    )
    return filename, content, security


def read_upload_file_secure_sync(upload: UploadFile) -> tuple[str, bytes, dict[str, Any]]:
    filename = normalize_filename(upload.filename or "unnamed")
    content = upload.file.read()
    security = validate_upload_content(
        filename=filename,
        content=content,
        declared_media_type=upload.content_type,
    )
    return filename, content, security
