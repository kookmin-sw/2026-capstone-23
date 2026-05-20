"""표표.pdf 3페이지 native table 디버그."""
import sys
from pathlib import Path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import fitz
from core.pdf_extractor import _is_native_table_reliable, _table_cells_to_html, _bbox_area, _bbox_contains

PDF = ROOT / "data/testdata/multi/표표.pdf"
doc = fitz.open(PDF)

for page_idx in range(len(doc)):
    page = doc.load_page(page_idx)
    print(f"\n=== Page {page_idx+1} ===")
    try:
        fitz_tabs = page.find_tables()
        all_tabs = list(fitz_tabs.tables)
        print(f"  find_tables() -> {len(all_tabs)}개")
        tab_bboxes = [list(tab.bbox) for tab in all_tabs]
        page_h = page.rect.height

        # 내 코드와 동일한 inner 감지
        inner_idxs = set()
        for i in range(len(all_tabs)):
            for j in range(len(all_tabs)):
                if i == j: continue
                if _bbox_area(tab_bboxes[j]) <= 0: continue
                if _bbox_area(tab_bboxes[j]) >= _bbox_area(tab_bboxes[i]) * 0.85: continue
                if _bbox_contains(tab_bboxes[i], tab_bboxes[j], tolerance=8.0):
                    inner_idxs.add(j)

        for i, tab in enumerate(all_tabs):
            bbox = tab_bboxes[i]
            reliable = _is_native_table_reliable(tab)
            is_inner = i in inner_idxs
            raw = tab.extract()
            filled = sum(1 for row in raw for c in (row or []) if c and str(c).strip()) if raw else 0
            total = (tab.row_count * tab.col_count) if raw else 0
            density = filled/total if total else 0
            print(f"  tab[{i}]: {tab.row_count}행×{tab.col_count}열  bbox_y=({bbox[1]:.1f}-{bbox[3]:.1f}/{page_h:.1f})  "
                  f"reliable={reliable}  inner={is_inner}  밀도={density:.2f}({filled}/{total})")
    except Exception as e:
        print(f"  find_tables 실패: {e}")
doc.close()
