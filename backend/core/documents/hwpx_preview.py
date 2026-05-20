from __future__ import annotations

import base64
import html
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from bs4 import BeautifulSoup


_IMAGE_MIME_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_HWPUNIT_PER_INCH = 7200
_CSS_PX_PER_INCH = 96


ImageDescriber = Callable[[bytes, str, str], str]


def render_hwpx_preview_html(
    hwpx_bytes: bytes,
    *,
    filename: str,
    describe_image: ImageDescriber | None = None,
) -> str:
    with zipfile.ZipFile(BytesIO(hwpx_bytes)) as archive:
        names = set(archive.namelist())
        render_context = _build_render_context(archive, names)
        render_context["describe_image"] = describe_image
        body_parts: list[str] = []
        max_content_width = 1040

        for section_name in sorted(
            (name for name in names if name.startswith("Contents/section") and name.endswith(".xml")),
            key=_section_sort_key,
        ):
            soup = BeautifulSoup(archive.read(section_name), "xml")
            max_content_width = max(max_content_width, _section_content_width(soup), _max_table_width(soup))
            for node in soup.find_all(True):
                name = _local_name(node)
                if name == "tbl":
                    if _has_ancestor(node, {"tbl", "p"}):
                        continue
                    table_html = _render_table(node, render_context)
                    if table_html:
                        body_parts.append(table_html)
                elif name == "p":
                    if _has_ancestor(node, {"tbl", "p"}):
                        continue
                    paragraph_html = _render_paragraph(node, render_context)
                    if paragraph_html:
                        body_parts.append(paragraph_html)
                elif name == "pic":
                    if _has_ancestor(node, {"tbl", "p"}):
                        continue
                    image_html = _render_image(node, render_context)
                    if image_html:
                        body_parts.append(f'<figure class="image">{image_html}</figure>')

    if not body_parts:
        body_parts.append('<p class="empty">HWPX 미리보기 내용을 추출하지 못했습니다.</p>')

    title = html.escape(filename)
    content = "\n".join(body_parts)
    main_max_width = max(1040, min(max_content_width + 96, 1800))
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f4f6f8;
      color: #1f2933;
    }}
    body {{
      margin: 0;
      padding: 24px;
      overflow: auto;
    }}
    main {{
      box-sizing: border-box;
      width: {main_max_width}px;
      max-width: none;
      min-height: calc(100vh - 48px);
      margin: 0 auto;
      padding: 42px 48px;
      background: #fff;
      border: 1px solid #d8dee8;
      box-shadow: 0 12px 32px rgba(15, 23, 42, 0.08);
    }}
    p {{
      margin: 0 0 12px;
      font-size: 14px;
      line-height: 1.75;
      white-space: pre-wrap;
    }}
    table {{
      margin: 0;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }}
    .table-wrap {{
      max-width: 100%;
      overflow-x: auto;
      margin: 18px 0;
    }}
    .table-wrap table {{
      max-width: none;
    }}
    td, th {{
      border: 1px solid #9aa6b2;
      padding: 6px 8px;
      vertical-align: top;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    th {{
      background: #eef2f6;
      font-weight: 600;
    }}
    figure.image {{
      margin: 18px 0;
    }}
    figure.image img {{
      display: block;
      max-width: 100%;
      height: auto;
      border: 1px solid #e1e6ed;
    }}
    .image-vlm {{
      margin: 18px 0;
      padding: 12px 14px;
      border: 1px solid #d8dee8;
      background: #f8fafc;
    }}
    .image-vlm pre {{
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: inherit;
      line-height: 1.65;
    }}
    .image-vlm.error {{
      color: #8a1f11;
      background: #fff7f5;
      border-color: #f1b8ad;
    }}
    .empty {{
      color: #667085;
    }}
  </style>
</head>
<body>
  <main>
    {content}
  </main>
</body>
</html>
"""


def render_hwpx_preview_pdf_bytes(hwpx_bytes: bytes, *, filename: str) -> bytes:
    html_content = render_hwpx_preview_html(hwpx_bytes, filename=filename)
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError("weasyprint가 설치되어 있지 않아 HWPX HTML PDF 미리보기를 만들 수 없습니다.") from exc

    return HTML(string=html_content, base_url=".").write_pdf()


def _section_sort_key(name: str) -> tuple[int, str]:
    match = re.search(r"section(\d+)\.xml$", name)
    if not match:
        return (10**9, name)
    return (int(match.group(1)), name)


def _read_bin_data_refs(archive: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    refs: dict[str, str] = {}
    if "Contents/header.xml" in names:
        soup = BeautifulSoup(archive.read("Contents/header.xml"), "xml")
        for node in soup.find_all(True):
            if _local_name(node) != "binData":
                continue

            item_id = _first_attr(node, "id")
            href = _first_attr(node, "href")
            if item_id and href:
                refs[item_id] = href.lstrip("./")

    if "Contents/content.hpf" in names:
        soup = BeautifulSoup(archive.read("Contents/content.hpf"), "xml")
        for node in soup.find_all(True):
            if _local_name(node) != "item":
                continue

            item_id = _first_attr(node, "id")
            href = _first_attr(node, "href")
            media_type = (_first_attr(node, "media-type") or "").lower()
            if item_id and href and (href.lower().startswith("bindata/") or media_type.startswith("image/")):
                refs[item_id] = href.lstrip("./")
    return refs


def _build_render_context(archive: zipfile.ZipFile, names: set[str]) -> dict[str, Any]:
    context: dict[str, Any] = {
        "archive": archive,
        "names": names,
        "bin_refs": _read_bin_data_refs(archive, names),
        "border_fills": {},
        "char_prs": {},
        "para_prs": {},
    }
    if "Contents/header.xml" not in names:
        return context

    soup = BeautifulSoup(archive.read("Contents/header.xml"), "xml")
    for node in soup.find_all(True):
        node_id = _first_attr(node, "id")
        if not node_id:
            continue

        name = _local_name(node)
        if name == "borderFill":
            context["border_fills"][node_id] = _parse_border_fill(node)
        elif name == "charPr":
            context["char_prs"][node_id] = _parse_char_pr(node)
        elif name == "paraPr":
            context["para_prs"][node_id] = _parse_para_pr(node)
    return context


def _render_paragraph(
    paragraph: Any,
    render_context: dict[str, Any],
) -> str:
    parts: list[str] = []
    for child in paragraph.children:
        if not getattr(child, "name", None):
            continue

        name = _local_name(child)
        if name == "t":
            parts.append(html.escape(child.get_text()))
        elif name == "lineBreak":
            parts.append("<br />")
        elif name == "run":
            parts.append(_render_run(child, render_context))
        elif name == "pic":
            image_html = _render_image(child, render_context)
            if image_html:
                parts.append(image_html)
        elif name == "tbl":
            table_html = _render_table(child, render_context)
            if table_html:
                parts.append(table_html)

    rendered = "".join(parts).strip()
    if not rendered:
        return ""
    style = _paragraph_css(paragraph, render_context)
    style_attr = f' style="{html.escape(style, quote=True)}"' if style else ""
    return f"<p{style_attr}>{rendered}</p>"


def _render_run(run: Any, render_context: dict[str, Any]) -> str:
    parts: list[str] = []
    for child in run.children:
        if not getattr(child, "name", None):
            continue
        name = _local_name(child)
        if name == "t":
            parts.append(html.escape(child.get_text()))
        elif name == "lineBreak":
            parts.append("<br />")
        elif name == "pic":
            image_html = _render_image(child, render_context)
            if image_html:
                parts.append(image_html)
        elif name == "tbl":
            table_html = _render_table(child, render_context)
            if table_html:
                parts.append(table_html)

    rendered = "".join(parts)
    if not rendered:
        return ""

    style = _char_css(_first_attr(run, "charPrIDRef"), render_context)
    if not style:
        return rendered
    return f'<span style="{html.escape(style, quote=True)}">{rendered}</span>'


def _render_table(table: Any, render_context: dict[str, Any]) -> str:
    rows: list[str] = []
    col_widths: dict[int, int] = {}
    table_rows = [
        row
        for row in table.find_all(lambda tag: _local_name(tag) == "tr")
        if not _has_ancestor_between(row, table, {"tbl"})
    ]
    for row_index, row in enumerate(table_rows):
        cells: list[str] = []
        for cell in _direct_children_by_name(row, "tc"):
            cell_html = _cell_html(cell, render_context)
            attrs = _cell_attrs(cell, render_context)
            tag = "th" if _first_attr(cell, "header") == "1" else "td"
            col_addr = _cell_addr(cell)
            width = _cell_size(cell, "width")
            col_span = _span_value(cell, "colSpan")
            if row_index == 0 and col_addr is not None and width and col_span == 1:
                col_widths[col_addr] = max(col_widths.get(col_addr, 0), width)
            cells.append(f"<{tag}{attrs}>{cell_html}</{tag}>")
        if cells:
            rows.append(f"<tr>{''.join(cells)}</tr>")

    if not rows:
        return ""

    table_style = _table_css(table)
    table_style_attr = f' style="{html.escape(table_style, quote=True)}"' if table_style else ""
    colgroup = _render_colgroup(col_widths)
    table_html = f"<table{table_style_attr}>{colgroup}<tbody>{''.join(rows)}</tbody></table>"
    if _has_ancestor(table, {"tbl"}):
        return table_html
    return f'<div class="table-wrap">{table_html}</div>'


def _cell_html(cell: Any, render_context: dict[str, Any]) -> str:
    containers = _direct_children_by_name(cell, "subList") or [cell]
    rendered: list[str] = []
    for container in containers:
        for child in getattr(container, "children", []):
            if not getattr(child, "name", None):
                continue
            name = _local_name(child)
            if name == "p":
                paragraph = _render_paragraph(child, render_context)
                if paragraph:
                    rendered.append(paragraph)
            elif name == "tbl":
                table = _render_table(child, render_context)
                if table:
                    rendered.append(table)
    if rendered:
        return "".join(rendered)
    return html.escape(_node_text(cell))


def _cell_attrs(cell: Any, render_context: dict[str, Any]) -> str:
    attrs: list[str] = _cell_span_attrs(cell)
    style = _cell_css(cell, render_context)
    if style:
        attrs.append(f'style="{html.escape(style, quote=True)}"')
    return "".join(f" {attr}" for attr in attrs)


def _cell_span_attrs(cell: Any) -> list[str]:
    attrs: list[str] = []
    for attr_name, html_name in (("colSpan", "colspan"), ("rowSpan", "rowspan")):
        value = _span_value(cell, attr_name)
        if value > 1:
            attrs.append(f'{html_name}="{value}"')
    return attrs


def _render_image(
    picture: Any,
    render_context: dict[str, Any],
) -> str:
    archive: zipfile.ZipFile = render_context["archive"]
    names: set[str] = render_context["names"]
    bin_data_refs: dict[str, str] = render_context["bin_refs"]
    image_ref = None
    for node in picture.find_all(True):
        image_ref = _first_attr(node, "binaryItemIDRef")
        if image_ref:
            break

    if not image_ref:
        return ""

    image_path = bin_data_refs.get(image_ref)
    if not image_path:
        return ""
    image_path = image_path.lstrip("/")
    if image_path not in names:
        return ""

    suffix = Path(image_path).suffix.lower()
    mime_type = _IMAGE_MIME_TYPES.get(suffix)
    if not mime_type:
        return ""

    image_bytes = archive.read(image_path)
    describe_image: ImageDescriber | None = render_context.get("describe_image")
    if describe_image is not None:
        try:
            described = describe_image(image_bytes, mime_type, image_ref)
        except Exception as exc:  # noqa: BLE001
            described = f"이미지 분석 실패: {exc}"
            return f'<div class="image-vlm error"><pre>{html.escape(described)}</pre></div>'
        return _render_vlm_image_preview(described)

    data = base64.b64encode(image_bytes).decode("ascii")
    style = _image_css(picture)
    style_attr = f' style="{html.escape(style, quote=True)}"' if style else ""
    return f'<img src="data:{mime_type};base64,{data}" alt=""{style_attr} />'


def _render_vlm_image_preview(result: str) -> str:
    result = (result or "").strip()
    if not result:
        return '<div class="image-vlm error"><pre>이미지 분석 결과가 비어 있습니다.</pre></div>'

    table_html = _extract_safe_table_html(result)
    text = _remove_table_blocks(result).strip()
    parts = ['<div class="image-vlm">']
    if table_html:
        parts.append(table_html)
    if text:
        parts.append(f"<pre>{html.escape(text)}</pre>")
    if not table_html and not text:
        parts.append("<pre>이미지 분석 결과가 비어 있습니다.</pre>")
    parts.append("</div>")
    return "".join(parts)


def _section_content_width(soup: BeautifulSoup) -> int:
    page_pr = soup.find(lambda tag: _local_name(tag) == "pagePr")
    if page_pr is None:
        return 0

    width = _int_attr(page_pr, "width", 0)
    height = _int_attr(page_pr, "height", 0)
    if width <= 0 or height <= 0:
        return 0

    page_width = max(width, height) if str(_first_attr(page_pr, "landscape") or "").upper() not in {"", "0", "FALSE"} else width
    return _hwpunit_to_px(page_width)


def _max_table_width(soup: BeautifulSoup) -> int:
    widths = []
    for table in soup.find_all(lambda tag: _local_name(tag) == "tbl"):
        width = _shape_size(table, "width")
        if width:
            widths.append(width)
    return max(widths, default=0)


def _extract_safe_table_html(text: str) -> str:
    match = re.search(r"<table\b[^>]*>.*?</table>", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""

    soup = BeautifulSoup(match.group(0), "html.parser")
    table = soup.find("table")
    if table is None:
        return ""

    allowed_tags = {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"}
    allowed_attrs = {"rowspan", "colspan", "scope"}
    for tag in list(table.find_all(True)):
        if tag.name not in allowed_tags:
            tag.unwrap()
            continue
        tag.attrs = {
            key: value
            for key, value in tag.attrs.items()
            if key.lower() in allowed_attrs
        }
    table.attrs = {}
    return str(table)


def _remove_table_blocks(text: str) -> str:
    text = re.sub(r"<table\b[^>]*>.*?</table>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"```(?:html)?\s*```", "", text, flags=re.IGNORECASE)
    return text


def _node_text(node: Any) -> str:
    return "".join(text_node.get_text() for text_node in node.find_all(lambda tag: _local_name(tag) == "t"))


def _table_css(table: Any) -> str:
    styles = []
    width = _shape_size(table, "width")
    if width:
        styles.append(f"width:{width}px")
    else:
        styles.append("width:100%")
    styles.append("border-collapse:collapse")
    styles.append("table-layout:fixed")
    return ";".join(styles)


def _render_colgroup(col_widths: dict[int, int]) -> str:
    if not col_widths:
        return ""
    parts = ["<colgroup>"]
    for col_index in sorted(col_widths):
        width = _hwpunit_to_px(col_widths[col_index])
        parts.append(f'<col style="width:{width}px" />')
    parts.append("</colgroup>")
    return "".join(parts)


def _cell_css(cell: Any, render_context: dict[str, Any]) -> str:
    styles: list[str] = []
    border_fill_id = _first_attr(cell, "borderFillIDRef")
    styles.extend(render_context["border_fills"].get(border_fill_id, []))

    width = _cell_size(cell, "width")
    height = _cell_size(cell, "height")
    if width:
        styles.append(f"width:{_hwpunit_to_px(width)}px")
    if height:
        styles.append(f"min-height:{_hwpunit_to_px(height)}px")

    margin = cell.find(lambda tag: _local_name(tag) == "cellMargin")
    if margin:
        top = _hwpunit_to_px(_int_attr(margin, "top", 0))
        right = _hwpunit_to_px(_int_attr(margin, "right", 0))
        bottom = _hwpunit_to_px(_int_attr(margin, "bottom", 0))
        left = _hwpunit_to_px(_int_attr(margin, "left", 0))
        styles.append(f"padding:{top}px {right}px {bottom}px {left}px")

    sub_list = cell.find(lambda tag: _local_name(tag) == "subList")
    vertical = (_first_attr(sub_list, "vertAlign") if sub_list else "") or ""
    if vertical:
        styles.append(f"vertical-align:{_css_vertical_align(vertical)}")

    if _first_attr(cell, "header") == "1":
        styles.append("font-weight:600")
    return ";".join(styles)


def _paragraph_css(paragraph: Any, render_context: dict[str, Any]) -> str:
    para_id = _first_attr(paragraph, "paraPrIDRef")
    return ";".join(render_context["para_prs"].get(para_id, []))


def _char_css(char_pr_id: str | None, render_context: dict[str, Any]) -> str:
    return ";".join(render_context["char_prs"].get(char_pr_id, []))


def _image_css(picture: Any) -> str:
    styles = ["max-width:100%", "height:auto"]
    width = _shape_size(picture, "width")
    height = _shape_size(picture, "height")
    if width:
        styles.append(f"width:{width}px")
    if height:
        styles.append(f"height:{height}px")
    return ";".join(styles)


def _parse_border_fill(node: Any) -> list[str]:
    styles: list[str] = []
    for local, css_name in (
        ("leftBorder", "border-left"),
        ("rightBorder", "border-right"),
        ("topBorder", "border-top"),
        ("bottomBorder", "border-bottom"),
    ):
        border = node.find(lambda tag: _local_name(tag) == local)
        if not border:
            continue
        border_type = (_first_attr(border, "type") or "SOLID").upper()
        width = _first_attr(border, "width") or "0.12 mm"
        color = _first_attr(border, "color") or "#000000"
        if border_type == "NONE":
            styles.append(f"{css_name}:0")
        else:
            styles.append(f"{css_name}:{width} solid {color}")

    brush = node.find(lambda tag: _local_name(tag) == "winBrush")
    face_color = _first_attr(brush, "faceColor") if brush else None
    if face_color and face_color.lower() != "none":
        styles.append(f"background-color:{face_color}")
    return styles


def _parse_char_pr(node: Any) -> list[str]:
    styles: list[str] = []
    height = _first_attr(node, "height")
    if height and height.isdigit():
        styles.append(f"font-size:{int(height) / 100:.2f}pt")
    color = _first_attr(node, "textColor")
    if color and color.lower() != "none":
        styles.append(f"color:{color}")
    if node.find(lambda tag: _local_name(tag) == "bold"):
        styles.append("font-weight:700")
    underline = node.find(lambda tag: _local_name(tag) == "underline")
    if underline and (_first_attr(underline, "type") or "NONE").upper() != "NONE":
        styles.append("text-decoration:underline")
    spacing = node.find(lambda tag: _local_name(tag) == "spacing")
    hangul_spacing = _first_attr(spacing, "hangul") if spacing else None
    if hangul_spacing and hangul_spacing.lstrip("-").isdigit():
        styles.append(f"letter-spacing:{int(hangul_spacing) / 100:.2f}em")
    return styles


def _parse_para_pr(node: Any) -> list[str]:
    styles: list[str] = []
    align = node.find(lambda tag: _local_name(tag) == "align")
    horizontal = (_first_attr(align, "horizontal") if align else "") or ""
    if horizontal:
        styles.append(f"text-align:{_css_text_align(horizontal)}")

    line_spacing = node.find(lambda tag: _local_name(tag) == "lineSpacing")
    line_value = _first_attr(line_spacing, "value") if line_spacing else None
    if line_value and line_value.isdigit():
        styles.append(f"line-height:{int(line_value) / 100:.2f}")

    margin = node.find(lambda tag: _local_name(tag) == "margin")
    if margin:
        left = _hwpunit_to_px(_int_child_attr(margin, "left", "value", 0))
        right = _hwpunit_to_px(_int_child_attr(margin, "right", "value", 0))
        prev = _hwpunit_to_px(_int_child_attr(margin, "prev", "value", 0))
        next_ = _hwpunit_to_px(_int_child_attr(margin, "next", "value", 0))
        styles.append(f"margin:{prev}px {right}px {next_}px {left}px")
    return styles


def _cell_addr(cell: Any) -> int | None:
    addr = _direct_child_by_name(cell, "cellAddr")
    value = _first_attr(addr, "colAddr") if addr else None
    return int(value) if value and value.isdigit() else None


def _cell_size(cell: Any, attr_name: str) -> int | None:
    size = _direct_child_by_name(cell, "cellSz")
    value = _first_attr(size, attr_name) if size else None
    return int(value) if value and value.isdigit() else None


def _span_value(cell: Any, attr_name: str) -> int:
    span = _direct_child_by_name(cell, "cellSpan")
    value = _first_attr(cell, attr_name) or _first_attr(span, attr_name)
    return int(value) if value and value.isdigit() else 1


def _shape_size(node: Any, attr_name: str) -> int | None:
    size = node.find(lambda tag: _local_name(tag) == "sz") or node.find(lambda tag: _local_name(tag) == "curSz")
    value = _first_attr(size, attr_name) if size else None
    if value and value.isdigit():
        return _hwpunit_to_px(int(value))
    return None


def _hwpunit_to_px(value: int) -> int:
    return max(0, round(value / _HWPUNIT_PER_INCH * _CSS_PX_PER_INCH))


def _css_text_align(value: str) -> str:
    mapping = {
        "CENTER": "center",
        "RIGHT": "right",
        "LEFT": "left",
        "JUSTIFY": "justify",
        "DISTRIBUTE": "justify",
    }
    return mapping.get(value.upper(), "left")


def _css_vertical_align(value: str) -> str:
    mapping = {
        "CENTER": "middle",
        "BOTTOM": "bottom",
        "TOP": "top",
    }
    return mapping.get(value.upper(), "top")


def _int_attr(node: Any, attr_name: str, default: int) -> int:
    value = _first_attr(node, attr_name)
    return int(value) if value and value.lstrip("-").isdigit() else default


def _int_child_attr(node: Any, child_name: str, attr_name: str, default: int) -> int:
    child = node.find(lambda tag: _local_name(tag) == child_name)
    return _int_attr(child, attr_name, default) if child else default


def _direct_children_by_name(node: Any, name: str) -> list[Any]:
    return [
        child
        for child in getattr(node, "children", [])
        if getattr(child, "name", None) and _local_name(child) == name
    ]


def _direct_child_by_name(node: Any, name: str) -> Any | None:
    for child in getattr(node, "children", []):
        if getattr(child, "name", None) and _local_name(child) == name:
            return child
    return None


def _local_name(node: Any) -> str:
    name = getattr(node, "name", "") or ""
    return name.rsplit(":", 1)[-1]


def _has_ancestor(node: Any, names: set[str]) -> bool:
    parent = getattr(node, "parent", None)
    while parent is not None:
        if getattr(parent, "name", None) and _local_name(parent) in names:
            return True
        parent = getattr(parent, "parent", None)
    return False


def _has_ancestor_between(node: Any, stop_node: Any, names: set[str]) -> bool:
    parent = getattr(node, "parent", None)
    while parent is not None and parent is not stop_node:
        if getattr(parent, "name", None) and _local_name(parent) in names:
            return True
        parent = getattr(parent, "parent", None)
    return False


def _first_attr(node: Any, attr_name: str) -> str | None:
    if node is None:
        return None
    if node.has_attr(attr_name):
        return str(node[attr_name])
    suffix = f":{attr_name}"
    for key, value in node.attrs.items():
        if key.endswith(suffix):
            return str(value)
    return None


def _first_descendant_attr(node: Any, attr_name: str) -> str | None:
    for descendant in node.find_all(True):
        value = _first_attr(descendant, attr_name)
        if value:
            return value
    return None
