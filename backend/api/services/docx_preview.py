from __future__ import annotations

import base64
import html
import io
import mimetypes
import posixpath
import zipfile
from pathlib import PurePosixPath
from typing import Any

from defusedxml import ElementTree

from api.services.original_content import _local_name
from core.converters import ConversionError


_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_PART = PurePosixPath("word/document.xml")


def _resolve_target_path(base_part: PurePosixPath, target: str) -> str:
    normalized = posixpath.normpath((base_part.parent / target).as_posix())
    return normalized.lstrip("/")


def _load_relationships(archive: zipfile.ZipFile, part_name: PurePosixPath) -> dict[str, str]:
    rels_path = part_name.parent / "_rels" / f"{part_name.name}.rels"
    if rels_path.as_posix() not in archive.namelist():
        return {}

    root = ElementTree.fromstring(archive.read(rels_path.as_posix()))
    relationships: dict[str, str] = {}
    for rel in root.findall(f"{{{_REL_NS}}}Relationship"):
        rel_id = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if not rel_id or not target:
            continue
        relationships[rel_id] = _resolve_target_path(part_name, target)
    return relationships


def _escape_text(value: str) -> str:
    return html.escape(value, quote=False)


def _render_image(archive: zipfile.ZipFile, relationships: dict[str, str], drawing: Any) -> str:
    for node in drawing.iter():
        if _local_name(node.tag) != "blip":
            continue
        rel_id = node.attrib.get(f"{{{_NS['r']}}}embed") or node.attrib.get(f"{{{_NS['r']}}}link")
        if not rel_id:
            continue
        target = relationships.get(rel_id)
        if not target or target not in archive.namelist():
            continue
        payload = archive.read(target)
        media_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        encoded = base64.b64encode(payload).decode("ascii")
        alt = _escape_text(PurePosixPath(target).name)
        return f"<img class=\"docx-image\" src=\"data:{media_type};base64,{encoded}\" alt=\"{alt}\" />"
    return ""


def _render_run_children(archive: zipfile.ZipFile, relationships: dict[str, str], parent: Any) -> str:
    fragments: list[str] = []
    for child in list(parent):
        local_name = _local_name(child.tag)
        if local_name == "t" and child.text:
            fragments.append(_escape_text(child.text))
            continue
        if local_name in {"br", "cr"}:
            fragments.append("<br/>")
            continue
        if local_name == "tab":
            fragments.append("&emsp;")
            continue
        if local_name == "drawing":
            image_html = _render_image(archive, relationships, child)
            if image_html:
                fragments.append(image_html)
            continue
        nested_html = _render_run_children(archive, relationships, child)
        if nested_html:
            if local_name == "hyperlink":
                fragments.append(f"<span class=\"docx-link\">{nested_html}</span>")
            else:
                fragments.append(nested_html)
    return "".join(fragments)


def _paragraph_tag(paragraph: Any) -> str:
    style = paragraph.find("w:pPr/w:pStyle", _NS)
    style_name = str(style.attrib.get(f"{{{_NS['w']}}}val") or "").lower() if style is not None else ""
    if style_name in {"title", "heading1"}:
        return "h1"
    if style_name == "heading2":
        return "h2"
    if style_name == "heading3":
        return "h3"
    return "p"


def _render_paragraph(archive: zipfile.ZipFile, relationships: dict[str, str], paragraph: Any) -> str:
    inner_html = _render_run_children(archive, relationships, paragraph)
    if not inner_html.strip():
        return ""
    tag = _paragraph_tag(paragraph)
    return f"<{tag}>{inner_html}</{tag}>"


def _render_table(archive: zipfile.ZipFile, relationships: dict[str, str], table: Any) -> str:
    rows: list[str] = []
    for row in table.findall("w:tr", _NS):
        cells: list[str] = []
        for cell in row.findall("w:tc", _NS):
            blocks = _render_block_children(archive, relationships, cell)
            cells.append(f"<td>{''.join(blocks) or '<p></p>'}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    if not rows:
        return ""
    return f"<table class=\"docx-table\"><tbody>{''.join(rows)}</tbody></table>"


def _render_block_children(archive: zipfile.ZipFile, relationships: dict[str, str], parent: Any) -> list[str]:
    fragments: list[str] = []
    for child in list(parent):
        local_name = _local_name(child.tag)
        if local_name == "p":
            paragraph_html = _render_paragraph(archive, relationships, child)
            if paragraph_html:
                fragments.append(paragraph_html)
            continue
        if local_name == "tbl":
            table_html = _render_table(archive, relationships, child)
            if table_html:
                fragments.append(table_html)
    return fragments


def _iter_header_footer_entries(archive: zipfile.ZipFile) -> list[str]:
    entries = [
        name
        for name in archive.namelist()
        if name.startswith("word/header") or name.startswith("word/footer")
    ]
    return sorted(name for name in entries if name.endswith(".xml"))


def _render_header_footer_sections(archive: zipfile.ZipFile) -> str:
    sections: list[str] = []
    for entry_name in _iter_header_footer_entries(archive):
        root = ElementTree.fromstring(archive.read(entry_name))
        relationships = _load_relationships(archive, PurePosixPath(entry_name))
        blocks = _render_block_children(archive, relationships, root)
        if not blocks:
            continue
        title = _escape_text(PurePosixPath(entry_name).stem.replace("_", " ").title())
        sections.append(f"<section class=\"docx-meta\"><h2>{title}</h2>{''.join(blocks)}</section>")
    return "".join(sections)


def render_docx_preview_html(docx_bytes: bytes, *, filename: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as archive:
            if _DOC_PART.as_posix() not in archive.namelist():
                raise ConversionError(f"DOCX preview body is missing: {filename}")

            root = ElementTree.fromstring(archive.read(_DOC_PART.as_posix()))
            relationships = _load_relationships(archive, _DOC_PART)
            body = root.find("w:body", _NS)
            if body is None:
                raise ConversionError(f"DOCX preview body is empty: {filename}")

            blocks = _render_block_children(archive, relationships, body)
            if not blocks:
                raise ConversionError(f"DOCX preview content could not be extracted: {filename}")

            extras = _render_header_footer_sections(archive)
    except zipfile.BadZipFile as exc:
        raise ConversionError(f"Invalid DOCX file: {filename}") from exc

    title = _escape_text(filename)
    content = "".join(blocks)
    return (
        "<!doctype html>"
        "<html lang=\"ko\">"
        "<head>"
        "<meta charset=\"utf-8\"/>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>"
        f"<title>{title}</title>"
        "<style>"
        "body{margin:0;background:#f4f1ea;color:#1f2937;font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;}"
        ".docx-shell{max-width:960px;margin:0 auto;padding:32px 24px 48px;}"
        ".docx-header{margin-bottom:24px;padding-bottom:12px;border-bottom:1px solid #d6d0c4;}"
        ".docx-header h1{margin:0;font-size:20px;font-weight:700;word-break:break-word;}"
        ".docx-body{background:#fff;border:1px solid #ded8cc;box-shadow:0 10px 30px rgba(15,23,42,.08);padding:28px 30px;}"
        "p,h1,h2,h3{margin:0 0 14px;line-height:1.7;word-break:break-word;}"
        "h1{font-size:28px;}h2{font-size:22px;}h3{font-size:18px;}"
        ".docx-table{width:100%;border-collapse:collapse;margin:18px 0 22px;}"
        ".docx-table td{border:1px solid #cfc7b8;padding:10px 12px;vertical-align:top;}"
        ".docx-image{display:block;max-width:100%;height:auto;margin:14px 0;border-radius:4px;}"
        ".docx-link{text-decoration:underline;}"
        ".docx-meta{margin-top:28px;padding-top:20px;border-top:1px dashed #d6d0c4;}"
        "@media (max-width:768px){.docx-shell{padding:18px 12px 28px;}.docx-body{padding:18px 16px;}}"
        "</style>"
        "</head>"
        "<body>"
        "<div class=\"docx-shell\">"
        f"<div class=\"docx-header\"><h1>{title}</h1></div>"
        f"<div class=\"docx-body\">{content}{extras}</div>"
        "</div>"
        "</body>"
        "</html>"
    )
