"""Strict table structure match metric.

Compares predicted HTML tables against GT tables with strict structure signals:
- table geometry (row/col size)
- anchor cell positions with rowspan/colspan
- full occupied grid coordinates
- header region alignment (th coverage)

Returns a score in [0, 1].
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class _CellDef:
    is_header: bool
    colspan: int
    rowspan: int


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: List[List[_CellDef]] = []
        self._current_row: Optional[List[_CellDef]] = None
        self._table_depth: int = 0  # 중첩 깊이: 1=외부 표, 2+=내부 표

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_depth += 1
            return

        # 외부 표(depth==1) 레벨의 행/셀만 처리, 내부 중첩 표는 무시
        if self._table_depth != 1:
            return

        attrs_dict = dict(attrs)
        if tag == "tr":
            self._current_row = []
            return

        if tag in ("td", "th") and self._current_row is not None:
            try:
                colspan = int(attrs_dict.get("colspan", 1))
            except Exception:
                colspan = 1
            try:
                rowspan = int(attrs_dict.get("rowspan", 1))
            except Exception:
                rowspan = 1

            if colspan < 1:
                colspan = 1
            if rowspan < 1:
                rowspan = 1

            self._current_row.append(
                _CellDef(
                    is_header=(tag == "th"),
                    colspan=colspan,
                    rowspan=rowspan,
                )
            )

    def handle_endtag(self, tag):
        if tag == "table":
            self._table_depth -= 1
            return

        if self._table_depth != 1:
            return

        if tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None


def _clean_html(html: str) -> str:
    return re.sub(r"```(?:html)?", "", html or "", flags=re.IGNORECASE).replace("```", "")


def _lcs_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        curr = [0]
        for j, cb in enumerate(b, 1):
            if ca == cb:
                curr.append(prev[j - 1] + 1)
            else:
                curr.append(max(prev[j], curr[-1]))
        prev = curr
    return prev[-1]


def _table_text_key(html: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", html or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", text)


def _table_text_similarity(gt_html: str, pred_html: str) -> float:
    gt = _table_text_key(gt_html)
    pred = _table_text_key(pred_html)
    if not gt and not pred:
        return 1.0
    if not gt or not pred:
        return 0.0
    lcs = _lcs_len(gt, pred)
    return (2 * lcs) / max(len(gt) + len(pred), 1)


def _parse_rows(html: str) -> List[List[_CellDef]]:
    parser = _TableParser()
    try:
        parser.feed(_clean_html(html))
    except Exception:
        pass
    return parser.rows


def _safe_ratio(a: int, b: int) -> float:
    m = max(a, b)
    if m == 0:
        return 1.0
    return min(a, b) / m


def _f1_sets(a: Set[Tuple], b: Set[Tuple]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0

    inter = len(a & b)
    p = inter / len(b)
    r = inter / len(a)
    if p + r == 0:
        return 0.0
    return (2 * p * r) / (p + r)


def _build_structure(rows: List[List[_CellDef]]) -> Dict[str, object]:
    """Convert row/span declarations into strict structure sets."""
    occupied: Set[Tuple[int, int]] = set()
    header_occupied: Set[Tuple[int, int]] = set()
    anchors: Set[Tuple[int, int, int, int, int]] = set()  # r,c,rowspan,colspan,is_header

    # keep track of cells occupied by rowspans from previous rows
    blocked: Set[Tuple[int, int]] = set()

    max_r = -1
    max_c = -1

    for r_idx, row in enumerate(rows):
        c_cursor = 0

        for cell in row:
            while (r_idx, c_cursor) in blocked:
                c_cursor += 1

            is_header_int = 1 if cell.is_header else 0
            anchors.add((r_idx, c_cursor, cell.rowspan, cell.colspan, is_header_int))

            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    rr = r_idx + dr
                    cc = c_cursor + dc
                    occupied.add((rr, cc))
                    if cell.is_header:
                        header_occupied.add((rr, cc))
                    blocked.add((rr, cc))
                    if rr > max_r:
                        max_r = rr
                    if cc > max_c:
                        max_c = cc

            c_cursor += cell.colspan

    return {
        "rows": max_r + 1 if max_r >= 0 else 0,
        "cols": max_c + 1 if max_c >= 0 else 0,
        "anchors": anchors,
        "occupied": occupied,
        "header": header_occupied,
    }


def _score_one_table(gt_html: str, pred_html: str) -> float:
    gt_rows = _parse_rows(gt_html)
    pred_rows = _parse_rows(pred_html)

    if not gt_rows and not pred_rows:
        return 1.0
    if not gt_rows or not pred_rows:
        return 0.0

    gt = _build_structure(gt_rows)
    pred = _build_structure(pred_rows)

    dim_score = 0.5 * _safe_ratio(int(gt["rows"]), int(pred["rows"])) + 0.5 * _safe_ratio(int(gt["cols"]), int(pred["cols"]))
    anchor_score = _f1_sets(gt["anchors"], pred["anchors"])
    occupied_score = _f1_sets(gt["occupied"], pred["occupied"])
    header_score = _f1_sets(gt["header"], pred["header"])

    # stricter weighting: exact cell anchors matter most
    score = (
        0.15 * dim_score
        + 0.45 * anchor_score
        + 0.30 * occupied_score
        + 0.10 * header_score
    )
    score = max(0.0, min(1.0, score))
    text_score = _table_text_similarity(gt_html, pred_html)
    if text_score >= 0.85:
        return max(score, min(0.92, text_score))
    return score


def compute_table_match(pred_tables: List[str], gt_tables: Optional[List[str]]) -> Optional[dict]:
    """Return strict structure score in [0,1]."""
    if not gt_tables:
        return None
    if not pred_tables:
        return {"score": 0.0}
    if len(gt_tables) == 1 and len(pred_tables) > 1:
        pred_tables = ["\n".join(pred_tables)]

    max_len = max(len(gt_tables), len(pred_tables))
    if max_len == 0:
        return {"score": 0.0}

    scores: List[float] = []
    for i in range(max_len):
        gt_html = gt_tables[i] if i < len(gt_tables) else ""
        pred_html = pred_tables[i] if i < len(pred_tables) else ""
        scores.append(_score_one_table(gt_html, pred_html))

    final = sum(scores) / len(scores)
    return {"score": round(final, 4)}
