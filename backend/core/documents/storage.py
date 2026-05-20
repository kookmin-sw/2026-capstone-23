from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from core.documents.hwpx_preview import render_hwpx_preview_pdf_bytes
from core.documents.records import normalize_filename
from infra.storage.file_assets import (
    materialize_binary_asset,
    materialize_record_asset,
    store_binary_asset,
)
from core.converters import (
    ConversionError,
    convert_hwp_to_pdf_via_libreoffice,
    convert_to_pdf,
)


HWP_SOURCE_SUFFIXES = {".hwp", ".hwpx"}
HWPX_SOURCE_SUFFIX = ".hwpx"
PDF_PREVIEW_CONVERSION_SOURCE_SUFFIXES = {
    ".hwp",
    ".hwpx",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
}


def prepare_document_assets(
    *,
    document_id: str,
    filename: str,
    content: bytes,
    tmp_root: Path,
) -> dict[str, Any]:
    source_filename = normalize_filename(filename)
    source_asset = store_binary_asset(
        category="document_source",
        filename=source_filename,
        content=content,
        document_id=document_id,
    )

    original_asset = source_asset
    original_storage_kind = "source"

    source_suffix = Path(source_filename).suffix.lower()
    if source_suffix in HWP_SOURCE_SUFFIXES:
        with materialize_binary_asset(
            file_id=source_asset["fileId"],
            filename=source_filename,
            tmp_root=tmp_root,
            purpose="hwp_source",
            owner_id=document_id,
        ) as source_path:
            try:
                pdf_bytes = _convert_preview_source_to_pdf_bytes(source_path)
            except ConversionError:
                if source_suffix != HWPX_SOURCE_SUFFIX:
                    raise
                pdf_bytes = None

        if pdf_bytes is None:
            return {
                "sourceFileId": source_asset["fileId"],
                "sourceFilename": source_asset["filename"],
                "sourceFileType": source_asset["fileType"],
                "sourceFilePath": source_asset["storagePath"],
                "originalFileId": original_asset["fileId"],
                "originalFilename": original_asset["filename"],
                "fileType": original_asset["fileType"],
                "originalFilePath": original_asset["storagePath"],
                "originalStorageKind": original_storage_kind,
            }

        original_filename = f"{Path(source_filename).stem}.pdf"
        original_asset = store_binary_asset(
            category="document_original",
            filename=original_filename,
            content=pdf_bytes,
            media_type="application/pdf",
            document_id=document_id,
            metadata={
                "derivedFromSourceFileId": source_asset["fileId"],
                "sourceFilename": source_filename,
            },
        )
        original_storage_kind = "converted_pdf"

    return {
        "sourceFileId": source_asset["fileId"],
        "sourceFilename": source_asset["filename"],
        "sourceFileType": source_asset["fileType"],
        "sourceFilePath": source_asset["storagePath"],
        "originalFileId": original_asset["fileId"],
        "originalFilename": original_asset["filename"],
        "fileType": original_asset["fileType"],
        "originalFilePath": original_asset["storagePath"],
        "originalStorageKind": original_storage_kind,
    }


def can_generate_pdf_preview_from_source(filename: str) -> bool:
    return (
        Path(normalize_filename(filename)).suffix.lower()
        in PDF_PREVIEW_CONVERSION_SOURCE_SUFFIXES
    )


def _convert_preview_source_to_pdf_bytes(source_path: Path) -> bytes:
    if source_path.suffix.lower() == HWPX_SOURCE_SUFFIX:
        converted = convert_hwp_to_pdf_via_libreoffice(source_path, source_path.parent)
        if converted:
            pdf_path = converted[0] if isinstance(converted, tuple) else converted
            return Path(pdf_path).read_bytes()

        try:
            return render_hwpx_preview_pdf_bytes(
                source_path.read_bytes(), filename=source_path.name
            )
        except Exception as exc:  # noqa: BLE001
            raise ConversionError(
                "HWPX PDF 미리보기를 LibreOffice/HTML 경로 모두에서 생성하지 못했습니다."
            ) from exc
    else:
        converted = convert_to_pdf(source_path, source_path.parent)

    pdf_path = converted[0] if isinstance(converted, tuple) else converted
    return Path(pdf_path).read_bytes()


def generate_pdf_preview_asset_for_record(
    *,
    record: dict[str, Any],
    document_id: str,
    tmp_root: Path,
) -> dict[str, Any]:
    source_filename = str(
        record.get("sourceFilename") or record.get("originalFilename") or document_id
    )
    if not can_generate_pdf_preview_from_source(source_filename):
        raise ValueError(
            f"pdf preview conversion is not supported for this file type: {Path(source_filename).suffix.lower()}"
        )

    with materialize_record_asset(
        record,
        file_id_key="sourceFileId",
        filename_key="sourceFilename",
        fallback_path_key="sourceFilePath",
        tmp_root=tmp_root,
        purpose="preview_pdf",
        owner_id=document_id,
    ) as source_path:
        pdf_bytes = _convert_preview_source_to_pdf_bytes(source_path)

    original_filename = f"{Path(normalize_filename(source_filename)).stem}.pdf"
    return store_binary_asset(
        category="document_original",
        filename=original_filename,
        content=pdf_bytes,
        media_type="application/pdf",
        document_id=document_id,
        metadata={
            "derivedFromSourceFileId": str(record.get("sourceFileId") or ""),
            "sourceFilename": source_filename,
            "generatedFor": "inline_preview",
        },
    )


def result_filename_for(source_filename: str) -> str:
    return f"{Path(normalize_filename(source_filename)).stem}.txt"


def load_output_artifacts(output_path: Path) -> tuple[str, dict[str, Any]]:
    content = output_path.read_text(encoding="utf-8", errors="ignore")
    meta_path = output_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return content, {}

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        meta = {}
    return content, meta


def _rewrite_output_header(content: str, display_source: str) -> str:
    if not content:
        return content

    has_trailing_newline = content.endswith("\n")
    lines = content.splitlines()
    if not lines:
        return content
    if not lines[0].lstrip().startswith("원본 파일:"):
        return content
    lines[0] = f"원본 파일: {display_source}"
    rewritten = "\n".join(lines)
    if has_trailing_newline:
        rewritten += "\n"
    return rewritten


def sanitize_output_meta(
    meta: dict[str, Any],
    *,
    display_source: str,
    display_pdf_path: str | None,
) -> dict[str, Any]:
    sanitized = dict(meta or {})
    sanitized["source"] = display_source
    if display_pdf_path is not None:
        sanitized["pdfPath"] = display_pdf_path
    return sanitized


def _safe_result_document_id(document_id: str) -> str:
    normalized = normalize_filename(document_id)
    return (
        normalized
        if normalized not in {"", ".", ".."}
        else f"d_{uuid.uuid4().hex[:12]}"
    )


def _write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def build_result_storage_path(
    *, storage_root: Path, document_id: str, filename: str
) -> Path:
    return (
        storage_root.expanduser()
        / "results"
        / _safe_result_document_id(document_id)
        / normalize_filename(filename)
    )


def store_result_files(
    *,
    storage_root: Path,
    document_id: str,
    output_filename: str,
    content: str,
    meta: dict[str, Any],
) -> Path:
    result_path = build_result_storage_path(
        storage_root=storage_root,
        document_id=document_id,
        filename=output_filename,
    )
    _write_text_file(result_path, content)
    _write_text_file(
        result_path.with_suffix(".meta.json"),
        json.dumps(meta, ensure_ascii=False, indent=2),
    )
    return result_path


def persist_result_artifact(
    *,
    document_id: str,
    output_path: Path,
    output_filename: str,
    display_source: str,
    display_pdf_path: str | None,
    storage_root: Path,
) -> dict[str, Any]:
    content, meta = load_output_artifacts(output_path)
    sanitized_content = _rewrite_output_header(content, display_source)
    sanitized_meta = sanitize_output_meta(
        meta,
        display_source=display_source,
        display_pdf_path=display_pdf_path,
    )
    result_path = store_result_files(
        storage_root=storage_root,
        document_id=document_id,
        output_filename=output_filename,
        content=sanitized_content,
        meta=sanitized_meta,
    )
    asset = store_binary_asset(
        category="document_result",
        filename=output_filename,
        content=sanitized_content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        document_id=document_id,
        metadata={"meta": sanitized_meta, "fileSystemPath": str(result_path)},
    )
    asset["dbStoragePath"] = asset["storagePath"]
    asset["storagePath"] = str(result_path)
    asset["fileSystemPath"] = str(result_path)
    asset["content"] = sanitized_content
    asset["meta"] = sanitized_meta
    return asset


def cleanup_output_artifacts(output_path: Path) -> None:
    meta_path = output_path.with_suffix(".meta.json")
    for path in (output_path, meta_path):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            continue
