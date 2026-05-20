"""Grid Validation — 표 구조 정합성 검증

GT 없이도 실행 가능.
추출된 HTML 표가 정상적인 행×열 그리드 구조를 유지하는지 검증한다.

검증 항목:
1. 각 행의 유효 셀 수(colspan 반영)가 전체 열 수와 일치하는지
2. rowspan으로 인해 아래 행에서 점유된 칸을 고려한 그리드 점유 충돌 없는지
3. 빈 표(행/셀 없음) 여부
"""
from html.parser import HTMLParser
from typing import List, Optional, Tuple


class _TableStructureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: List[List[Tuple[int, int]]] = []  # [(colspan, rowspan), ...]
        self._current_row: Optional[List[Tuple[int, int]]] = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            colspan = int(attrs_dict.get("colspan", 1))
            rowspan = int(attrs_dict.get("rowspan", 1))
            self._current_row.append((colspan, rowspan))

    def handle_endtag(self, tag):
        if tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None


def _validate_grid(html: str) -> Tuple[bool, str]:
    """
    HTML 표 하나의 그리드 구조를 검증한다.

    Returns:
        (valid: bool, reason: str)
    """
    parser = _TableStructureParser()
    try:
        parser.feed(html)
    except Exception as e:
        return False, f"HTML 파싱 오류: {e}"

    rows = parser.rows
    if not rows:
        return False, "행(tr)이 없음"

    # 그리드 점유 시뮬레이션
    # occupied[row][col] = True 이면 해당 셀이 이미 점유됨
    max_rows = len(rows) + max(
        (rs - 1 for row in rows for (_, rs) in row), default=0
    )
    max_cols_estimate = sum(cs for (cs, _) in rows[0]) if rows else 0
    for row in rows:
        col_sum = sum(cs for (cs, _) in row)
        if col_sum > max_cols_estimate:
            max_cols_estimate = col_sum

    grid_cols = max_cols_estimate
    occupied = [[False] * (grid_cols + 5) for _ in range(max_rows + 5)]

    for r_idx, row in enumerate(rows):
        c_cursor = 0
        # 이미 점유된 칸 건너뛰기
        while c_cursor < grid_cols and occupied[r_idx][c_cursor]:
            c_cursor += 1

        for colspan, rowspan in row:
            # 빈 칸 건너뜀
            while c_cursor < grid_cols + 5 and occupied[r_idx][c_cursor]:
                c_cursor += 1

            # 점유 표시
            for dr in range(rowspan):
                for dc in range(colspan):
                    tr, tc = r_idx + dr, c_cursor + dc
                    if tr < len(occupied) and tc < len(occupied[0]):
                        if occupied[tr][tc]:
                            return False, f"셀 충돌: ({tr}, {tc}) 위치가 중복 점유됨"
                        occupied[tr][tc] = True

            c_cursor += colspan

        while c_cursor < grid_cols and occupied[r_idx][c_cursor]:
            c_cursor += 1

    # 모든 행의 첫 번째 행 기준 열 수 일치 여부 확인
    col_counts = []
    for r_idx, row in enumerate(rows):
        # rowspan 점유 포함한 유효 열 수
        filled = sum(1 for c in range(grid_cols + 5) if occupied[r_idx][c])
        col_counts.append(filled)

    if len(set(col_counts)) > 1:
        return False, f"행마다 열 수 불일치: {col_counts}"

    return True, "정상"


def compute_grid_validation(tables: List[str]) -> dict:
    """
    추출된 표 리스트의 그리드 구조를 검증한다. GT 불필요.

    Returns:
        {
          "total": int,          # 검증한 표 수
          "valid": int,          # 정상 표 수
          "invalid": int,        # 비정상 표 수
          "score": float,        # valid / total (0.0 ~ 1.0)
          "details": [{"index": int, "valid": bool, "reason": str}, ...]
        }
    """
    if not tables:
        return {
            "total": 0,
            "valid": 0,
            "invalid": 0,
            "score": None,
            "details": [],
        }

    details = []
    for i, html in enumerate(tables):
        valid, reason = _validate_grid(html)
        details.append({"index": i + 1, "valid": valid, "reason": reason})

    valid_count = sum(1 for d in details if d["valid"])
    return {
        "total": len(tables),
        "valid": valid_count,
        "invalid": len(tables) - valid_count,
        "score": round(valid_count / len(tables), 4),
        "details": details,
    }
