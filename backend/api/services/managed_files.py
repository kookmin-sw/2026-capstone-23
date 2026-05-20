import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from core.config import EXCLUDE_FILES, SUPPORTED_EXTENSIONS
from infra.storage.settings import get_configured_storage_path


ManagedScope = Literal["input", "output", "tmp"]


def _scope_root(config, scope: ManagedScope) -> Path:
    if scope == "input":
        return config.input_root.resolve()
    if scope == "output":
        return get_configured_storage_path(config.output_root).resolve()
    if scope == "tmp":
        return config.tmp_root.resolve()
    raise ValueError(f"unsupported scope: {scope}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _to_iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def resolve_managed_path(
    config,
    raw_path: str,
    *,
    scopes: Iterable[ManagedScope] = ("input", "output"),
    must_exist: bool = True,
) -> tuple[Path, ManagedScope, Path]:
    path = Path(raw_path).expanduser().resolve()

    for scope in scopes:
        root = _scope_root(config, scope)
        if _is_relative_to(path, root):
            if must_exist and not path.exists():
                raise FileNotFoundError(str(path))
            return path, scope, root

    raise PermissionError(str(path))


def _scan_input_files(config) -> list[Path]:
    root = _scope_root(config, "input")
    if not root.exists():
        return []

    items: list[Path] = []
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.name.startswith(".") and file_path.name not in EXCLUDE_FILES:
            continue
        if file_path.name in EXCLUDE_FILES:
            continue
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        items.append(file_path.resolve())
    return sorted(items)


def _scan_output_files(config) -> list[Path]:
    root = _scope_root(config, "output")
    if not root.exists():
        return []
    return sorted(path.resolve() for path in root.rglob("*.txt") if path.is_file())


def list_managed_files(config, scope: ManagedScope) -> list[dict[str, Any]]:
    root = _scope_root(config, scope)
    paths = _scan_input_files(config) if scope == "input" else _scan_output_files(config)

    items: list[dict[str, Any]] = []
    for path in paths:
        stat = path.stat()
        items.append(
            {
                "scope": scope,
                "name": path.name,
                "path": str(path),
                "relativePath": str(path.relative_to(root)).replace("\\", "/"),
                "sizeBytes": stat.st_size,
                "modifiedAt": _to_iso(stat.st_mtime),
                "extension": path.suffix.lower(),
            }
        )
    return items


def extract_html_content(text: str) -> str:
    if not text:
        return ""
    matches = re.findall(r"\[\[TABLE\]\](.*?)\[\[/TABLE\]\]", text, re.DOTALL)
    return "\n\n".join(match.strip() for match in matches if match.strip())


def format_markdown(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s*(#?\s*TableTitle:)", r"\n\1", text)
    text = re.sub(r"\s*(#{1,6}\s+)", r"\n\1", text)
    text = re.sub(r"\s+-\s+", r"\n- ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_markdown_content(content: str) -> str:
    if "[[TABLE_MARKDOWN]]" in content:
        matches = re.findall(r"\[\[TABLE_MARKDOWN\]\](.*?)\[\[/TABLE_MARKDOWN\]\]", content, re.DOTALL)
        formatted = [format_markdown(match.strip()) for match in matches if match.strip()]
        if formatted:
            return "\n\n---\n\n".join(formatted)

    if "# TableTitle:" in content:
        image_matches = re.findall(r"\[\[IMAGE[^\]]*\]\](.*?)\[\[/IMAGE\]\]", content, re.DOTALL)
        sections: list[str] = []
        for image_content in image_matches:
            match = re.search(r"# TableTitle:.*", image_content, re.DOTALL)
            if match:
                sections.append(format_markdown(match.group(0).strip()))
        if sections:
            return "\n\n---\n\n".join(sections)

    return ""


def extract_image_content(text: str) -> list[str]:
    if not text:
        return []

    matches = re.findall(r"\[\[IMAGE[^\]]*\]\](.*?)\[\[/IMAGE\]\]", text, re.DOTALL)
    results = [match.strip() for match in matches if match.strip()]

    placeholder_matches = re.findall(r"\[\[IMAGE id=[^\]]+\]\]", text)
    if placeholder_matches and not results:
        return [
            "VLM response is missing. Image analysis may not have completed or may have been truncated.",
        ]

    return results


def build_preview_html(content: str) -> str:
    lines = content.splitlines()
    meta_lines: list[str] = []
    for line in lines:
        if line.startswith("원본 파일:") or line.startswith("페이지 수:") or line.strip() == "-" * 60:
            meta_lines.append(line)
            if line.strip() == "-" * 60:
                break
        elif meta_lines:
            break

    html_section = extract_html_content(content)
    markdown_section = extract_markdown_content(content)
    image_sections = extract_image_content(content)

    html_parts = ["<div class='markdown-viewer'>"]
    if meta_lines:
        html_parts.append(f"<div class='meta-info'>{'<br>'.join(meta_lines)}</div>")
    if html_section:
        html_parts.append("<div class='section-header'>HTML Preview</div>")
        html_parts.append(f"<div class='html-preview'>{html_section}</div>")
    if markdown_section:
        html_parts.append("<div class='section-header'>Markdown Preview</div>")
        html_parts.append(f"<div class='image-preview'><pre>{markdown_section}</pre></div>")
    for idx, image_section in enumerate(image_sections, start=1):
        html_parts.append(f"<div class='section-header'>Image Description {idx}</div>")
        html_parts.append(f"<div class='image-preview'><pre>{image_section}</pre></div>")
    if len(html_parts) == 1:
        html_parts.append("<div class='markdown-content'>Preview is empty.</div>")
    html_parts.append("</div>")
    return "".join(html_parts)


def get_output_preview(config, raw_path: str) -> dict[str, Any]:
    path, scope, root = resolve_managed_path(config, raw_path, scopes=("output",))
    if path.suffix.lower() != ".txt":
        raise ValueError("only output txt files are previewable")

    content = path.read_text(encoding="utf-8")
    meta_path = path.with_suffix(".meta.json")
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}

    stat = path.stat()
    html_section = extract_html_content(content)
    markdown_section = extract_markdown_content(content)
    image_sections = extract_image_content(content)

    return {
        "scope": scope,
        "path": str(path),
        "relativePath": str(path.relative_to(root)).replace("\\", "/"),
        "fileName": path.name,
        "sizeBytes": stat.st_size,
        "modifiedAt": _to_iso(stat.st_mtime),
        "rawText": content,
        "previewHtml": build_preview_html(content),
        "hasHtml": bool(html_section.strip()),
        "htmlSection": html_section,
        "markdownSection": markdown_section,
        "imageSections": image_sections,
        "meta": meta,
    }


def delete_managed_paths(config, raw_paths: Iterable[str]) -> dict[str, Any]:
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    deleted_meta: list[str] = []

    for raw_path in raw_paths:
        try:
            path, scope, _ = resolve_managed_path(config, raw_path, scopes=("input", "output"), must_exist=False)
            if not path.exists():
                failed.append({"path": str(path), "reason": "missing"})
                continue
            if not path.is_file():
                failed.append({"path": str(path), "reason": "not_a_file"})
                continue

            path.unlink()
            deleted.append(str(path))

            if scope == "output" and path.suffix.lower() == ".txt":
                meta_path = path.with_suffix(".meta.json")
                if meta_path.exists() and meta_path.is_file():
                    meta_path.unlink()
                    deleted_meta.append(str(meta_path))
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            failed.append({"path": raw_path, "reason": str(exc)})

    return {
        "deletedCount": len(deleted),
        "deletedPaths": deleted,
        "deletedMetaPaths": deleted_meta,
        "failedCount": len(failed),
        "failedPaths": failed,
    }


def create_download_artifact(config, raw_paths: Iterable[str]) -> Path:
    resolved: list[Path] = []
    for raw_path in raw_paths:
        path, _, _ = resolve_managed_path(config, raw_path, scopes=("input", "output", "tmp"))
        if not path.is_file():
            raise ValueError(f"not a file: {path}")
        resolved.append(path)

    unique_paths = list(dict.fromkeys(resolved))
    if not unique_paths:
        raise ValueError("paths must not be empty")

    if len(unique_paths) == 1:
        return unique_paths[0]

    tmp_root = _scope_root(config, "tmp")
    tmp_root.mkdir(parents=True, exist_ok=True)
    archive_name = f"luminir_files_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    archive_path = tmp_root / archive_name

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for path in unique_paths:
            arcname = path.name
            for scope in ("input", "output"):
                root = _scope_root(config, scope)
                if _is_relative_to(path, root):
                    arcname = str(path.relative_to(root)).replace("\\", "/")
                    break
            zip_file.write(path, arcname=arcname)

    return archive_path
