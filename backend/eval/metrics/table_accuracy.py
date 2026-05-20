"""HTML 표 정확도 — 셀 단위 Precision / Recall / F1"""
from html.parser import HTMLParser
from typing import List, Optional, Tuple


class _TableCellParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells: List[str] = []
        self._current: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self._current = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._current is not None:
            self.cells.append(" ".join(self._current.split()))
            self._current = None

    def handle_data(self, data):
        if self._current is not None:
            self._current += data


def _parse_cells(html: str) -> List[str]:
    parser = _TableCellParser()
    parser.feed(html)
    return parser.cells


def _cell_f1(pred_cells: List[str], gt_cells: List[str]) -> Tuple[float, float, float]:
    if not gt_cells:
        return (0.0, 0.0, 0.0)
    if not pred_cells:
        return (0.0, 0.0, 0.0)

    gt_set = {}
    for c in gt_cells:
        gt_set[c] = gt_set.get(c, 0) + 1

    matched = 0
    for c in pred_cells:
        if gt_set.get(c, 0) > 0:
            matched += 1
            gt_set[c] -= 1

    precision = matched / len(pred_cells)
    recall = matched / len(gt_cells)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def compute_table_accuracy(pred_tables: List[str], gt_tables: List[str]) -> dict:
    """
    예측 표 리스트와 GT 표 리스트를 비교하여 셀 단위 정확도 반환.
    GT가 없으면 None 반환.

    Returns:
        {
          "precision": float,
          "recall": float,
          "f1": float,
          "pred_table_count": int,
          "gt_table_count": int,
        }
        또는 None (GT 없는 경우)
    """
    if not gt_tables:
        return None

    # 표가 여러 개일 때: 모든 GT 셀과 예측 셀을 합산하여 비교
    all_pred_cells = []
    for t in pred_tables:
        all_pred_cells.extend(_parse_cells(t))

    all_gt_cells = []
    for t in gt_tables:
        all_gt_cells.extend(_parse_cells(t))

    precision, recall, f1 = _cell_f1(all_pred_cells, all_gt_cells)
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "pred_table_count": len(pred_tables),
        "gt_table_count": len(gt_tables),
    }
