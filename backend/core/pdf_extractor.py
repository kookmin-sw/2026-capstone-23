from pathlib import Path
from typing import Dict, List, Any, Tuple
import fitz  # PyMuPDF
import hashlib
import html as html_module
import io
import re
from PIL import Image, ImageEnhance, ImageFilter


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _extract_block_text_sorted(block: Dict[str, Any]) -> str:
    """모든 span을 y-그룹, x-origin 순으로 정렬하여 reading order 보장.

    PyMuPDF는 같은 시각적 행의 span들을 서로 다른 line 객체로 분리하는 경우가
    있으므로, block 내 전체 span을 모아 y 좌표 기준으로 행을 그룹화한 뒤
    각 행 안에서 x 좌표 순으로 정렬한다.
    """
    Y_TOLERANCE = 3  # px — 같은 행으로 묶을 y 차이 허용 범위

    all_spans = [
        span
        for line in block.get("lines", [])
        for span in line.get("spans", [])
    ]
    if not all_spans:
        return ""

    all_spans.sort(key=lambda s: s.get("origin", (0, 0))[1])
    rows: List[List[Any]] = []
    for span in all_spans:
        y = span.get("origin", (0, 0))[1]
        if rows and abs(y - rows[-1][-1].get("origin", (0, 0))[1]) <= Y_TOLERANCE:
            rows[-1].append(span)
        else:
            rows.append([span])

    text_content = ""
    for row in rows:
        row.sort(key=lambda s: s.get("origin", (0, 0))[0])
        text_content += "".join(s.get("text", "") for s in row) + "\n"
    return text_content


def _table_cells_to_html(tab) -> str:
    """fitz Table을 rowspan/colspan이 있는 HTML 표로 변환."""
    raw = tab.extract()
    if not raw:
        return ""
    n_raw_rows = len(raw)
    n_raw_cols = max(len(row) for row in raw) if raw else 0
    for row in raw:
        while len(row) < n_raw_cols:
            row.append(None)

    cell_rects = [tuple(cell) for cell in getattr(tab, "cells", []) or []]
    if not cell_rects:
        return ""

    x_lines = _cluster_positions(
        [coord for cell in cell_rects for coord in (cell[0], cell[2])],
        tolerance=2.0,
    )
    y_lines = _cluster_positions(
        [coord for cell in cell_rects for coord in (cell[1], cell[3])],
        tolerance=2.0,
    )
    if len(x_lines) < 2 or len(y_lines) < 2:
        return ""

    n_rows = len(y_lines) - 1
    n_cols = len(x_lines) - 1

    def nearest_index(lines: List[float], value: float) -> int:
        return min(range(len(lines)), key=lambda idx: abs(lines[idx] - value))

    anchors: Dict[Tuple[int, int], Dict[str, Any]] = {}
    covered: set[Tuple[int, int]] = set()

    for cell in cell_rects:
        x0, y0, x1, y1 = cell
        row_start = nearest_index(y_lines, y0)
        row_end = nearest_index(y_lines, y1)
        col_start = nearest_index(x_lines, x0)
        col_end = nearest_index(x_lines, x1)
        if row_end <= row_start or col_end <= col_start:
            continue
        if row_start >= n_rows or col_start >= n_cols:
            continue

        rowspan = max(1, min(row_end, n_rows) - row_start)
        colspan = max(1, min(col_end, n_cols) - col_start)
        text = ""
        if row_start < n_raw_rows and col_start < len(raw[row_start]):
            text = raw[row_start][col_start] or ""

        anchors[(row_start, col_start)] = {
            "text": text,
            "rowspan": rowspan,
            "colspan": colspan,
        }
        for dr in range(rowspan):
            for dc in range(colspan):
                if dr or dc:
                    covered.add((row_start + dr, col_start + dc))

    html_rows = []

    for r in range(n_rows):
        html_cells = []
        for c in range(n_cols):
            if (r, c) in covered:
                continue
            cell = anchors.get((r, c))
            if cell is None:
                continue
            text = cell["text"]
            rowspan = cell["rowspan"]
            colspan = cell["colspan"]

            tag = "th" if r == 0 else "td"
            attrs = ""
            if colspan > 1:
                attrs += f' colspan="{colspan}"'
            if rowspan > 1:
                attrs += f' rowspan="{rowspan}"'

            paragraphs = [html_module.escape(line.strip()) for line in text.split("\n") if line.strip()]
            cell_content = "".join(f"<p>{p}</p>" for p in paragraphs)
            html_cells.append(f"    <{tag}{attrs}>{cell_content}</{tag}>")

        html_rows.append("  <tr>\n" + "\n".join(html_cells) + "\n  </tr>")

    return "<table>\n" + "\n".join(html_rows) + "\n</table>"


def _table_cells_to_meta(tab) -> List[Dict[str, Any]]:
    """표 HTML은 건드리지 않고, 셀 내부 이미지 귀속용 bbox 메타만 만든다."""
    raw = tab.extract()
    cell_rects = [tuple(cell) for cell in getattr(tab, "cells", []) or []]
    if not raw or not cell_rects:
        return []

    x_lines = _cluster_positions(
        [coord for cell in cell_rects for coord in (cell[0], cell[2])],
        tolerance=2.0,
    )
    y_lines = _cluster_positions(
        [coord for cell in cell_rects for coord in (cell[1], cell[3])],
        tolerance=2.0,
    )
    if len(x_lines) < 2 or len(y_lines) < 2:
        return []

    def nearest_index(lines: List[float], value: float) -> int:
        return min(range(len(lines)), key=lambda idx: abs(lines[idx] - value))

    metas: List[Dict[str, Any]] = []
    for cell in cell_rects:
        x0, y0, x1, y1 = cell
        row = nearest_index(y_lines, y0)
        col = nearest_index(x_lines, x0)
        row_end = nearest_index(y_lines, y1)
        col_end = nearest_index(x_lines, x1)
        if row_end <= row or col_end <= col:
            continue

        text = ""
        if row < len(raw) and col < len(raw[row]):
            text = raw[row][col] or ""

        metas.append({
            "row": row,
            "col": col,
            "rowspan": max(1, row_end - row),
            "colspan": max(1, col_end - col),
            "bbox": [x0, y0, x1, y1],
            "text": text.strip(),
        })
    return metas


def _is_native_table_reliable(tab) -> bool:
    """find_tables()로 추출한 표가 신뢰 가능한지 판단."""
    if tab.col_count < 2 or tab.row_count < 2:
        return False
    raw = tab.extract()
    if not raw:
        return False
    cells_with_text = sum(1 for row in raw for c in row if c and c.strip())
    total = tab.row_count * tab.col_count
    return total > 0 and cells_with_text / total >= 0.3


def _cluster_positions(values: List[float], tolerance: float = 3.0) -> List[float]:
    if not values:
        return []
    clusters: List[List[float]] = []
    for value in sorted(values):
        if not clusters or abs(value - (sum(clusters[-1]) / len(clusters[-1]))) > tolerance:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _extract_rule_segments(page) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []

    def add_segment(x0: float, y0: float, x1: float, y1: float) -> None:
        if abs(y1 - y0) <= 1.5 and abs(x1 - x0) >= 8.0:
            sx0, sx1 = sorted((float(x0), float(x1)))
            y = (float(y0) + float(y1)) / 2
            segments.append({"orientation": "h", "bbox": (sx0, y, sx1, y), "pos": y})
        elif abs(x1 - x0) <= 1.5 and abs(y1 - y0) >= 8.0:
            sy0, sy1 = sorted((float(y0), float(y1)))
            x = (float(x0) + float(x1)) / 2
            segments.append({"orientation": "v", "bbox": (x, sy0, x, sy1), "pos": x})

    try:
        drawings = page.get_drawings()
    except Exception:
        return []

    for drawing in drawings:
        for item in drawing.get("items", []):
            kind = item[0] if item else None
            if kind == "l" and len(item) >= 3:
                p0, p1 = item[1], item[2]
                add_segment(p0.x, p0.y, p1.x, p1.y)
            elif kind == "re" and len(item) >= 2:
                rect = item[1]
                add_segment(rect.x0, rect.y0, rect.x1, rect.y0)
                add_segment(rect.x0, rect.y1, rect.x1, rect.y1)
                add_segment(rect.x0, rect.y0, rect.x0, rect.y1)
                add_segment(rect.x1, rect.y0, rect.x1, rect.y1)
    return segments


def _block_center(block: Dict[str, Any]) -> Tuple[float, float]:
    x0, y0, x1, y1 = block.get("bbox", [0, 0, 0, 0])
    return ((x0 + x1) / 2, (y0 + y1) / 2)


def _bbox_contains(outer, inner, tolerance: float = 4.0) -> bool:
    ox0, oy0, ox1, oy1 = outer
    ix0, iy0, ix1, iy1 = inner
    return (
        ix0 >= ox0 - tolerance
        and iy0 >= oy0 - tolerance
        and ix1 <= ox1 + tolerance
        and iy1 <= oy1 + tolerance
    )


def _table_text_tokens(table_html: str) -> set[str]:
    text = re.sub(r"<[^>]+>", " ", table_html or "")
    text = html_module.unescape(text).replace("\xa0", " ")
    return {token for token in re.findall(r"[0-9A-Za-z가-힣]+", text) if len(token) >= 2}


def _table_text_coverage(parent_html: str, child_html: str) -> float:
    child_tokens = _table_text_tokens(child_html)
    if not child_tokens:
        return 0.0
    parent_tokens = _table_text_tokens(parent_html)
    return len(child_tokens & parent_tokens) / len(child_tokens)


def _remove_contained_native_table_duplicates(
    native_tables: List[Dict[str, Any]],
    page_no: int,
) -> List[Dict[str, Any]]:
    """외곽 표 셀 안에 이미 포함된 내부 native 표는 standalone 결과에서 제거한다."""
    if len(native_tables) < 2:
        return native_tables

    drop_indexes: set[int] = set()
    for child_idx, child in enumerate(native_tables):
        child_bbox = child.get("bbox", [0, 0, 0, 0])
        child_area = _bbox_area(child_bbox)
        if child_area <= 0:
            continue
        for parent_idx, parent in enumerate(native_tables):
            if parent_idx == child_idx:
                continue
            parent_bbox = parent.get("bbox", [0, 0, 0, 0])
            parent_area = _bbox_area(parent_bbox)
            if parent_area <= child_area * 1.35:
                continue
            if not _bbox_contains(parent_bbox, child_bbox, tolerance=8.0):
                continue
            if _table_text_coverage(parent.get("html", ""), child.get("html", "")) < 0.60:
                continue
            drop_indexes.add(child_idx)
            break

    if not drop_indexes:
        return native_tables

    print(f"[INFO] Page {page_no}: 표 내부 standalone native 표 {len(drop_indexes)}개 제거")
    return [table for idx, table in enumerate(native_tables) if idx not in drop_indexes]


def _html_from_text_block(block: Dict[str, Any]) -> str:
    parts = []
    content = str(block.get("content") or "").strip()
    for line in content.splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            parts.append(f"<p>{html_module.escape(cleaned)}</p>")
    return "".join(parts)


def _detect_single_column_container_tables(
    page,
    text_block_elements: List[Dict[str, Any]],
    nested_table_candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """단일 컬럼 외곽 표/폼을 HTML 표로 직접 복원한다."""
    segments = _extract_rule_segments(page)
    if not segments:
        return []

    horizontal = [s for s in segments if s["orientation"] == "h"]
    vertical = [s for s in segments if s["orientation"] == "v"]
    x_lines = _cluster_positions([s["pos"] for s in vertical])
    y_lines = _cluster_positions([s["pos"] for s in horizontal])
    if len(x_lines) < 2 or len(y_lines) < 3:
        return []

    page_rect = page.rect
    candidates: List[Dict[str, Any]] = []
    for left_idx in range(len(x_lines) - 1):
        for right_idx in range(left_idx + 1, len(x_lines)):
            x0 = x_lines[left_idx]
            x1 = x_lines[right_idx]
            width_ratio = (x1 - x0) / max(float(page_rect.width), 1.0)
            if width_ratio < 0.70:
                continue

            span_y_lines = []
            for y in y_lines:
                has_covering_hline = any(
                    s["orientation"] == "h"
                    and abs(s["pos"] - y) <= 3.0
                    and min(s["bbox"][0], s["bbox"][2]) <= x0 + 6.0
                    and max(s["bbox"][0], s["bbox"][2]) >= x1 - 6.0
                    for s in horizontal
                )
                if has_covering_hline:
                    span_y_lines.append(y)

            if len(span_y_lines) < 3:
                continue

            y0 = min(span_y_lines)
            y1 = max(span_y_lines)
            height_ratio = (y1 - y0) / max(float(page_rect.height), 1.0)
            if height_ratio < 0.30:
                continue

            has_left_vline = any(
                s["orientation"] == "v"
                and abs(s["pos"] - x0) <= 3.0
                and min(s["bbox"][1], s["bbox"][3]) <= y0 + 6.0
                and max(s["bbox"][1], s["bbox"][3]) >= y1 - 6.0
                for s in vertical
            )
            has_right_vline = any(
                s["orientation"] == "v"
                and abs(s["pos"] - x1) <= 3.0
                and min(s["bbox"][1], s["bbox"][3]) <= y0 + 6.0
                and max(s["bbox"][1], s["bbox"][3]) >= y1 - 6.0
                for s in vertical
            )
            if not (has_left_vline and has_right_vline):
                continue

            has_full_height_inner_vline = any(
                s["orientation"] == "v"
                and x0 + 6.0 < s["pos"] < x1 - 6.0
                and min(s["bbox"][1], s["bbox"][3]) <= y0 + 6.0
                and max(s["bbox"][1], s["bbox"][3]) >= y1 - 6.0
                for s in vertical
            )
            if has_full_height_inner_vline:
                continue

            container_bbox = [
                max(0.0, x0 - 4.0),
                max(0.0, y0 - 4.0),
                min(float(page_rect.width), x1 + 4.0),
                min(float(page_rect.height), y1 + 4.0),
            ]
            cell_parts = []
            for row_start, row_end in zip(span_y_lines, span_y_lines[1:]):
                cell_bbox = [x0, row_start, x1, row_end]
                row_text_blocks = [
                    block
                    for block in text_block_elements
                    if cell_bbox[0] <= _block_center(block)[0] <= cell_bbox[2]
                    and cell_bbox[1] <= _block_center(block)[1] <= cell_bbox[3]
                ]
                nested_tables = [
                    table
                    for table in nested_table_candidates
                    if table.get("bbox") and _bbox_contains(cell_bbox, table["bbox"], tolerance=8.0)
                ]
                # 네이티브 중첩 표 내부 텍스트는 외곽 셀의 일반 문단에서 제외한다.
                for table in nested_tables:
                    row_text_blocks = [
                        block
                        for block in row_text_blocks
                        if not _bbox_contains(table["bbox"], block.get("bbox", [0, 0, 0, 0]), tolerance=2.0)
                    ]

                content_items = []
                for block in row_text_blocks:
                    item_bbox = block.get("bbox", [0, 0, 0, 0])
                    html = _html_from_text_block(block)
                    if html:
                        content_items.append((item_bbox[1], item_bbox[0], html))
                for table in nested_tables:
                    item_bbox = table.get("bbox", [0, 0, 0, 0])
                    table_html = str(table.get("html") or "")
                    if table_html:
                        content_items.append((item_bbox[1], item_bbox[0], table_html))
                content = "".join(item[2] for item in sorted(content_items, key=lambda item: (item[0], item[1])))
                cell_parts.append(f"  <tr>\n    <td>{content}</td>\n  </tr>")

            if not any("<p>" in row or "<table" in row.lower() for row in cell_parts):
                continue

            html = "<table>\n" + "\n".join(cell_parts) + "\n</table>"
            candidates.append({
                "type": "native_table",
                "bbox": container_bbox,
                "html": html,
                "image_id": f"page-{page.number+1}-container-table-{len(candidates)+1}",
                "page": page.number + 1,
                "is_container_table": True,
            })

    # 가장 큰 컨테이너만 사용한다. 중첩/중복 외곽선을 여러 번 잡는 것을 막는다.
    if not candidates:
        return []
    candidates.sort(key=lambda item: _bbox_area(item["bbox"]), reverse=True)
    return [candidates[0]]


def _bbox_overlaps_any(bbox, bboxes, threshold: float = 0.7) -> bool:
    """bbox가 bboxes 중 하나와 threshold 비율 이상 겹치는지 확인."""
    ax0, ay0, ax1, ay1 = bbox
    a_area = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    if a_area <= 0:
        return False
    for bx0, by0, bx1, by1 in bboxes:
        overlap_x = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        overlap_y = max(0.0, min(ay1, by1) - max(ay0, by0))
        if (overlap_x * overlap_y) / a_area >= threshold:
            return True
    return False


def _is_oversized_table_crop_for_native_tables(
    table_bbox,
    native_table_bboxes,
    page_width: float,
    page_height: float,
) -> bool:
    """네이티브 표를 포함한 과대 table crop 후보인지 판단."""
    if not native_table_bboxes:
        return False

    tx0, ty0, tx1, ty1 = table_bbox
    table_area = max(0.0, tx1 - tx0) * max(0.0, ty1 - ty0)
    page_area = max(0.0, page_width) * max(0.0, page_height)
    if table_area <= 0 or page_area <= 0:
        return False

    covers_native = False
    for nx0, ny0, nx1, ny1 in native_table_bboxes:
        native_area = max(0.0, nx1 - nx0) * max(0.0, ny1 - ny0)
        if native_area <= 0:
            continue
        overlap_x = max(0.0, min(tx1, nx1) - max(tx0, nx0))
        overlap_y = max(0.0, min(ty1, ny1) - max(ty0, ny0))
        native_covered = (overlap_x * overlap_y) / native_area
        if native_covered >= 0.85:
            covers_native = True
            break

    if not covers_native:
        return False

    table_page_ratio = table_area / page_area
    table_width_ratio = max(0.0, tx1 - tx0) / max(page_width, 1)
    table_height_ratio = max(0.0, ty1 - ty0) / max(page_height, 1)
    return table_page_ratio >= 0.50 or (table_width_ratio >= 0.75 and table_height_ratio >= 0.58)


def _is_table_crop_covered_by_native_tables(
    table_bbox,
    native_table_bboxes,
    native_cover_threshold: float = 0.85,
    aggregate_cover_threshold: float = 0.55,
) -> bool:
    """여러 native 표가 함께 덮는 table crop 중복 후보인지 판단."""
    if not native_table_bboxes:
        return False

    table_area = _bbox_area(table_bbox)
    if table_area <= 0:
        return False

    covered_area = 0.0
    covered_count = 0
    for native_bbox in native_table_bboxes:
        native_area = _bbox_area(native_bbox)
        if native_area <= 0:
            continue
        intersection = _bbox_intersection_area(table_bbox, native_bbox)
        if intersection / native_area < native_cover_threshold:
            continue
        covered_area += intersection
        covered_count += 1

    if covered_count == 0:
        return False

    aggregate_cover = covered_area / table_area
    if covered_count >= 2 and aggregate_cover >= aggregate_cover_threshold:
        return True
    return aggregate_cover >= 0.70


def _sanitize_text(text: str) -> str:
    """null(0x00) 및 HWP 채움 문자(PUA) 제거 - NUL 표시 오류 방지"""
    if not text:
        return text
    s = text.replace('\x00', ' ')
    s = ''.join(' ' if (0xE000 <= ord(c) <= 0xF8FF or 0x0F0000 <= ord(c) <= 0x0FFFFF) else c for c in s)
    lines = [' '.join(line.split()) if line.strip() else line for line in s.split('\n')]
    return '\n'.join(lines).strip()


def _sort_by_reading_order(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    bbox 기반 reading order 결정 (top-to-bottom, left-to-right)
    """
    def get_sort_key(elem: Dict[str, Any]) -> Tuple[float, float]:
        bbox = elem.get('bbox', [0, 0, 0, 0])
        # y0 (상단)으로 먼저 정렬, 그 다음 x0 (좌측)으로 정렬
        # 같은 줄로 간주: y0 차이가 10pt 이하
        y0 = bbox[1]
        x0 = bbox[0]
        # y를 10pt 단위로 그룹화
        y_group = int(y0 / 10)
        return (y_group, x0)
    
    return sorted(elements, key=get_sort_key)


def _bbox_area(bbox: Tuple[float, float, float, float] | List[float]) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _bbox_intersection_area(
    a: Tuple[float, float, float, float] | List[float],
    b: Tuple[float, float, float, float] | List[float],
) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    overlap_x = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    overlap_y = max(0.0, min(ay1, by1) - max(ay0, by0))
    return overlap_x * overlap_y


def _bbox_overlap_ratio(a, b) -> float:
    area = _bbox_area(a)
    if area <= 0:
        return 0.0
    return _bbox_intersection_area(a, b) / area


def _color_is_near_white(color) -> bool:
    if color is None:
        return False
    return all(float(channel) >= 0.94 for channel in color[:3])


def _drawing_bbox(drawing) -> List[float]:
    rect = drawing.get("rect")
    if not rect:
        return [0, 0, 0, 0]
    return [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)]


def _is_meaningful_cell_drawing(drawing) -> bool:
    """표 선/흰 배경은 버리고, 셀 내부 차트/플로우를 구성하는 벡터만 남긴다."""
    items = drawing.get("items", []) or []
    item_types = [item[0] for item in items if item]
    if not item_types:
        return False

    fill = drawing.get("fill")
    stroke = drawing.get("color")
    width = float(drawing.get("width") or 0)

    if fill is not None and _color_is_near_white(fill) and stroke is None:
        return False

    # 표 테두리는 대부분 얇은 단일 수평/수직 선이다. 화살표/折線은 items가 여러 개라 살린다.
    if fill is None and len(item_types) == 1 and item_types[0] == "l" and width <= 1.2:
        return False

    if fill is not None and not _color_is_near_white(fill):
        return True
    if stroke is not None and len(item_types) > 1:
        return True
    if width > 1.2 and len(item_types) > 1:
        return True
    return False


def _render_page_clip(page, bbox: List[float], scale: float = 2.0, padding: float = 4.0) -> bytes:
    page_rect = page.rect
    clip = fitz.Rect(
        max(float(page_rect.x0), bbox[0] - padding),
        max(float(page_rect.y0), bbox[1] - padding),
        min(float(page_rect.x1), bbox[2] + padding),
        min(float(page_rect.y1), bbox[3] + padding),
    )
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = img.filter(ImageFilter.SHARPEN)
    img_buffer = io.BytesIO()
    img.save(img_buffer, format="PNG")
    return img_buffer.getvalue()


def _looks_like_table_caption(text: str) -> bool:
    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return False
    caption_patterns = (
        r"^표\s*\d+[\.\-:)]",
        r"^table\s*\d+[\.\-:)]",
        r"^\[?\s*table\s*\d+[\.\-:)]",
    )
    return any(re.match(pattern, normalized, re.IGNORECASE) for pattern in caption_patterns)


def _is_reliable_table_region_for_text_removal(
    table_bbox: Tuple[float, float, float, float] | List[float],
    page_width: float,
    page_height: float,
) -> bool:
    page_area = max(0.0, page_width) * max(0.0, page_height)
    if page_area <= 0:
        return False

    tx0, ty0, tx1, ty1 = table_bbox
    table_width = max(0.0, tx1 - tx0)
    table_height = max(0.0, ty1 - ty0)
    # 거의 전체 페이지를 덮는 경우만 오검출로 간주 (2단 본문/문서 본문 방어)
    # 60% 면적 조건은 진짜 큰 표도 걸러내므로 제거; 가로+세로 동시 조건만 유지
    if table_width / max(page_width, 1) > 0.85 and table_height / max(page_height, 1) > 0.55:
        return False

    return True


def _should_remove_text_for_table_region(
    text_bbox: Tuple[float, float, float, float] | List[float],
    table_bbox: Tuple[float, float, float, float] | List[float],
    page_width: float,
    page_height: float,
    text: str = "",
) -> bool:
    """표 VLM 결과와 중복되는 표 내부 텍스트만 제거한다."""
    if _looks_like_table_caption(text):
        return False
    if not _is_reliable_table_region_for_text_removal(table_bbox, page_width, page_height):
        return False

    text_area = _bbox_area(text_bbox)
    if text_area <= 0:
        return False

    tx0, ty0, tx1, ty1 = table_bbox
    bx0, by0, bx1, by1 = text_bbox
    center_x = (bx0 + bx1) / 2
    center_y = (by0 + by1) / 2
    center_inside = tx0 <= center_x <= tx1 and ty0 <= center_y <= ty1
    if not center_inside:
        return False

    overlap_ratio = _bbox_intersection_area(text_bbox, table_bbox) / text_area
    return overlap_ratio >= 0.85


def _table_crop_scale(table_bbox, page_width: float, page_height: float) -> float:
    """표 crop의 크기와 페이지 비율에 따라 렌더링 배율을 조정한다."""
    x0, y0, x1, y1 = table_bbox
    width = max(0.0, float(x1) - float(x0))
    height = max(0.0, float(y1) - float(y0))
    area = width * height
    page_area = max(page_width * page_height, 1.0)
    area_ratio = area / page_area
    longest_side = max(width, height)

    if area_ratio < 0.18 or longest_side < 420:
        return 3.0
    if area_ratio < 0.35 or longest_side < 700:
        return 2.5
    return 2.0


def _extract_header_names(html: str) -> List[str]:
    """HTML 표의 첫 번째 헤더 행 <th> 텍스트를 정규화하여 반환."""
    first_row_m = re.search(r'<tr>(.*?)</tr>', html, re.DOTALL)
    if not first_row_m or '<th' not in first_row_m.group(1):
        return []
    th_contents = re.findall(r'<th[^>]*>(.*?)</th>', first_row_m.group(1), re.DOTALL)
    names = []
    for content in th_contents:
        text = ' '.join(re.sub(r'<[^>]+>', '', content).split())
        if text:
            names.append(text)
    return names


def _table_rows(html: str) -> List[str]:
    return re.findall(r'[ \t]*<tr>.*?[ \t]*</tr>', html or "", re.DOTALL)


def _table_row_count(html: str) -> int:
    return len(_table_rows(html))


def _normalize_data_row_tags(row_html: str) -> str:
    row_html = re.sub(r"<th\b", "<td", row_html, flags=re.IGNORECASE)
    row_html = re.sub(r"</th>", "</td>", row_html, flags=re.IGNORECASE)
    return row_html


_HTML_CELL_PATTERN = re.compile(r"<(t[dh])([^>]*)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)


def _cell_text(cell_inner: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", cell_inner or "").split())


def _header_spans(html: str) -> List[Tuple[str, int]]:
    rows = _table_rows(html)
    if not rows:
        return []
    spans: List[Tuple[str, int]] = []
    for cell_m in _HTML_CELL_PATTERN.finditer(rows[0]):
        attrs = cell_m.group(2)
        colspan_m = re.search(r'colspan="(\d+)"', attrs)
        colspan = int(colspan_m.group(1)) if colspan_m else 1
        spans.append((_cell_text(cell_m.group(3)), colspan))
    return spans


def _add_colspan_to_cell(row_html: str, cell_index: int, extra_span: int) -> str:
    if extra_span <= 0:
        return row_html

    cells = list(_HTML_CELL_PATTERN.finditer(row_html))
    if cell_index < 0 or cell_index >= len(cells):
        return row_html

    cell_m = cells[cell_index]
    tag = cell_m.group(1)
    attrs = cell_m.group(2)
    inner = cell_m.group(3)
    colspan_m = re.search(r'colspan="(\d+)"', attrs)
    if colspan_m:
        new_attrs = re.sub(
            r'colspan="(\d+)"',
            lambda m: f'colspan="{int(m.group(1)) + extra_span}"',
            attrs,
            count=1,
        )
    else:
        new_attrs = attrs + f' colspan="{extra_span + 1}"'
    new_cell = f"<{tag}{new_attrs}>{inner}</{tag}>"
    return row_html[:cell_m.start()] + new_cell + row_html[cell_m.end():]


def _expand_base_table_to_continuation_columns(base_html: str, continuation_html: str) -> str:
    """반복 헤더에서 continuation이 더 세밀한 컬럼을 잡은 경우 base 행을 같은 grid로 맞춘다."""
    base_header = _header_spans(base_html)
    cont_header = _header_spans(continuation_html)
    if not base_header or len(base_header) != len(cont_header):
        return base_html
    if [name for name, _ in base_header] != [name for name, _ in cont_header]:
        return base_html

    expanded_indices = [
        idx
        for idx, ((_, base_span), (_, cont_span)) in enumerate(zip(base_header, cont_header))
        if cont_span > base_span
    ]
    if len(expanded_indices) != 1:
        return base_html

    target_idx = expanded_indices[0]
    extra_span = cont_header[target_idx][1] - base_header[target_idx][1]
    if extra_span <= 0:
        return base_html

    base_cols = sum(span for _, span in base_header)
    rows = _table_rows(base_html)
    new_rows: List[str] = []
    for row_idx, row in enumerate(rows):
        cells = list(_HTML_CELL_PATTERN.finditer(row))
        if not cells:
            new_rows.append(row)
            continue

        row_cols = 0
        for cell_m in cells:
            colspan_m = re.search(r'colspan="(\d+)"', cell_m.group(2))
            row_cols += int(colspan_m.group(1)) if colspan_m else 1

        if row_idx == 0 or row_cols >= base_cols:
            cell_index = target_idx
        elif row_cols < base_cols:
            # 앞/뒤 컬럼이 rowspan으로 생략된 행은 target 컬럼이 직접 셀 목록의
            # 첫 쪽으로 당겨져 나타난다.
            cell_index = max(0, target_idx - 1)
        else:
            new_rows.append(row)
            continue
        new_rows.append(_add_colspan_to_cell(row, cell_index, extra_span))

    result = base_html
    for old_row, new_row in zip(rows, new_rows):
        if old_row != new_row:
            result = result.replace(old_row, new_row, 1)
    return result


def _merge_table_html(base_html: str, continuation_html: str, drop_repeated_header: bool = False) -> str:
    """continuation 표의 헤더 행을 제거하고 데이터 행을 base 표 끝에 추가."""
    cont_rows = _table_rows(continuation_html)
    data_rows = cont_rows[1:] if drop_repeated_header and cont_rows else cont_rows
    data_rows = [_normalize_data_row_tags(row) for row in data_rows]
    if not data_rows:
        return base_html
    extra = '\n' + '\n'.join(data_rows)
    return re.sub(r'\n</table>\s*$', extra + '\n</table>', base_html.rstrip())


def _extend_junction_first_cell_rowspan(
    merged_html: str,
    junction_row_index: int,
    additional_rows: int,
) -> str:
    if additional_rows <= 0:
        return merged_html

    rows = _table_rows(merged_html)
    if junction_row_index < 0 or junction_row_index >= len(rows):
        return merged_html

    row = rows[junction_row_index]
    cell_m = re.search(r"<td([^>]*)>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
    if cell_m is None:
        return merged_html

    attrs = cell_m.group(1)
    content = cell_m.group(2)
    rowspan_m = re.search(r'rowspan="(\d+)"', attrs)
    if rowspan_m:
        new_attrs = re.sub(
            r'rowspan="(\d+)"',
            lambda m: f'rowspan="{int(m.group(1)) + additional_rows}"',
            attrs,
            count=1,
        )
    else:
        new_attrs = attrs + f' rowspan="{additional_rows + 1}"'

    new_cell = f"<td{new_attrs}>{content}</td>"
    new_row = row[:cell_m.start()] + new_cell + row[cell_m.end():]
    return merged_html.replace(row, new_row, 1)


def _html_column_count(html: str) -> int:
    first_row_m = re.search(r'<tr>(.*?)</tr>', html or "", re.DOTALL)
    if not first_row_m:
        return 0
    count = 0
    for attrs in re.findall(r'<t[dh]([^>]*)>', first_row_m.group(1), re.IGNORECASE):
        colspan_m = re.search(r'colspan="(\d+)"', attrs)
        count += int(colspan_m.group(1)) if colspan_m else 1
    return count


def _remove_empty_table_rows(html: str) -> Tuple[str, int]:
    rows = _table_rows(html)
    removed = 0
    result = html
    for row in rows:
        if _HTML_CELL_PATTERN.search(row) is not None:
            continue
        result = result.replace(row, "", 1)
        removed += 1
    return result, removed


def _table_grid_diagnostics(html: str) -> Dict[str, Any]:
    rows = _table_rows(html)
    occupied_by_row: Dict[int, set[int]] = {}
    anchors: List[Tuple[int, int, int, int, str]] = []
    blocked: set[Tuple[int, int]] = set()
    max_col = -1

    for row_idx, row in enumerate(rows):
        col_idx = 0
        for cell_m in _HTML_CELL_PATTERN.finditer(row):
            while (row_idx, col_idx) in blocked:
                col_idx += 1

            attrs = cell_m.group(2)
            rowspan_m = re.search(r'rowspan="(\d+)"', attrs)
            colspan_m = re.search(r'colspan="(\d+)"', attrs)
            rowspan = max(1, int(rowspan_m.group(1))) if rowspan_m else 1
            colspan = max(1, int(colspan_m.group(1))) if colspan_m else 1
            tag = cell_m.group(1).lower()
            anchors.append((row_idx, col_idx, rowspan, colspan, tag))

            for dr in range(rowspan):
                rr = row_idx + dr
                for dc in range(colspan):
                    cc = col_idx + dc
                    occupied_by_row.setdefault(rr, set()).add(cc)
                    blocked.add((rr, cc))
                    max_col = max(max_col, cc)
            col_idx += colspan

    col_count = max_col + 1 if max_col >= 0 else 0
    row_widths = [
        len(occupied_by_row.get(row_idx, set()))
        for row_idx in range(len(rows))
    ]
    incomplete_rows = [
        (row_idx, width)
        for row_idx, width in enumerate(row_widths)
        if col_count and width != col_count
    ]
    overflowing_rowspans = [
        (row_idx, col_idx, rowspan, colspan)
        for row_idx, col_idx, rowspan, colspan, _tag in anchors
        if row_idx + rowspan > len(rows)
    ]

    return {
        "rows": len(rows),
        "cols": col_count,
        "row_widths": row_widths,
        "incomplete_rows": incomplete_rows,
        "overflowing_rowspans": overflowing_rowspans,
    }


def _cleanup_merged_table_html(html: str, label: str = "") -> Tuple[str, Dict[str, Any]]:
    cleaned, removed_empty_rows = _remove_empty_table_rows(html)
    diagnostics = _table_grid_diagnostics(cleaned)
    diagnostics["removed_empty_rows"] = removed_empty_rows

    if removed_empty_rows:
        suffix = f" ({label})" if label else ""
        print(f"[INFO] 병합 표 빈 행 제거{suffix}: {removed_empty_rows}개")

    incomplete = diagnostics.get("incomplete_rows") or []
    overflowing = diagnostics.get("overflowing_rowspans") or []
    if incomplete or overflowing:
        suffix = f" ({label})" if label else ""
        preview = ", ".join(
            f"r{row_idx + 1}:{width}/{diagnostics.get('cols', 0)}"
            for row_idx, width in incomplete[:5]
        )
        if len(incomplete) > 5:
            preview += f", +{len(incomplete) - 5}"
        print(
            f"[WARNING] 병합 표 grid 진단{suffix}: "
            f"rows={diagnostics.get('rows')} cols={diagnostics.get('cols')} "
            f"incomplete=[{preview}] overflowing_rowspans={len(overflowing)}"
        )

    return cleaned, diagnostics


def _table_continuation_score(
    cur_table: Dict[str, Any],
    nxt_table: Dict[str, Any],
    cur_page: Dict[str, Any],
    nxt_page: Dict[str, Any],
) -> Tuple[int, List[str]]:
    cur_bbox = cur_table.get("bbox") or [0, 0, 0, 0]
    nxt_bbox = nxt_table.get("bbox") or [0, 0, 0, 0]
    cur_width = max(0.0, cur_bbox[2] - cur_bbox[0])
    nxt_width = max(0.0, nxt_bbox[2] - nxt_bbox[0])
    max_width = max(cur_width, nxt_width, 1.0)
    cur_page_height = max(float(cur_page.get("height") or 0), 1.0)
    nxt_page_height = max(float(nxt_page.get("height") or 0), 1.0)

    score = 0
    reasons: List[str] = []

    cur_cols = _html_column_count(cur_table.get("html", ""))
    nxt_cols = _html_column_count(nxt_table.get("html", ""))
    if cur_cols and cur_cols == nxt_cols:
        score += 2
        reasons.append(f"cols={cur_cols}")

    width_ratio = min(cur_width, nxt_width) / max_width
    if width_ratio >= 0.82:
        score += 2
        reasons.append(f"width={width_ratio:.2f}")

    x0_close = abs(cur_bbox[0] - nxt_bbox[0]) <= max_width * 0.18
    x1_close = abs(cur_bbox[2] - nxt_bbox[2]) <= max_width * 0.18
    if x0_close and x1_close:
        score += 1
        reasons.append("x-aligned")

    if cur_bbox[3] >= cur_page_height * 0.72:
        score += 2
        reasons.append("cur-bottom")
    if nxt_bbox[1] <= nxt_page_height * 0.28:
        score += 2
        reasons.append("next-top")

    cur_headers = _extract_header_names(cur_table.get("html", ""))
    nxt_headers = _extract_header_names(nxt_table.get("html", ""))
    if cur_headers and cur_headers == nxt_headers:
        score += 3
        reasons.append("same-header")
    elif cur_cols and cur_cols == nxt_cols and not nxt_headers:
        score += 1
        reasons.append("next-no-header")

    return score, reasons


def _should_merge_split_table(
    cur_table: Dict[str, Any],
    nxt_table: Dict[str, Any],
    cur_page: Dict[str, Any],
    nxt_page: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    cur_bbox = cur_table.get("bbox") or [0, 0, 0, 0]
    nxt_bbox = nxt_table.get("bbox") or [0, 0, 0, 0]
    cur_page_height = max(float(cur_page.get("height") or 0), 1.0)
    nxt_page_height = max(float(nxt_page.get("height") or 0), 1.0)
    if cur_bbox[3] < cur_page_height * 0.72 or nxt_bbox[1] > nxt_page_height * 0.28:
        return False, []

    score, reasons = _table_continuation_score(cur_table, nxt_table, cur_page, nxt_page)
    return score >= 7, reasons


def _get_cont_info(tab) -> Dict[str, Any]:
    """테이블 첫 데이터 행에서 cross-page rowspan continuation 셀을 감지한다.

    셀 높이가 헤더 행 높이의 1.3배 이하인 첫 데이터 행 셀은 이전 페이지에서
    잘린 rowspan 셀의 나머지 부분이다.
    """
    if tab.row_count < 2 or not tab.cells:
        return {"cont_cells": []}
    try:
        table_y0 = tab.bbox[1]
        raw = tab.extract()
        if len(raw) < 2:
            return {"cont_cells": []}

        # 헤더 행 높이
        header_cells = [c for c in tab.cells if abs(c[1] - table_y0) < 5]
        if not header_cells:
            return {"cont_cells": []}
        header_y1 = max(c[3] for c in header_cells)
        header_height = header_y1 - table_y0

        # 첫 데이터 행 셀들 (y0 ≈ header_y1), x순 정렬
        first_data_cells = sorted(
            [c for c in tab.cells if abs(c[1] - header_y1) < 5 and c[1] > table_y0 + 3],
            key=lambda c: c[0],
        )
        if not first_data_cells:
            return {"cont_cells": []}

        # raw[1] 비-None 값을 col 순서대로
        row1_vals = [(c_idx, v) for c_idx, v in enumerate(raw[1]) if v is not None]

        # 표 왼쪽 절반 이내의 셀만 continuation 후보로 인정
        # (레이블/분류 컬럼은 좌측에 있으며, 우측 데이터 컬럼은 continuation이 아님)
        table_x_center = (tab.bbox[0] + tab.bbox[2]) / 2

        cont_cells = []
        for i, fc in enumerate(first_data_cells):
            if fc[3] - fc[1] > header_height * 1.3:
                continue  # 정상 높이 → continuation 아님
            if (fc[0] + fc[2]) / 2 > table_x_center:
                continue  # 우측 데이터 컬럼 → continuation 아님
            if i >= len(row1_vals):
                continue
            col_idx, text = row1_vals[i]
            if not text or not text.strip():
                continue  # 빈 값은 제외
            cont_cells.append({"col": col_idx, "text": text})

        return {"cont_cells": cont_cells}
    except Exception:
        return {"cont_cells": []}


def _fix_cont_cells(merged_html: str, cont_cells: List[Dict], p1_row_count: int) -> str:
    """병합된 HTML에서 cross-page continuation 셀을 수정한다.

    Phase 1 (텍스트 continuation): 짧은 비어있지 않은 셀 → junction_row 첫 <td>에 병합
    Phase 2 (구조 continuation): 빈 rowspan 셀 → 대응 page1 셀의 rowspan 연장 후 제거
    """
    all_rows = re.findall(r'[ \t]*<tr>.*?[ \t]*</tr>', merged_html, re.DOTALL)
    if len(all_rows) <= p1_row_count:
        return merged_html

    junction_orig = all_rows[p1_row_count - 1]
    cont_orig     = all_rows[p1_row_count]
    junction_row  = junction_orig
    cont_row      = cont_orig

    cell_pat = re.compile(r'<td([^>]*)>(.*?)</td>', re.DOTALL)

    # ── Phase 1: 텍스트 continuation ──
    for cc in cont_cells:
        found = None
        for m in cell_pat.finditer(cont_row):
            inner = ' '.join(re.sub(r'<[^>]+>', '', m.group(2)).split())
            needle = ' '.join(cc["text"].split())
            if inner and needle in inner:
                found = m
                break
        if found is None:
            continue
        cont_cell_inner = found.group(2)
        cont_row = cont_row[:found.start()] + cont_row[found.end():]

        jm = cell_pat.search(junction_row)
        if jm is None:
            continue
        attrs   = jm.group(1)
        content = jm.group(2)
        if 'rowspan' in attrs:
            attrs = re.sub(r'rowspan="(\d+)"',
                           lambda m: f'rowspan="{int(m.group(1)) + 1}"', attrs)
        else:
            attrs += ' rowspan="2"'
        new_cell = f'<td{attrs}>{content}\n    {cont_cell_inner}</td>'
        junction_row = junction_row[:jm.start()] + new_cell + junction_row[jm.end():]

    # ── Phase 2: 구조적 continuation (빈 rowspan 셀) ──
    # cont_row에서 빈 rowspan 셀 → page1의 대응 셀 rowspan 연장 + 빈 셀 제거
    p1_row_mods: Dict[int, str] = {}
    struct_to_remove: List[tuple] = []

    for em in cell_pat.finditer(cont_row):
        if re.sub(r'<[^>]+>', '', em.group(2)).strip():
            continue  # 비어있지 않은 셀은 건너뜀
        rs_m = re.search(r'rowspan="(\d+)"', em.group(1))
        if not rs_m:
            continue
        cont_rs = int(rs_m.group(1))

        # page1 rows에서 rowspan이 p1_row_count에서 끝나는 셀 탐색 (역방향)
        found_p1 = False
        for r in range(p1_row_count - 1, -1, -1):
            p1_row = all_rows[r]
            for cm in cell_pat.finditer(p1_row):
                p1_rs_m = re.search(r'rowspan="(\d+)"', cm.group(1))
                if p1_rs_m and r + int(p1_rs_m.group(1)) == p1_row_count:
                    orig_rs = int(p1_rs_m.group(1))
                    base = p1_row_mods.get(r, p1_row)
                    new_cell = re.sub(r'rowspan="(\d+)"',
                                      f'rowspan="{orig_rs + cont_rs}"',
                                      cm.group(0))
                    p1_row_mods[r] = base.replace(cm.group(0), new_cell, 1)
                    found_p1 = True
                    break
            if found_p1:
                break

        if found_p1:
            junction_cell_count = len(list(cell_pat.finditer(junction_row)))
            remaining_cell_count = sum(
                1
                for other in cell_pat.finditer(cont_row)
                if other.start() != em.start()
            )
            effective_cont_rs = cont_rs
            if junction_cell_count > 0 and remaining_cell_count == junction_cell_count:
                # 이 continuation row 자체는 junction row에 병합될 예정이므로
                # 상위 rowspan에는 그 다음 행들만 추가한다.
                effective_cont_rs = max(0, cont_rs - 1)
            struct_to_remove.append((em.start(), em.end()))
            p1_text = re.sub(r'<[^>]*>', ' ', all_rows[r]).split()
            p1_label = ' '.join(p1_text[:3]) if p1_text else ''
            orig_rs = int(re.search(r'rowspan="(\d+)"', all_rows[r]).group(1))
            if effective_cont_rs != cont_rs:
                base = p1_row_mods.get(r, p1_row)
                p1_row_mods[r] = re.sub(
                    r'rowspan="(\d+)"',
                    f'rowspan="{orig_rs + effective_cont_rs}"',
                    base,
                    count=1,
                )
            print(
                f"[INFO]   구조적 rowspan 연장: '{p1_label}...' "
                f"{orig_rs} → {orig_rs + effective_cont_rs}"
            )

    for start, end in reversed(struct_to_remove):
        cont_row = cont_row[:start] + cont_row[end:]

    # ── Phase 3: 나머지 cont_row 셀을 junction_row 대응 셀에 병합 ──
    # Phase 1/2 이후에도 cont_row에 남은 셀이 있으면 junction_row의 대응 셀과 합친다.
    junc_all = list(cell_pat.finditer(junction_row))
    cont_remaining = list(cell_pat.finditer(cont_row))
    merge_same_row = False
    if len(junc_all) > 1 and len(cont_remaining) == len(junc_all) - 1:
        merge_pairs = list(zip(reversed(junc_all[1:]), reversed(cont_remaining)))
    elif struct_to_remove and len(junc_all) > 1 and len(cont_remaining) == len(junc_all):
        merge_same_row = True
        merge_pairs = list(zip(reversed(junc_all), reversed(cont_remaining)))
    else:
        merge_pairs = []
    if merge_pairs:
        new_junction_row = junction_row
        new_cont_row = cont_row
        for jm, cm in merge_pairs:
            j_attrs = jm.group(1)
            j_content = jm.group(2)
            if merge_same_row:
                pass
            elif 'rowspan' in j_attrs:
                j_attrs = re.sub(r'rowspan="(\d+)"',
                                 lambda m2: f'rowspan="{int(m2.group(1)) + 1}"', j_attrs)
            else:
                j_attrs += ' rowspan="2"'
            new_junc_cell = f'<td{j_attrs}>{j_content}\n    {cm.group(2)}</td>'
            new_junction_row = (new_junction_row[:jm.start()]
                                + new_junc_cell
                                + new_junction_row[jm.end():])
            new_cont_row = new_cont_row[:cm.start()] + new_cont_row[cm.end():]
        junction_row = new_junction_row
        cont_row = new_cont_row

    if cell_pat.search(cont_row) is None:
        cont_row = ""

    # ── 변경 적용 ──
    result = merged_html
    if junction_row != junction_orig:
        result = result.replace(junction_orig, junction_row, 1)
    if cont_row != cont_orig:
        result = result.replace(cont_orig, cont_row, 1)
    for r, new_row in p1_row_mods.items():
        result = result.replace(all_rows[r], new_row, 1)
    return result


def extract_pdf(pdf_path: Path) -> Dict[str, Any]:
    """
    PDF에서 텍스트/이미지/표를 bbox 기반으로 추출하고 reading order 결정
    텍스트 기반 표도 자동 감지하여 이미지로 변환
    """
    from core.converters import extract_table_regions_from_pdf_by_text
    
    doc = fitz.open(pdf_path)
    pages: List[Dict[str, Any]] = []
    seen_image_sha1: set = set()  # 페이지 간 동일 이미지 중복 제거용
    seen_image_xrefs: set[int] = set()

    for page_index in range(len(doc)):
        page = doc.load_page(page_index)
        
        elements = []
        
        # 1. 텍스트 블록 추출 (bbox 포함)
        text_blocks = page.get_text("dict", sort=True)["blocks"]
        page_rect = page.rect
        page_height = page_rect.height
        page_width = page_rect.width
        
        text_block_elements = []
        for block_idx, block in enumerate(text_blocks):
            if block.get("type") == 0:  # 텍스트 블록
                bbox = block.get("bbox", [0, 0, 0, 0])
                text_content = _extract_block_text_sorted(block)
                
                if text_content.strip():
                    # null/PUA 제거 후 쪽번호 필터링
                    cleaned_text = _sanitize_text(text_content.strip())
                    
                    # 쪽번호 패턴 확인: "- 숫자 -" 형식
                    page_number_pattern = re.match(r'^\s*-\s*\d+\s*-\s*$', cleaned_text)
                    
                    # 페이지 하단 중앙 위치 확인 (페이지 높이의 하단 10% 영역, 중앙 ±20% 범위)
                    y_center = (bbox[1] + bbox[3]) / 2
                    x_center = (bbox[0] + bbox[2]) / 2
                    is_bottom_area = y_center > page_height * 0.9  # 하단 10% 영역
                    is_center_area = abs(x_center - page_width / 2) < page_width * 0.2  # 중앙 ±20% 범위
                    
                    # 쪽번호로 판단되면 제외
                    if page_number_pattern and is_bottom_area and is_center_area:
                        print(f"[DEBUG] 쪽번호 제거: '{cleaned_text}' (페이지 {page_index + 1}, 위치: y={y_center:.1f}, x={x_center:.1f})")
                        continue
                    
                    # 단독 숫자도 쪽번호로 의심 (하단 중앙에 위치한 경우)
                    if re.match(r'^\s*\d+\s*$', cleaned_text) and is_bottom_area and is_center_area:
                        print(f"[DEBUG] 쪽번호 제거 (단독 숫자): '{cleaned_text}' (페이지 {page_index + 1})")
                        continue
                    
                    text_block_elements.append({
                        "type": "text",
                        "bbox": list(bbox),
                        "content": cleaned_text,
                        "block_id": f"text-{block_idx}"
                    })
        
        # 2. 이미지 추출 (bbox 포함) — 크기 필터링 + SHA1 중복 제거
        image_elements = []
        skipped_small = 0
        skipped_dup = 0
        for img_index, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            try:
                bbox = page.get_image_bbox(img)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARNING] Page {page_index+1}: image bbox lookup failed: {exc}")
                continue

            # 최소 크기 필터: 50pt(≈17.6mm) 미만 또는 면적 5000pt² 미만인 아이콘/장식 요소 제외
            img_width = bbox.x1 - bbox.x0
            img_height = bbox.y1 - bbox.y0
            if img_width < 50 or img_height < 50 or (img_width * img_height) < 5000:
                skipped_small += 1
                continue

            if xref in seen_image_xrefs:
                skipped_dup += 1
                continue

            pix = fitz.Pixmap(doc, xref)
            if pix.n >= 4:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            img_bytes = pix.tobytes("png")
            pix = None

            # SHA1 해시 기반 중복 제거 (동일 헤더/로고가 매 페이지 반복되는 경우)
            sha1 = _hash_bytes(img_bytes)
            if sha1 in seen_image_sha1:
                skipped_dup += 1
                continue
            seen_image_sha1.add(sha1)
            seen_image_xrefs.add(xref)

            image_elements.append({
                "type": "image",
                "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
                "image_id": f"page-{page_index+1}-img-{img_index+1}",
                "page": page_index + 1,
                "bytes": img_bytes,
                "sha1": sha1,
                "needs_vlm": True
            })

        if skipped_small or skipped_dup:
            print(f"[DEBUG] Page {page_index+1}: 이미지 필터링 — {skipped_small}개 소형 제외, {skipped_dup}개 중복 제외, {len(image_elements)}개 유지")

        # 2-1. 텍스트 기반 표 감지 (벡터 그래픽 판단보다 먼저 수행)
        table_regions = extract_table_regions_from_pdf_by_text(pdf_path, page_index + 1, table_count=0)
        if table_regions and image_elements:
            filtered_table_regions = []
            dropped_regions = 0
            for region in table_regions:
                rx0, ry0, rx1, ry1 = region
                region_area = max(0, rx1 - rx0) * max(0, ry1 - ry0)
                matched_image = None
                for img_elem in image_elements:
                    ix0, iy0, ix1, iy1 = img_elem["bbox"]
                    image_area = max(0, ix1 - ix0) * max(0, iy1 - iy0)
                    if image_area <= 0:
                        continue
                    overlap_x = max(0, min(rx1, ix1) - max(rx0, ix0))
                    overlap_y = max(0, min(ry1, iy1) - max(ry0, iy0))
                    overlap_area = overlap_x * overlap_y
                    image_covered = overlap_area / image_area
                    # 표 후보가 이미 존재하는 임베디드 이미지를 대부분 포함하면
                    # 텍스트 기반 표 오검출로 보고 주변 문단 삭제를 막는다.
                    if image_covered >= 0.85 and region_area > image_area * 1.3:
                        matched_image = img_elem
                        break
                if matched_image is not None:
                    matched_image["is_table"] = True
                    matched_image["is_embedded_table"] = True
                    dropped_regions += 1
                    continue
                filtered_table_regions.append(region)
            if dropped_regions:
                print(
                    f"[DEBUG] Page {page_index + 1}: 임베디드 표 이미지와 겹치는 텍스트 기반 표 후보 "
                    f"{dropped_regions}개 제외"
                )
            table_regions = filtered_table_regions

        # 2-2. 벡터 그래픽 감지: 임베디드 이미지가 없거나 적은데 drawing 객체가 많으면
        #      흐름도나 다이어그램일 가능성이 높음
        #      단, 텍스트 기반 표가 이미 감지된 경우 전체 페이지 렌더링 생략 (표 중복 방지)
        has_few_images = len(image_elements) < 2  # 임베디드 이미지가 2개 미만
        has_drawings = False
        page_drawings = []

        # Drawing 객체 확인 (벡터 그래픽)
        try:
            page_drawings = page.get_drawings()
            if page_drawings and len(page_drawings) > 10:  # Drawing 객체가 10개 이상이면 다이어그램 가능성
                has_drawings = True
                print(f"[DEBUG] Page {page_index + 1}: {len(page_drawings)}개의 drawing 객체 감지")
        except Exception:
            pass

        page_text_length = sum(len(elem.get("content", "").strip()) for elem in text_block_elements)
        has_substantial_text_layer = page_text_length >= 200 or len(text_block_elements) >= 5

        # 벡터 그래픽이 있고 이미지가 적으면 전체 페이지를 이미지로 렌더링
        # 단, 텍스트 레이어가 충분하거나 텍스트 기반 표가 발견된 페이지는 건너뜀.
        if has_few_images and has_drawings and not table_regions and not has_substantial_text_layer:
            print(f"[INFO] Page {page_index + 1}: 벡터 그래픽 감지 (표 없음), 전체 페이지를 이미지로 렌더링")
            try:
                # 고해상도 렌더링 (2배)
                mat = fitz.Matrix(2, 2)
                pix_fullpage = page.get_pixmap(matrix=mat)
                page_img_bytes = pix_fullpage.tobytes("png")
                pix_fullpage = None

                # 전체 페이지 이미지를 첫 번째 이미지로 추가
                page_bbox = page.rect
                image_elements.insert(0, {
                    "type": "image",
                    "bbox": [page_bbox.x0, page_bbox.y0, page_bbox.x1, page_bbox.y1],
                    "image_id": f"page-{page_index+1}-fullpage",
                    "page": page_index + 1,
                    "bytes": page_img_bytes,
                    "sha1": _hash_bytes(page_img_bytes),
                    "needs_vlm": True,
                    "is_full_page": True,  # 전체 페이지 이미지 표시
                    "is_flowchart": None  # VLM 자동 분류에 위임
                })
                print(f"[INFO] Page {page_index + 1}: 전체 페이지 이미지 추가 완료 (자동 분류 위임)")
            except Exception as e:
                print(f"[WARNING] Page {page_index + 1}: 전체 페이지 렌더링 실패: {e}")
        elif has_few_images and has_drawings and (table_regions or has_substantial_text_layer):
            reason = (
                f"텍스트 기반 표 {len(table_regions)}개 존재"
                if table_regions
                else f"텍스트 레이어 충분 ({page_text_length}자, {len(text_block_elements)}블록)"
            )
            print(f"[DEBUG] Page {page_index + 1}: 벡터 그래픽 감지되었으나 {reason} → 전체 페이지 렌더링 생략")

        # 2-3. 표 영역 근처의 작은 임베디드 이미지 필터링
        # (표 테두리/장식 요소가 별도 이미지로 추출되어 VLM 할루시네이션 유발 방지)
        if table_regions and image_elements:
            filtered_image_elements = []
            for img_elem in image_elements:
                if img_elem.get("is_full_page"):
                    filtered_image_elements.append(img_elem)
                    continue
                img_bbox = img_elem["bbox"]
                img_cx = (img_bbox[0] + img_bbox[2]) / 2
                img_cy = (img_bbox[1] + img_bbox[3]) / 2
                img_area = (img_bbox[2] - img_bbox[0]) * (img_bbox[3] - img_bbox[1])
                # 표 영역 근처(마진 20pt)의 작은 이미지(면적 < 15000pt²)는 장식 요소로 간주
                near_table = False
                for tx0, ty0, tx1, ty1 in table_regions:
                    margin = 20
                    if (tx0 - margin <= img_cx <= tx1 + margin and
                        ty0 - margin <= img_cy <= ty1 + margin):
                        near_table = True
                        break
                if near_table and img_area < 15000:
                    print(f"[DEBUG] Page {page_index + 1}: 표 근처 소형 이미지 제외 (면적: {img_area:.0f}pt², id: {img_elem.get('image_id', '')})")
                    continue
                filtered_image_elements.append(img_elem)
            if len(filtered_image_elements) < len(image_elements):
                print(f"[DEBUG] Page {page_index + 1}: 표 근처 소형 이미지 {len(image_elements) - len(filtered_image_elements)}개 필터링")
            image_elements = filtered_image_elements

        # 2-4. 네이티브 표 추출 (find_tables() 기반) — VLM 없이 직접 HTML 생성
        native_table_elements = []
        nested_table_candidates = []
        native_table_bboxes = []
        try:
            fitz_tabs = page.find_tables()
            for tab_idx, tab in enumerate(fitz_tabs.tables):
                html = _table_cells_to_html(tab)
                if html:
                    tab_bbox = list(tab.bbox)
                    table_element = {
                        "type": "native_table",
                        "bbox": tab_bbox,
                        "html": html,
                        "image_id": f"page-{page_index+1}-native-table-{tab_idx+1}",
                        "page": page_index + 1,
                        "_cont_info": _get_cont_info(tab),
                        # 셀 내부 이미지/차트 설명을 표 셀에 후삽입하기 위한 위치 메타.
                        "cells": _table_cells_to_meta(tab),
                    }
                    nested_table_candidates.append(table_element)
                    if _is_native_table_reliable(tab):
                        native_table_elements.append({
                            **table_element,
                        })
                        native_table_bboxes.append(tab_bbox)
                        print(f"[INFO] Page {page_index+1}: 표 네이티브 추출 ({tab.row_count}행×{tab.col_count}열) → VLM 건너뜀")
        except Exception as _e:
            print(f"[WARNING] Page {page_index+1}: find_tables() 실패: {_e}")

        if image_elements and native_table_elements:
            embedded_count = 0
            for img_elem in image_elements:
                if img_elem.get("is_full_page") or img_elem.get("is_text_table"):
                    continue
                img_bbox = img_elem.get("bbox", [0, 0, 0, 0])
                img_cx = (img_bbox[0] + img_bbox[2]) / 2
                img_cy = (img_bbox[1] + img_bbox[3]) / 2
                for table in native_table_elements:
                    table_bbox = table.get("bbox", [0, 0, 0, 0])
                    if not _bbox_contains(table_bbox, img_bbox, tolerance=6.0):
                        continue
                    for cell in table.get("cells", []):
                        cell_bbox = cell.get("bbox", [0, 0, 0, 0])
                        if not (
                            cell_bbox[0] <= img_cx <= cell_bbox[2]
                            and cell_bbox[1] <= img_cy <= cell_bbox[3]
                        ):
                            continue
                        if _bbox_overlap_ratio(img_bbox, cell_bbox) < 0.70:
                            continue
                        # 표 셀 안 차트/플로우는 standalone 이미지가 아니라 해당 셀의 내용으로 귀속한다.
                        img_elem["is_embedded_in_table_cell"] = True
                        img_elem["embedded_table_cell"] = {
                            "table_image_id": table.get("image_id"),
                            "row": cell.get("row"),
                            "col": cell.get("col"),
                        }
                        img_elem["is_flowchart"] = None
                        embedded_count += 1
                        break
                    if img_elem.get("is_embedded_in_table_cell"):
                        break
            if embedded_count:
                print(f"[INFO] Page {page_index+1}: 표 셀 내부 이미지 {embedded_count}개를 셀 설명 후보로 표시")

        container_table_elements = _detect_single_column_container_tables(
            page,
            text_block_elements,
            nested_table_candidates,
        )
        if container_table_elements:
            container_bboxes = [item["bbox"] for item in container_table_elements]
            nested_count = len(native_table_elements)
            native_table_elements = [
                item
                for item in native_table_elements
                if not any(_bbox_contains(container_bbox, item.get("bbox", [0, 0, 0, 0]), tolerance=8.0) for container_bbox in container_bboxes)
            ]
            absorbed_count = nested_count - len(native_table_elements)
            native_table_bboxes = [item["bbox"] for item in native_table_elements]
            native_table_elements.extend(container_table_elements)
            native_table_bboxes.extend(container_bboxes)
            print(
                f"[INFO] Page {page_index+1}: 단일 컬럼 컨테이너 표 {len(container_table_elements)}개 직접 복원 "
                f"(중첩 native 표 {absorbed_count}개 흡수)"
            )

        native_table_elements = _remove_contained_native_table_duplicates(
            native_table_elements,
            page_index + 1,
        )
        native_table_bboxes = [item["bbox"] for item in native_table_elements]

        # 네이티브로 처리된 표 영역은 이미지 렌더링에서 제외
        image_table_regions = table_regions
        if native_table_bboxes and table_regions:
            image_table_regions = [
                r
                for r in table_regions
                if not _bbox_overlaps_any(r, native_table_bboxes)
                and not _is_table_crop_covered_by_native_tables(r, native_table_bboxes)
                and not _is_oversized_table_crop_for_native_tables(
                    r,
                    native_table_bboxes,
                    page_width,
                    page_height,
                )
            ]
            skipped = len(table_regions) - len(image_table_regions)
            if skipped:
                print(f"[DEBUG] Page {page_index+1}: 네이티브 추출로 처리된 표 {skipped}개 → 이미지 렌더링 생략")

        # 3. 표 영역 처리: 비네이티브 표는 bbox별 고해상도 crop으로 저장해 VLM 인식률을 높인다.
        table_image_elements = []
        if image_table_regions:
            print(f"[DEBUG] Page {page_index+1}: {len(image_table_regions)}개 표 → 이미지 렌더링")
            for table_idx, (x0, y0, x1, y1) in enumerate(image_table_regions):
                scale = _table_crop_scale((x0, y0, x1, y1), page_width, page_height)
                padding = 6.0
                clip = fitz.Rect(
                    max(0.0, x0 - padding),
                    max(0.0, y0 - padding),
                    min(float(page_width), x1 + padding),
                    min(float(page_height), y1 + padding),
                )
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
                img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                img = ImageEnhance.Contrast(img).enhance(1.12)
                img = img.filter(ImageFilter.SHARPEN)
                img_buffer = io.BytesIO()
                img.save(img_buffer, format="PNG")
                img_bytes = img_buffer.getvalue()
                sha1 = _hash_bytes(img_bytes)
                table_image_elements.append({
                    "type": "image",
                    "bbox": [x0, y0, x1, y1],
                    "image_id": f"page-{page_index+1}-table-{table_idx+1}",
                    "page": page_index + 1,
                    "bytes": img_bytes,
                    "sha1": sha1,
                    "needs_vlm": True,
                    "is_text_table": True,
                })

        # 표 내부 텍스트 제거 (네이티브 + 비네이티브 표 모두)
        all_table_bboxes_for_removal = native_table_bboxes + [list(r) for r in table_regions]
        if all_table_bboxes_for_removal:
            filtered_text_blocks = []
            removed_text_count = 0
            for text_elem in text_block_elements:
                text_bbox = text_elem["bbox"]
                remove_as_table_text = False
                for table_bbox in all_table_bboxes_for_removal:
                    if _should_remove_text_for_table_region(
                        text_bbox,
                        table_bbox,
                        page_width,
                        page_height,
                        text_elem.get("content", ""),
                    ):
                        remove_as_table_text = True
                        break
                if remove_as_table_text:
                    removed_text_count += 1
                else:
                    filtered_text_blocks.append(text_elem)
            if removed_text_count:
                print(
                    f"[DEBUG] Page {page_index+1}: 표 내부 텍스트 {removed_text_count}개 제거 "
                    f"(주변 문단/캡션 보존)"
                )
            text_block_elements = filtered_text_blocks

        # 4. 모든 요소 합치기
        elements = text_block_elements + image_elements + table_image_elements + native_table_elements
        
        # 5. Reading order 정렬
        sorted_elements = _sort_by_reading_order(elements)
        
        # 6. 페이지 텍스트 (기존 호환성) - null/PUA 제거, 쪽번호 제거
        full_text = _sanitize_text(page.get_text("text", sort=True))
        # 쪽번호 패턴 제거: "- 숫자 -" 형식
        lines = full_text.split('\n')
        cleaned_lines = []
        for line in lines:
            stripped = line.strip()
            # 쪽번호 패턴 제거
            if not re.match(r'^\s*-\s*\d+\s*-\s*$', stripped):
                # 단독 숫자도 제거 (하단에 위치할 가능성 높음)
                if not (re.match(r'^\s*\d+\s*$', stripped) and len(cleaned_lines) > len(lines) * 0.9):
                    cleaned_lines.append(line)
        full_text = '\n'.join(cleaned_lines)
        
        # 7. 이미지 리스트 (기존 호환성)
        images = [e for e in sorted_elements if e["type"] == "image"]
        
        pages.append({
            "page": page_index + 1,
            "width": page_width,
            "height": page_height,
            "text": full_text,
            "images": images,
            "elements": sorted_elements,
        })

    doc.close()
    return {"pages": pages, "page_count": len(pages)}


def detect_effective_pdf_pages(pages: List[Dict[str, Any]]) -> int:
    """
    OLE/바이너리 덤프로 생성된 쓰레기 페이지를 감지하여 유효한 페이지 수만 반환.
    Page 2부터 'Root Entry', 'FileHeader' 등이 보이거나 기호(#) 비율이 높으면
    그 이전까지를 유효 페이지로 본다.
    """
    if not pages:
        return 0
    # OLE/PDF 내부 구조 이름 (쓰레기 페이지 지표)
    OLE_MARKERS = ("Root Entry", "FileHeader", "HwpSummaryInformation", "DocInfo")
    for i in range(1, len(pages)):
        text = (pages[i].get("text") or "").strip()
        if not text:
            continue
        # OLE/내부 이름이 보이면 이 페이지부터 쓰레기
        if any(m in text for m in OLE_MARKERS):
            print(f"[INFO] 쓰레기 페이지 감지 (페이지 {i+1}): OLE/내부 구조 이름 발견. 유효 페이지 수 = {i}")
            return i
        # 기호(#) 비율이 매우 높으면 바이너리 덤프로 간주
        if len(text) > 50:
            sharp_ratio = text.count("#") / max(len(text), 1)
            if sharp_ratio > 0.25:
                print(f"[INFO] 쓰레기 페이지 감지 (페이지 {i+1}): '#' 비율 높음 ({sharp_ratio:.2f}). 유효 페이지 수 = {i}")
                return i
        # 한글이 거의 없고 길이가 긴 경우 (영문/기호 위주)
        if len(text) > 200:
            hangul = sum(1 for c in text if "\uAC00" <= c <= "\uD7A3")
            if hangul / max(len(text), 1) < 0.02:
                print(f"[INFO] 쓰레기 페이지 감지 (페이지 {i+1}): 한글 비율 매우 낮음. 유효 페이지 수 = {i}")
                return i
    return len(pages)
