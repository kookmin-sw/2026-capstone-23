from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Dict, List, Tuple

def _normalize_excel_text_value(value: Any) -> str:
    """엑셀 셀 값을 텍스트 추출용 문자열로 정규화"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (datetime, date)):
        if isinstance(value, datetime):
            if value.time() == time(0, 0, 0):
                return value.strftime("%Y-%m-%d")
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value.strftime("%Y-%m-%d")
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def _cell_value_to_str(value) -> str:
    """셀 값을 문자열로 변환 (정확한 데이터 보존)"""
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        if isinstance(value, datetime):
            if value.time() == time(0, 0, 0):
                return value.strftime("%Y-%m-%d")
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value.strftime("%Y-%m-%d")
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, float):
        # 소수점 이하 불필요한 0 제거 (예: 100.0 → 100)
        if value == int(value):
            return str(int(value))
        return str(value)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _escape_html(text: str) -> str:
    """HTML 특수문자 이스케이프"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_html_table(rows: List[List[str]], header_row: bool = True,
                      merge_map: Dict[Tuple[int, int], Tuple[int, int]] = None,
                      skip_cells: set = None) -> str:
    """2차원 데이터를 HTML 표로 변환 (병합 셀 지원)"""
    if not rows:
        return ""

    merge_map = merge_map or {}
    skip_cells = skip_cells or set()

    html_lines = ["<table>"]
    for r_idx, row in enumerate(rows):
        html_lines.append("<tr>")
        for c_idx, cell_val in enumerate(row):
            if (r_idx, c_idx) in skip_cells:
                continue

            tag = "th" if header_row and r_idx == 0 else "td"
            attrs = ""

            if (r_idx, c_idx) in merge_map:
                rowspan, colspan = merge_map[(r_idx, c_idx)]
                if rowspan > 1:
                    attrs += f' rowspan="{rowspan}"'
                if colspan > 1:
                    attrs += f' colspan="{colspan}"'

            escaped = _escape_html(cell_val)
            # 셀 내 줄바꿈 → <br>
            escaped = escaped.replace("\n", "<br>")
            html_lines.append(f"<{tag}{attrs}>{escaped}</{tag}>")
        html_lines.append("</tr>")
    html_lines.append("</table>")
    return "\n".join(html_lines)


def _build_markdown_table(rows: List[List[str]], sheet_name: str = "") -> str:
    """2차원 데이터를 Markdown 표로 변환"""
    if not rows:
        return ""

    parts = []
    title = sheet_name if sheet_name else "데이터"
    parts.append(f"# TableTitle: {title}")
    parts.append("")

    # 열 수 통일
    max_cols = max(len(r) for r in rows) if rows else 0
    if max_cols == 0:
        return ""

    normalized = []
    for row in rows:
        padded = list(row) + [""] * (max_cols - len(row))
        normalized.append(padded)

    # 헤더 행
    header = normalized[0]
    header_line = "| " + " | ".join(h.replace("|", "\\|").replace("\n", " ") for h in header) + " |"
    parts.append(header_line)

    # 구분선
    sep_line = "| " + " | ".join("---" for _ in header) + " |"
    parts.append(sep_line)

    # 데이터 행
    for row in normalized[1:]:
        data_line = "| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in row) + " |"
        parts.append(data_line)

    return "\n".join(parts)


def _trim_empty_trailing(rows: List[List[str]], max_cols: int) -> Tuple[List[List[str]], int]:
    """후행 빈 행/열 트리밍"""
    if not rows:
        return rows, max_cols

    # 후행 빈 행 제거
    while rows and all(c == "" for c in rows[-1]):
        rows.pop()

    if not rows:
        return rows, 0

    # 후행 빈 열 제거
    while max_cols > 0:
        if all(len(r) < max_cols or r[max_cols - 1] == "" for r in rows):
            max_cols -= 1
        else:
            break

    trimmed = [r[:max_cols] for r in rows]
    return trimmed, max_cols
