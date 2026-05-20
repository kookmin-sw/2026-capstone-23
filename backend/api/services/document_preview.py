from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Literal


PreviewKind = Literal["pdf", "image", "text", "html", "unsupported"]

_CONVERTIBLE_TO_PDF_SUFFIXES = {
    ".hwp",
    ".hwpx",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
}
_INLINE_PREVIEW_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif"}
_INLINE_PREVIEW_HTML_SUFFIXES = {".html", ".htm"}
_INLINE_PREVIEW_TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".csv",
    ".log",
}


def detect_preview_kind(filename_or_path: str | Path) -> PreviewKind:
    suffix = Path(str(filename_or_path)).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in _INLINE_PREVIEW_IMAGE_SUFFIXES:
        return "image"
    if suffix in _INLINE_PREVIEW_HTML_SUFFIXES:
        return "html"
    if suffix in _INLINE_PREVIEW_TEXT_SUFFIXES:
        return "text"
    return "unsupported"


def is_inline_preview_supported(filename_or_path: str | Path) -> bool:
    return detect_preview_kind(filename_or_path) != "unsupported"


def supports_converted_pdf_preview(filename_or_path: str | Path) -> bool:
    return Path(str(filename_or_path)).suffix.lower() in _CONVERTIBLE_TO_PDF_SUFFIXES


def supports_generated_html_preview(filename_or_path: str | Path) -> bool:
    return Path(str(filename_or_path)).suffix.lower() in {".docx", ".hwpx"}


def detect_document_preview_kind(
    original_filename_or_path: str | Path,
    source_filename_or_path: str | Path | None = None,
) -> PreviewKind:
    preview_kind = detect_preview_kind(original_filename_or_path)
    if preview_kind != "unsupported":
        return preview_kind

    if supports_generated_html_preview(original_filename_or_path):
        return "html"
    if source_filename_or_path and supports_generated_html_preview(source_filename_or_path):
        return "html"

    if supports_converted_pdf_preview(original_filename_or_path):
        return "pdf"
    if source_filename_or_path and supports_converted_pdf_preview(source_filename_or_path):
        return "pdf"
    return "unsupported"


def resolve_preview_media_type(path: Path) -> str | None:
    preview_kind = detect_preview_kind(path)
    if preview_kind == "pdf":
        return "application/pdf"
    if preview_kind == "image":
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if preview_kind == "html":
        return "text/html; charset=utf-8"
    if preview_kind == "text":
        return "text/plain; charset=utf-8"
    return None
