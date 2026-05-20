"""단일 PDF 파싱 결과를 HTML로 저장해 브라우저에서 확인."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from core.pdf_extractor import extract_pdf

PDF = ROOT / "data/testdata/multi/multipage_table_test4.pdf"

if len(sys.argv) > 1:
    PDF = Path(sys.argv[1])

print(f"[파싱] {PDF.name}")
result = extract_pdf(PDF)

pages = result.get("pages", [])
print(f"[결과] {len(pages)} 페이지")

tables_html = []
for page in pages:
    for elem in page.get("elements", []):
        if elem.get("type") == "native_table":
            tables_html.append((page["page"], elem.get("html", "")))

print(f"[표] {len(tables_html)}개 native_table 발견")

# HTML 뷰어 생성
rows = []
for page_num, html in tables_html:
    rows.append(f"""
    <div style="margin:20px 0; border:2px solid #2196F3; border-radius:8px; padding:12px;">
      <div style="background:#2196F3;color:white;padding:4px 10px;border-radius:4px;display:inline-block;margin-bottom:8px;font-size:13px;">페이지 {page_num}</div>
      {html}
    </div>""")

output = ROOT / "check_parse_result.html"
output.write_text(f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><title>파싱 결과 확인 — {PDF.name}</title>
<style>
body{{font-family:-apple-system,'Malgun Gothic',sans-serif;max-width:1400px;margin:0 auto;padding:20px;background:#f5f5f5;}}
h1{{border-bottom:3px solid #333;padding-bottom:10px;}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0;}}
td,th{{border:1px solid #999;padding:6px 10px;text-align:left;vertical-align:top;}}
th{{background:#e8e8e8;font-weight:bold;}}
</style></head>
<body>
<h1>📄 파싱 결과 — {PDF.name}</h1>
<p>native_table {len(tables_html)}개</p>
{"".join(rows) if rows else "<p style='color:red'>표가 없습니다.</p>"}
</body></html>""", encoding="utf-8")

print(f"[저장] {output}")
