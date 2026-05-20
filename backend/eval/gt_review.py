"""GT 검수용 HTML 뷰어 생성 스크립트. 원본 이미지/파일과 GT를 나란히 표시."""
import json
import html as html_module
from pathlib import Path

BASE = Path(__file__).resolve().parent
GT_ROOT = BASE / "gt"
# 우선순위:
# 1) eval/data/input  (팀 공용 eval 실행 경로)
# 2) data/testdata    (기존 로컬 테스트셋 경로)
TESTDATA_ROOT = (
    BASE / "data" / "input"
    if (BASE / "data" / "input").exists()
    else BASE.parent / "data" / "testdata"
)
OUTPUT = BASE / "gt_review.html"


def build_section(category: str) -> str:
    gt_dir = GT_ROOT / category
    testdata_dir = TESTDATA_ROOT / category
    if not gt_dir.exists():
        return ""

    parts = [f'<h2 id="{category}">{category.upper()}</h2>']

    for f in sorted(gt_dir.glob("*.json")):
        # 10MB 이상 파일은 건너뛰기 (브라우저 멈춤 방지)
        if f.stat().st_size > 10 * 1024 * 1024:
            parts.append(f'<div class="pair"><div class="original"><h3>⚠️ {f.name}</h3><p class="note">파일 크기 {f.stat().st_size // (1024*1024)}MB — 리뷰에서 제외 (직접 확인 필요)</p></div></div>')
            continue

        try:
            gt = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            parts.append(f'<div class="error">❌ {f.name}: {e}</div>')
            continue

        filename = gt.get("file", "")
        note = gt.get("note", "")
        text = gt.get("text", "")
        tables = gt.get("tables") or []
        has_multipage = gt.get("has_multipage_table", False)

        # 이미지/파일 경로
        src_file = testdata_dir / filename
        is_image = src_file.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp"}

        parts.append('<div class="pair">')

        # 왼쪽: 원본
        parts.append('<div class="original">')
        parts.append(f'<h3>📄 {filename}</h3>')
        if note:
            parts.append(f'<p class="note">{html_module.escape(note)}</p>')
        if is_image and src_file.exists():
            rel_path = "../" + str(src_file.relative_to(BASE.parent))
            parts.append(f'<img src="{rel_path}" loading="lazy">')
        elif src_file.exists():
            parts.append(f'<p class="file-info">파일 크기: {src_file.stat().st_size:,} bytes</p>')
        else:
            parts.append('<p class="file-info">⚠️ 원본 파일 없음</p>')
        parts.append('</div>')

        # 오른쪽: GT
        parts.append('<div class="gt">')
        parts.append(f'<h3>GT ({f.name})</h3>')

        if tables:
            max_tables = 3 if category == "excel" else len(tables)
            shown = tables[:max_tables]
            parts.append(f'<h4>표 ({len(tables)}개{", " + str(max_tables) + "개만 표시" if len(tables) > max_tables else ""})</h4>')
            for i, t in enumerate(shown):
                # 표 HTML이 너무 길면 잘라서 표시
                if len(t) > 5000:
                    t = t[:5000] + '...</table>'
                parts.append(f'<div class="table-wrap"><span class="table-label">표 {i+1}</span>{t}</div>')

        text_preview = text[:1000]
        if len(text) > 1000:
            text_preview += f"\n\n... ({len(text):,}자 중 1000자 표시)"
        parts.append(f'<h4>텍스트 ({len(text):,}자)</h4>')
        parts.append(f'<pre class="text-preview">{html_module.escape(text_preview)}</pre>')

        if has_multipage:
            parts.append('<span class="badge">다중 페이지 표</span>')


        parts.append('</div>')  # .gt
        parts.append('</div>')  # .pair

    return "\n".join(parts)


def main():
    categories = ["table", "flow", "chart", "excel", "documents"]

    nav_links = " | ".join(
        f'<a href="#{c}">{c.upper()}</a>' for c in categories
    )

    # 통계
    stats = []
    for c in categories:
        gt_dir = GT_ROOT / c
        count = len(list(gt_dir.glob("*.json"))) if gt_dir.exists() else 0
        stats.append(f"{c}: {count}개")

    sections = "\n".join(build_section(c) for c in categories)

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>Luminir GT 검수 뷰어</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, 'Malgun Gothic', sans-serif; max-width: 1600px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
h1 {{ border-bottom: 3px solid #333; padding-bottom: 10px; }}
h2 {{ background: #333; color: white; padding: 10px 20px; border-radius: 8px; margin-top: 40px; }}
.nav {{ position: sticky; top: 0; background: white; padding: 10px 20px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); z-index: 100; margin-bottom: 20px; font-size: 16px; }}
.stats {{ color: #666; font-size: 14px; margin-top: 5px; }}
.pair {{ display: flex; gap: 20px; margin: 20px 0; background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
.original {{ flex: 1; min-width: 0; }}
.gt {{ flex: 1; min-width: 0; }}
img {{ max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 4px; }}
table {{ border-collapse: collapse; margin: 10px 0; font-size: 13px; width: 100%; }}
td, th {{ border: 1px solid #999; padding: 6px 8px; text-align: left; }}
th {{ background: #e8e8e8; font-weight: bold; }}
.table-wrap {{ border: 2px solid #4CAF50; border-radius: 8px; padding: 10px; margin: 8px 0; overflow-x: auto; }}
.table-label {{ background: #4CAF50; color: white; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
.text-preview {{ background: #f8f8f8; padding: 12px; border-radius: 8px; font-size: 12px; white-space: pre-wrap; word-break: break-all; max-height: 300px; overflow-y: auto; border: 1px solid #ddd; }}
.note {{ color: #e67e22; font-weight: bold; }}
.badge {{ background: #3498db; color: white; padding: 4px 12px; border-radius: 12px; font-size: 12px; }}
.file-info {{ color: #999; font-style: italic; }}
.error {{ color: red; padding: 10px; background: #fee; border-radius: 8px; }}
.review-box {{ margin-top: 12px; padding: 12px; background: #fffde7; border-radius: 8px; border: 1px solid #f0e68c; }}
.review-box label {{ margin-right: 16px; cursor: pointer; }}
.review-box textarea {{ display: block; width: 100%; margin-top: 8px; padding: 8px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px; resize: vertical; min-height: 40px; }}
.chk-ok:checked + span, label:has(.chk-ok:checked) {{ color: green; font-weight: bold; }}
.chk-fix:checked + span, label:has(.chk-fix:checked) {{ color: orange; font-weight: bold; }}
</style>
</head>
<body>
<h1>Luminir GT 검수 뷰어</h1>
<div class="nav">
    📂 {nav_links}
    <div class="stats">{' | '.join(stats)}</div>
</div>
{sections}
</body>
</html>"""

    OUTPUT.write_text(html, encoding="utf-8")
    print(f" 생성 완료: {OUTPUT}")
    print(f"   브라우저에서 열기: open {OUTPUT}")


if __name__ == "__main__":
    main()
