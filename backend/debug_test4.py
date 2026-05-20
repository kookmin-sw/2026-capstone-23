"""test4 페이지별 구조 상세 디버그."""
import sys
from pathlib import Path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import fitz
from core.pdf_extractor import _is_native_table_reliable, _bbox_area, _bbox_contains, _cluster_positions

PDF = ROOT / "data/testdata/multi/multipage_table_test4.pdf"
doc = fitz.open(PDF)

for page_idx in range(len(doc)):
    page = doc.load_page(page_idx)
    page_h = page.rect.height
    page_w = page.rect.width
    print(f"\n=== Page {page_idx+1} (height={page_h:.1f}) ===")

    # 텍스트 블록
    text_blocks = page.get_text("dict", sort=True)["blocks"]
    texts = []
    for b in text_blocks:
        if b.get("type") == 0:
            content = " ".join(
                span["text"] for line in b.get("lines", [])
                for span in line.get("spans", [])
            ).strip()
            if content:
                bbox = b.get("bbox", [0,0,0,0])
                texts.append((bbox[1], bbox[3], content[:60]))
    print(f"  텍스트 {len(texts)}개:")
    for y0, y1, t in texts[:8]:
        print(f"    y={y0:.0f}-{y1:.0f}: {t}")
    if len(texts) > 8:
        print(f"    ... +{len(texts)-8}개")

    # native tables
    try:
        fitz_tabs = page.find_tables()
        all_tabs = list(fitz_tabs.tables)
        tab_bboxes = [list(tab.bbox) for tab in all_tabs]

        inner_idxs = set()
        for i in range(len(all_tabs)):
            for j in range(len(all_tabs)):
                if i == j: continue
                if _bbox_area(tab_bboxes[j]) <= 0: continue
                if _bbox_area(tab_bboxes[j]) >= _bbox_area(tab_bboxes[i]) * 0.85: continue
                if _bbox_contains(tab_bboxes[i], tab_bboxes[j], tolerance=8.0):
                    inner_idxs.add(j)

        print(f"  find_tables() -> {len(all_tabs)}개")
        for i, tab in enumerate(all_tabs):
            bbox = tab_bboxes[i]
            reliable = _is_native_table_reliable(tab)
            is_inner = i in inner_idxs
            raw = tab.extract()
            print(f"  tab[{i}]: {tab.row_count}r×{tab.col_count}c  y=({bbox[1]:.0f}-{bbox[3]:.0f}/{page_h:.0f})  x=({bbox[0]:.0f}-{bbox[2]:.0f}/{page_w:.0f})  reliable={reliable}  inner={is_inner}")
            if raw and not is_inner:
                for r_idx, row in enumerate(raw[:3]):
                    cells = [str(c or "")[:20] for c in row]
                    print(f"    row[{r_idx}]: {cells}")
    except Exception as e:
        print(f"  find_tables 실패: {e}")

doc.close()
