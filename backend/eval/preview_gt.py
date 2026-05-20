"""Generate a static GT preview HTML.

Typical use:
  .venv/bin/python eval/preview_gt.py --open
  .venv/bin/python eval/preview_gt.py --sync-table4 --open
  .venv/bin/python eval/preview_gt.py --sync-html-tables --open
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote


BASE = Path(__file__).resolve().parent.parent
DEFAULT_GT = BASE / "eval/gt/documents/(견적공고문) 충남디자인예술고등학교 그린스마트스쿨 가설공사.json"
DEFAULT_PDF = Path("/Users/seungzun/Desktop/(견적공고문) 충남디자인예술고등학교 그린스마트스쿨 가설공사.pdf")
DEFAULT_OUT = Path("/private/tmp/lemong_gt_preview.html")


def split_tables(markup: str) -> list[str]:
    return [m.group(0).strip() for m in re.finditer(r"<table\b.*?</table>", markup, re.S | re.I)]


def load_gt(gt_path: Path) -> dict:
    return json.loads(gt_path.read_text(encoding="utf-8"))


def save_gt(gt_path: Path, gt: dict) -> None:
    gt_path.write_text(json.dumps(gt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sync_table4(gt_path: Path, table4_path: Path) -> None:
    tables = split_tables(table4_path.read_text(encoding="utf-8"))
    if len(tables) != 2:
        raise ValueError(f"{table4_path} must contain exactly 2 <table> blocks, found {len(tables)}")

    gt = load_gt(gt_path)
    gt_tables = gt.get("tables") or []
    gt["tables"] = gt_tables[:3] + tables + gt_tables[5:]
    save_gt(gt_path, gt)


def sync_table(gt_path: Path, html_path: Path, table_index: int) -> None:
    tables = split_tables(html_path.read_text(encoding="utf-8"))
    if len(tables) != 1:
        raise ValueError(f"{html_path} must contain exactly 1 <table> block, found {len(tables)}")

    gt = load_gt(gt_path)
    gt_tables = gt.get("tables") or []
    if table_index >= len(gt_tables):
        raise IndexError(f"tables[{table_index}] does not exist in {gt_path}")
    gt_tables[table_index] = tables[0]
    gt["tables"] = gt_tables
    save_gt(gt_path, gt)


def sync_html_tables(gt_path: Path) -> None:
    table4_path = gt_path.with_suffix(".table4.html")
    table5_path = gt_path.with_suffix(".table5.html")
    table6_path = gt_path.with_suffix(".table6.html")

    if table4_path.exists():
        sync_table4(gt_path, table4_path)
    if table5_path.exists():
        sync_table(gt_path, table5_path, 4)
    if table6_path.exists():
        sync_table(gt_path, table6_path, 5)


def render_preview(gt_path: Path, pdf_path: Path, out_path: Path) -> None:
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    tables = gt.get("tables") or []
    text = gt.get("text") or ""
    text_preview = text[:4000]
    if len(text) > 4000:
        text_preview += f"\n\n... ({len(text):,}자 중 4000자 표시)"

    table_blocks = "".join(
        f'<section class="table-block"><div class="table-title">표 {i}</div>{table}</section>'
        for i, table in enumerate(tables, 1)
    )

    pdf_frame = f'<iframe src="file://{quote(str(pdf_path))}"></iframe>' if pdf_path.exists() else "<p>원본 PDF 없음</p>"

    out_path.write_text(
        f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>GT Preview</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;color:#1f2933;background:#f4f6f8}}
header{{height:52px;display:flex;align-items:center;gap:16px;padding:0 18px;background:#17212b;color:white}}
header .name{{font-size:14px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
header .meta{{margin-left:auto;font-size:12px;color:#c8d1dc}}
main{{display:grid;grid-template-columns:minmax(420px,48vw) 1fr;height:calc(100vh - 52px)}}
.pdf{{background:#20252b;padding:12px}}
iframe{{width:100%;height:100%;border:0;background:white}}
.gt{{overflow:auto;padding:16px 18px 32px}}
.panel{{background:white;border:1px solid #d9e0e7;border-radius:8px;padding:14px;margin-bottom:14px}}
h2{{font-size:15px;margin:0 0 10px}}
.badge{{display:inline-block;margin-right:8px;padding:3px 8px;border:1px solid #cbd5df;border-radius:999px;background:#f8fafc;font-size:12px;color:#52616f}}
.table-block{{background:white;border:2px solid #2f855a;border-radius:8px;padding:10px;margin-bottom:14px;overflow-x:auto}}
.table-title{{display:inline-block;margin-bottom:8px;padding:3px 8px;border-radius:4px;background:#2f855a;color:white;font-size:12px;font-weight:700}}
table{{border-collapse:collapse;width:100%;min-width:520px;font-size:13px}}
td,th{{border:1px solid #8795a1;padding:6px 8px;vertical-align:top}}
pre{{margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.55;max-height:360px;overflow:auto;background:#f8fafc;border:1px solid #d9e0e7;border-radius:6px;padding:10px}}
</style>
</head>
<body>
<header><div class="name">{html.escape(gt.get("file", ""))}</div><div class="meta">GT preview</div></header>
<main>
  <div class="pdf">{pdf_frame}</div>
  <div class="gt">
    <div class="panel">
      <h2>GT 요약</h2>
      <span class="badge">text {len(text):,}자</span>
      <span class="badge">tables {len(tables)}개</span>
      <span class="badge">multipage {gt.get("has_multipage_table")}</span>
      <span class="badge">gt: {html.escape(str(gt_path))}</span>
    </div>
    {table_blocks}
    <div class="panel"><h2>텍스트 미리보기</h2><pre>{html.escape(text_preview)}</pre></div>
  </div>
</main>
</body>
</html>
""",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gt", type=Path, default=DEFAULT_GT)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--sync-table4", action="store_true", help="Sync sibling .table4.html into tables[3:5] before preview")
    parser.add_argument("--sync-table5", action="store_true", help="Sync sibling .table5.html into tables[4] before preview")
    parser.add_argument("--sync-table6", action="store_true", help="Sync sibling .table6.html into tables[5] before preview")
    parser.add_argument("--sync-html-tables", action="store_true", help="Sync sibling .table4.html, .table5.html, and .table6.html if present")
    parser.add_argument("--open", action="store_true", help="Open generated preview in the default browser")
    args = parser.parse_args()

    gt_path = args.gt.resolve()
    if args.sync_html_tables:
        sync_html_tables(gt_path)
    elif args.sync_table4:
        table4_path = gt_path.with_suffix(".table4.html")
        sync_table4(gt_path, table4_path)
    elif args.sync_table5:
        table5_path = gt_path.with_suffix(".table5.html")
        sync_table(gt_path, table5_path, 4)
    elif args.sync_table6:
        table6_path = gt_path.with_suffix(".table6.html")
        sync_table(gt_path, table6_path, 5)

    render_preview(gt_path, args.pdf.expanduser().resolve(), args.out)
    print(args.out)

    if args.open:
        subprocess.run(["open", str(args.out)], check=True)


if __name__ == "__main__":
    main()
