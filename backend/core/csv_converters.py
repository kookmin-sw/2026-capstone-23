from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Dict, List

from core.conversion_common import ConversionError
from core.spreadsheet_formatting import _build_html_table, _build_markdown_table, _trim_empty_trailing

def parse_csv(path: Path) -> Dict[str, Any]:
    """
    CSV 파일을 직접 파싱하여 구조화된 결과 반환 (단일 시트).
    인코딩 자동 감지 (utf-8 → cp949 → euc-kr → latin-1),
    구분자 자동 감지 (csv.Sniffer).

    Returns:
        build_document_text()와 호환되는 딕셔너리
    """
    # 인코딩 자동 감지
    raw_bytes = path.read_bytes()
    content = None
    detected_encoding = None

    for enc in ["utf-8-sig", "utf-8", "cp949", "euc-kr", "latin-1"]:
        try:
            content = raw_bytes.decode(enc)
            detected_encoding = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if content is None:
        raise ConversionError(f"CSV 인코딩 감지 실패: {path}")

    print(f"[INFO] CSV 인코딩 감지: {detected_encoding} ({path.name})")

    # 구분자 자동 감지
    sample = content[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel  # 기본 콤마 구분

    # CSV 파싱
    reader = csv.reader(io.StringIO(content), dialect)
    raw_rows: List[List[str]] = []
    for row in reader:
        raw_rows.append([c.strip() for c in row])

    # 빈 파일 처리
    if not raw_rows:
        return {
            "page_count": 1,
            "pages": [{
                "page": 1,
                "text": "(빈 CSV 파일)",
                "images": [],
                "elements": [],
            }],
        }

    # 열 수 통일
    max_cols = max(len(r) for r in raw_rows)
    for row in raw_rows:
        while len(row) < max_cols:
            row.append("")

    # 후행 빈 행/열 트리밍
    raw_rows, max_cols = _trim_empty_trailing(raw_rows, max_cols)

    if not raw_rows or max_cols == 0:
        return {
            "page_count": 1,
            "pages": [{
                "page": 1,
                "text": "(빈 CSV 파일)",
                "images": [],
                "elements": [],
            }],
        }

    sheet_name = path.stem
    html = _build_html_table(raw_rows, header_row=True)
    md = _build_markdown_table(raw_rows, sheet_name)

    print(f"[INFO] CSV 파싱 완료: {path.name} → {len(raw_rows)}행 × {max_cols}열")

    return {
        "page_count": 1,
        "pages": [{
            "page": 1,
            "text": "",
            "images": [],
            "elements": [
                {
                    "type": "table",
                    "content": html,
                    "markdown": md,
                    "sheet": sheet_name,
                    "bbox": None,
                }
            ],
        }],
    }


# ──────────────────────────────────────────────────────────────
# B 파트: 스타일 기반 Excel 표 추출 (parse_hwpx_tables 동일 형식)
# ──────────────────────────────────────────────────────────────
