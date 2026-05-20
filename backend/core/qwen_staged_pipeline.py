from __future__ import annotations

import copy
import hashlib
import io
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image

from core.config import AppConfig
from core.converters import convert_to_pdf, extract_text_from_hwp_pyhwp, parse_hwp_document_direct, parse_hwpx_document
from core.output import build_document_text, write_outputs
from core.pdf_extractor import extract_pdf
from core.pipeline import merge_hwp_source_text_with_structured_blocks, split_hwp_source_text_into_pages

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}
_PAGE_HEADER_PATTERN = re.compile(r"(?m)^## Page (\d+)\s*$")


def _normalize_bbox(value: Any) -> list[float]:
    bbox = value or [0, 0, 0, 0]
    if not isinstance(bbox, list):
        bbox = list(bbox)
    if len(bbox) != 4:
        return [0, 0, 0, 0]
    return [float(part) for part in bbox]


def _make_relative_output_path(source_path: Path, config: AppConfig) -> Path:
    try:
        rel = source_path.resolve().relative_to(config.input_root.resolve())
    except ValueError:
        rel = Path(source_path.name)
    return config.output_root / rel.parent / f"{rel.stem}.txt"


def _persist_task_image(tmp_root: Path, job_item_id: str, page_num: int, image_id: str, image_bytes: bytes) -> Path:
    safe_image_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", image_id).strip("_") or "image"
    ext = ".png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    task_dir = tmp_root / "qwen_staged" / job_item_id / "images"
    task_dir.mkdir(parents=True, exist_ok=True)
    file_path = task_dir / f"page_{page_num:04d}_{safe_image_id}{ext}"
    file_path.write_bytes(image_bytes)
    return file_path


def cleanup_staged_artifacts(*, tmp_root: Path, job_item_id: str) -> bool:
    if not job_item_id:
        return False

    root = (tmp_root / "qwen_staged").resolve()
    target = (root / job_item_id).resolve()
    if target == root:
        return False

    try:
        target.relative_to(root)
    except ValueError:
        return False

    if not target.exists():
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


def _build_image_page(source_path: Path, job_item_id: str, tmp_root: Path) -> dict[str, Any]:
    image_bytes = source_path.read_bytes()
    with Image.open(io.BytesIO(image_bytes)) as image:
        width, height = image.size

    image_id = "page-1-img-1"
    image_path = _persist_task_image(tmp_root, job_item_id, 1, image_id, image_bytes)
    page = {
        "page": 1,
        "text": "",
        "images": [
            {
                "image_id": image_id,
                "page": 1,
                "bbox": [0, 0, width, height],
                "sha1": hashlib.sha1(image_bytes, usedforsecurity=False).hexdigest(),
            }
        ],
        "elements": [
            {
                "type": "image",
                "image_id": image_id,
                "bbox": [0, 0, width, height],
            }
        ],
    }
    infer_input = {
        "pageNum": 1,
        "imageId": image_id,
        "imagePath": str(image_path),
        "bbox": [0, 0, width, height],
        "isTable": None,
        "isFlowchart": None,
        "isMath": None,
    }
    return {
        "pdfPath": "",
        "pageCount": 1,
        "pages": [page],
        "sourceTextPages": [],
        "inferenceInputs": [infer_input],
    }


def _sanitize_page(page: Dict[str, Any], job_item_id: str, tmp_root: Path) -> tuple[Dict[str, Any], list[dict[str, Any]]]:
    page_num = int(page.get("page", 1))
    page_images: list[dict[str, Any]] = []
    infer_inputs: list[dict[str, Any]] = []

    for image in page.get("images", []):
        image_id = str(image.get("image_id") or f"page-{page_num}-image-{len(page_images) + 1}")
        image_bytes = image.get("bytes")
        image_meta = {
            "image_id": image_id,
            "page": page_num,
            "bbox": _normalize_bbox(image.get("bbox")),
            "sha1": str(image.get("sha1") or ""),
            "is_table": bool(image.get("is_table") or image.get("is_text_table")),
            "is_flowchart": image.get("is_flowchart"),
        }
        page_images.append(image_meta)
        if image_bytes:
            image_path = _persist_task_image(tmp_root, job_item_id, page_num, image_id, bytes(image_bytes))
            infer_inputs.append(
                {
                    "pageNum": page_num,
                    "imageId": image_id,
                    "imagePath": str(image_path),
                    "bbox": image_meta["bbox"],
                    "isTable": image_meta["is_table"] or None,
                    "isFlowchart": image_meta.get("is_flowchart"),
                    "isMath": None,
                }
            )

    page_elements: list[dict[str, Any]] = []
    for element in page.get("elements", []):
        elem_type = str(element.get("type") or "")
        if elem_type == "text":
            page_elements.append(
                {
                    "type": "text",
                    "bbox": _normalize_bbox(element.get("bbox")),
                    "content": str(element.get("content") or ""),
                }
            )
        elif elem_type == "table":
            page_elements.append(
                {
                    "type": "table",
                    "bbox": _normalize_bbox(element.get("bbox")),
                    "content": str(element.get("content") or ""),
                    "markdown": str(element.get("markdown") or ""),
                }
            )
        elif elem_type == "image":
            page_elements.append(
                {
                    "type": "image",
                    "bbox": _normalize_bbox(element.get("bbox")),
                    "image_id": str(element.get("image_id") or ""),
                }
            )

    sanitized_page = {
        "page": page_num,
        "text": str(page.get("text") or ""),
        "images": page_images,
        "elements": page_elements,
    }
    return sanitized_page, infer_inputs


def preprocess_document_for_qwen(*, source_path: Path, job_item_id: str, config: AppConfig) -> Dict[str, Any]:
    source_path = source_path.resolve()
    suffix = source_path.suffix.lower()

    if suffix in IMAGE_EXTENSIONS:
        payload = _build_image_page(source_path, job_item_id, config.tmp_root)
        payload["sourceType"] = "image"
        return payload

    if suffix == ".hwpx":
        parsed = parse_hwpx_document(source_path)
        pages: list[dict[str, Any]] = []
        infer_inputs: list[dict[str, Any]] = []
        for page in parsed.get("pages", []):
            sanitized_page, page_inputs = _sanitize_page(page, job_item_id, config.tmp_root)
            pages.append(sanitized_page)
            infer_inputs.extend(page_inputs)
        return {
            "sourceType": "hwpx_direct_xml",
            "pdfPath": "",
            "pageCount": int(parsed.get("page_count", len(pages))),
            "pages": pages,
            "sourceTextPages": [],
            "inferenceInputs": infer_inputs,
            "tables": parsed.get("tables", []),
        }

    if suffix == ".hwp":
        try:
            parsed = parse_hwp_document_direct(source_path, tmp_dir=config.tmp_root)
            pages: list[dict[str, Any]] = []
            infer_inputs: list[dict[str, Any]] = []
            for page in parsed.get("pages", []):
                sanitized_page, page_inputs = _sanitize_page(page, job_item_id, config.tmp_root)
                pages.append(sanitized_page)
                infer_inputs.extend(page_inputs)
            return {
                "sourceType": "hwp_direct_pyhwp",
                "pdfPath": "",
                "pageCount": int(parsed.get("page_count", len(pages))),
                "pages": pages,
                "sourceTextPages": [],
                "inferenceInputs": infer_inputs,
                "tables": [],
            }
        except Exception as exc:  # noqa: BLE001
            print(f"[qwen-stage] direct hwp parsing failed; falling back to PDF path: {exc}")

    pdf_result_path = source_path
    if suffix != ".pdf":
        converted = convert_to_pdf(source_path, config.tmp_root)
        pdf_result_path = converted[0] if isinstance(converted, tuple) else converted

    pdf_data = extract_pdf(pdf_result_path)
    pages: list[dict[str, Any]] = []
    infer_inputs: list[dict[str, Any]] = []
    for page in pdf_data.get("pages", []):
        sanitized_page, page_inputs = _sanitize_page(page, job_item_id, config.tmp_root)
        pages.append(sanitized_page)
        infer_inputs.extend(page_inputs)

    source_text_pages: list[str] = []
    if suffix in {".hwp", ".hwpx"}:
        try:
            source_text = extract_text_from_hwp_pyhwp(source_path)
            source_text_pages = split_hwp_source_text_into_pages(source_text, len(pages))
        except Exception as exc:  # noqa: BLE001
            print(f"[qwen-stage] direct hwp text extraction skipped: {exc}")

    return {
        "sourceType": "document",
        "pdfPath": str(pdf_result_path),
        "pageCount": int(pdf_data.get("page_count", len(pages))),
        "pages": pages,
        "sourceTextPages": source_text_pages,
        "inferenceInputs": infer_inputs,
    }


def _split_page_sections(content: str) -> tuple[str, list[tuple[int, str]]]:
    matches = list(_PAGE_HEADER_PATTERN.finditer(content))
    if not matches:
        return content, []

    header = content[: matches[0].start()]
    sections: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        page_num = int(match.group(1))
        sections.append((page_num, content[start:end].strip()))
    return header, sections


def _merge_hwp_pages(content: str, source_text_pages: list[str]) -> str:
    if not source_text_pages:
        return content

    header, sections = _split_page_sections(content)
    if not sections:
        return content

    merged_sections: list[str] = []
    for page_num, page_body in sections:
        source_page_text = source_text_pages[page_num - 1] if page_num - 1 < len(source_text_pages) else ""
        merged_body = merge_hwp_source_text_with_structured_blocks(source_page_text, page_body)
        merged_sections.append(f"## Page {page_num}\n{merged_body}".rstrip())

    prefix = header.rstrip()
    suffix = "\n\n".join(merged_sections).strip()
    if prefix:
        return f"{prefix}\n\n{suffix}\n"
    return f"{suffix}\n"


def finalize_document_from_qwen_results(
    *,
    source_path: Path,
    output_path: Path,
    preprocess_payload: Dict[str, Any],
    infer_results: List[Dict[str, Any]],
) -> Path:
    pages = copy.deepcopy(list(preprocess_payload.get("pages") or []))
    image_results: dict[str, Dict[str, Any]] = {}
    for infer_result in infer_results:
        image_id = str(infer_result.get("imageId") or "")
        if not image_id:
            continue
        image_results[image_id] = dict(infer_result.get("result") or {})

    content = build_document_text(
        source_path=source_path,
        page_count=int(preprocess_payload.get("pageCount", len(pages))),
        pages=pages,
        image_results=image_results,
    )
    source_type = str(preprocess_payload.get("sourceType") or "")
    if source_type not in {"hwpx_direct_xml", "hwp_direct_pyhwp"}:
        content = _merge_hwp_pages(content, list(preprocess_payload.get("sourceTextPages") or []))

    meta = {
        "source": str(source_path),
        "page_count": int(preprocess_payload.get("pageCount", len(pages))),
        "pipeline": source_type if source_type in {"hwpx_direct_xml", "hwp_direct_pyhwp"} else "qwen_staged",
        "pdfPath": str(preprocess_payload.get("pdfPath") or ""),
        "images": [
            {
                "image_id": image.get("image_id"),
                "page": image.get("page"),
                "bbox": image.get("bbox"),
                "sha1": image.get("sha1"),
                "vlm": image_results.get(str(image.get("image_id") or ""), {}),
            }
            for page in pages
            for image in list(page.get("images") or [])
        ],
    }

    write_outputs(output_path, content, meta)
    return output_path


def output_path_for_source(source_path: Path, config: AppConfig) -> Path:
    return _make_relative_output_path(source_path, config)
