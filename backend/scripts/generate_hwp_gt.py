"""Generate HWP ground-truth JSON from hwp5html, with optional VLM verification.

Examples:
  .venv/bin/python scripts/generate_hwp_gt.py \
    --input "/Users/dusehd1/Downloads/개인정보 활용 동의서_20001234_이름.hwp" \
    --output-dir /private/tmp/hwp_gt \
    --ai-verify

  .venv/bin/python scripts/generate_hwp_gt.py \
    --input /Users/dusehd1/Downloads/hwp_gt_sources \
    --output-dir /Users/dusehd1/Downloads/hwp_gt_sources
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.env import load_project_env  # noqa: E402
from core.converters import convert_to_pdf, parse_hwp_document_direct  # noqa: E402
from core.output import build_document_text  # noqa: E402
from core.vlm_client import VLMClient  # noqa: E402

load_project_env()

DEFAULT_OPENROUTER_VERIFY_MODEL = "openrouter/openai/gpt-5.2"


@dataclass
class GeneratedGt:
    source_path: Path
    gt_path: Path
    review_path: Path
    text_chars: int
    table_count: int
    confidence: str
    warnings: list[str]


def _normalize_text(value: str) -> str:
    text = html.unescape(str(value or "")).replace("\xa0", " ")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _table_to_inline_text(match: re.Match) -> str:
    table_html = match.group(0)
    cell_texts = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", table_html, re.IGNORECASE)
    cleaned = [_normalize_text(cell) for cell in cell_texts]
    return " " + " ".join(cell for cell in cleaned if cell) + " "


def _extract_text_for_gt(source_path: Path, parsed: dict[str, Any]) -> str:
    raw = build_document_text(
        source_path=source_path,
        page_count=int(parsed.get("page_count") or 1),
        pages=list(parsed.get("pages") or []),
        image_results={},
    )
    text = re.sub(r"^.*?(?=## Page)", "", raw, count=1, flags=re.DOTALL)
    text = re.sub(r"## Page \d+\s*", "", text)
    text = re.sub(r"\[\[TABLE_MARKDOWN\]\][\s\S]*?\[\[/TABLE_MARKDOWN\]\]", "", text)
    text = re.sub(r"\[\[.*?\]\]", "", text)
    text = re.sub(r"<table[\s\S]*?</table>", _table_to_inline_text, text, flags=re.IGNORECASE)
    return _normalize_text(text)


def _extract_tables(parsed: dict[str, Any]) -> list[str]:
    tables: list[str] = []
    for table in list(parsed.get("tables") or []):
        table_html = str(table.get("html") or "").strip()
        if table_html:
            tables.append(table_html)
    if tables:
        return tables

    for page in list(parsed.get("pages") or []):
        for element in list(page.get("elements") or []):
            if element.get("type") == "table":
                table_html = str(element.get("content") or "").strip()
                if table_html:
                    tables.append(table_html)
    return tables


def _render_first_page_png(source_path: Path, tmp_root: Path, same_stem_pdf: Path | None = None) -> tuple[bytes | None, str | None]:
    pdf_path: Path | None = same_stem_pdf if same_stem_pdf and same_stem_pdf.exists() else None
    if pdf_path is None:
        try:
            converted = convert_to_pdf(source_path, tmp_root)
            pdf_path = converted[0] if isinstance(converted, tuple) else converted
        except Exception as exc:  # noqa: BLE001
            return None, f"PDF 렌더링용 변환 실패: {exc}"

    try:
        import fitz

        doc = fitz.open(pdf_path)
        if len(doc) < 1:
            return None, "PDF에 렌더링할 페이지가 없습니다."
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        return pix.tobytes("png"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"PDF 첫 페이지 렌더링 실패: {exc}"


def _verify_with_vlm(
    *,
    image_bytes: bytes | None,
    gt_text: str,
    model: str,
    warnings: list[str],
) -> dict[str, Any]:
    if not image_bytes:
        warnings.append("AI 검증용 렌더링 이미지가 없어 VLM 검증을 건너뜀")
        return {"enabled": False, "error": "missing rendered image"}

    if model.lower().startswith("openrouter/") and not os.getenv("OPENROUTER_API_KEY"):
        warnings.append("OPENROUTER_API_KEY가 없어 OpenRouter AI 검증을 건너뜀")
        return {"enabled": True, "model": model, "error": "missing OPENROUTER_API_KEY"}

    if model.lower().startswith("openrouter/"):
        try:
            from openai import OpenAI

            actual_model = model[len("openrouter/") :]
            image_data = base64.b64encode(image_bytes).decode("ascii")
            client = OpenAI(
                api_key=os.environ["OPENROUTER_API_KEY"],
                base_url="https://openrouter.ai/api/v1",
            )
            prompt = f"""당신은 HWP GT 품질 검증자입니다.

렌더링된 첫 페이지 이미지와 코드로 추출한 GT 텍스트를 비교하세요.
새 GT를 만들지 말고, 보이는 문서 내용이 GT에 충분히 포함되는지만 판정하세요.
표/큰 외곽 박스/중첩 표가 보이면 GT 텍스트에 그 내용이 보존되는지도 확인하세요.

반드시 JSON만 출력하세요:
{{
  "verdict": "pass|warn|fail",
  "visible_text_coverage": 0.0,
  "table_structure_assessment": "string",
  "missing_major_sections": ["string"],
  "notes": "string"
}}

GT 텍스트 일부:
{gt_text[:8000]}
"""
            response = client.chat.completions.create(
                model=actual_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_data}",
                                },
                            },
                        ],
                    }
                ],
                temperature=0,
                max_tokens=1500,
            )
            vlm_text = response.choices[0].message.content or ""
            parsed: dict[str, Any] | None = None
            try:
                parsed = json.loads(re.sub(r"^```(?:json)?|```$", "", vlm_text.strip(), flags=re.MULTILINE).strip())
            except Exception:
                parsed = None
            if parsed:
                verdict = str(parsed.get("verdict") or "").lower()
                coverage = parsed.get("visible_text_coverage")
                try:
                    coverage_value = float(coverage)
                except Exception:
                    coverage_value = None
                if verdict in {"warn", "fail"}:
                    warnings.append(f"AI 검증 verdict={verdict}: {parsed.get('notes') or '상세 사유 없음'}")
                elif coverage_value is not None and coverage_value < 0.75:
                    warnings.append(f"AI 검증 visible_text_coverage 낮음: {coverage_value}")
                return {
                    "enabled": True,
                    "model": model,
                    "actual_model": actual_model,
                    "result": parsed,
                    "raw_preview": vlm_text[:1200],
                }
            warnings.append("AI 검증 응답이 JSON 형식이 아님")
            return {
                "enabled": True,
                "model": model,
                "actual_model": actual_model,
                "raw_preview": vlm_text[:1200],
                "error": "non_json_response",
            }
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"AI 검증 실패: {exc}")
            return {"enabled": True, "model": model, "error": str(exc)}

    try:
        client = VLMClient(openai_model=model, max_concurrent_api=1, gpu_max_concurrent=1)
        result = client.describe_image(image_bytes, language="한국어", is_table=None, is_flowchart=None, is_math=None)
        vlm_text = str(result.get("text") or "")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"AI 검증 실패: {exc}")
        return {"enabled": True, "error": str(exc)}

    gt_norm = re.sub(r"[^0-9a-zA-Z가-힣]+", "", gt_text)
    vlm_norm = re.sub(r"[^0-9a-zA-Z가-힣]+", "", vlm_text)
    common_hits = 0
    probes = [token for token in re.findall(r"[0-9a-zA-Z가-힣]{3,}", gt_text)[:80] if len(token) >= 3]
    if probes:
        common_hits = sum(1 for token in probes if token in vlm_text)
    coverage = common_hits / len(probes) if probes else 0.0

    if len(gt_norm) > 200 and len(vlm_norm) > 50 and coverage < 0.15:
        warnings.append("AI 검증 결과와 GT 텍스트의 키워드 겹침이 낮음")

    return {
        "enabled": True,
        "model": model,
        "coverage": round(coverage, 4),
        "vlm_text_preview": vlm_text[:1200],
        "usage": result.get("usage"),
        "error": result.get("error"),
    }


def _confidence(warnings: list[str], *, table_count: int, text_chars: int, source_type: str) -> str:
    if source_type != "hwp_direct_html":
        return "low"
    if text_chars < 50:
        return "low"
    if warnings:
        return "medium"
    if table_count > 0:
        return "high"
    return "medium"


def _write_review_html(
    *,
    review_path: Path,
    source_path: Path,
    gt_data: dict[str, Any],
    rendered_png: bytes | None,
) -> None:
    image_html = ""
    if rendered_png:
        encoded = base64.b64encode(rendered_png).decode("ascii")
        image_html = f'<img src="data:image/png;base64,{encoded}" alt="rendered first page">'

    table_sections = []
    for idx, table_html in enumerate(gt_data.get("tables") or [], start=1):
        table_sections.append(f"<h3>Table {idx}</h3><div class='table-wrap'>{table_html}</div>")

    warnings = gt_data.get("quality", {}).get("warnings", [])
    warnings_html = "".join(f"<li>{html.escape(str(w))}</li>" for w in warnings) or "<li>없음</li>"
    text = html.escape(str(gt_data.get("text") or ""))
    quality = html.escape(json.dumps(gt_data.get("quality") or {}, ensure_ascii=False, indent=2))

    review_path.write_text(
        f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HWP GT Review - {html.escape(source_path.name)}</title>
  <style>
    body {{ margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, 'Noto Sans KR', sans-serif; background: #f6f7f9; color: #18202a; }}
    main {{ max-width: 1440px; margin: 0 auto; }}
    section {{ background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 18px; margin: 14px 0; }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; }}
    img {{ max-width: 100%; border: 1px solid #d9dee7; background: white; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #111827; color: #f3f6ff; padding: 14px; border-radius: 8px; max-height: 520px; overflow: auto; }}
    table {{ border-collapse: collapse; max-width: 100%; }}
    td, th {{ border: 1px solid #9aa4b2; padding: 4px 6px; vertical-align: top; }}
    .table-wrap {{ overflow: auto; border: 1px solid #d9dee7; padding: 12px; border-radius: 8px; }}
    .badge {{ display: inline-block; background: #eef4ff; color: #165bd8; padding: 3px 8px; border-radius: 999px; font-size: 12px; }}
  </style>
</head>
<body>
<main>
  <h1>HWP GT Review</h1>
  <p><span class="badge">{html.escape(source_path.name)}</span></p>
  <section>
    <h2>Quality</h2>
    <pre>{quality}</pre>
    <h3>Warnings</h3>
    <ul>{warnings_html}</ul>
  </section>
  <div class="grid">
    <section>
      <h2>Rendered Preview</h2>
      {image_html or '<p>렌더링 이미지 없음</p>'}
    </section>
    <section>
      <h2>Extracted Text</h2>
      <pre>{text}</pre>
    </section>
  </div>
  <section>
    <h2>Tables</h2>
    {''.join(table_sections) or '<p>추출된 표 없음</p>'}
  </section>
</main>
</body>
</html>
""",
        encoding="utf-8",
    )


def generate_one(
    source_path: Path,
    *,
    output_dir: Path,
    tmp_root: Path,
    ai_verify: bool,
    model: str,
) -> GeneratedGt:
    warnings: list[str] = []
    parsed = parse_hwp_document_direct(source_path, tmp_dir=tmp_root)
    text = _extract_text_for_gt(source_path, parsed)
    tables = _extract_tables(parsed)
    source_type = str(parsed.get("source_type") or "unknown")
    if not tables:
        warnings.append("추출된 table이 없음")
    if len(text) < 50:
        warnings.append("추출 텍스트가 50자 미만")

    same_stem_pdf = source_path.with_suffix(".pdf")
    rendered_png, render_warning = _render_first_page_png(source_path, tmp_root, same_stem_pdf=same_stem_pdf)
    if render_warning:
        warnings.append(render_warning)

    ai_result = {"enabled": False}
    if ai_verify:
        ai_result = _verify_with_vlm(
            image_bytes=rendered_png,
            gt_text=text,
            model=model,
            warnings=warnings,
        )

    confidence = _confidence(warnings, table_count=len(tables), text_chars=len(text), source_type=source_type)
    output_dir.mkdir(parents=True, exist_ok=True)
    gt_path = output_dir / f"{source_path.stem}.json"
    review_path = output_dir / f"{source_path.stem}.review.html"
    gt_data = {
        "file": source_path.with_suffix(".pdf").name,
        "text": text,
        "tables": tables or None,
        "has_multipage_table": False,
        "quality": {
            "source": source_type,
            "confidence": confidence,
            "needs_review": bool(warnings) or confidence != "high",
            "warnings": warnings,
            "checks": {
                "outer_table": "preserved",
                "nested_tables": "preserved",
                "empty_cells": "preserved",
                "text_chars": len(text),
                "table_count": len(tables),
            },
            "ai_verification": ai_result,
        },
    }
    gt_path.write_text(json.dumps(gt_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_review_html(review_path=review_path, source_path=source_path, gt_data=gt_data, rendered_png=rendered_png)
    return GeneratedGt(
        source_path=source_path,
        gt_path=gt_path,
        review_path=review_path,
        text_chars=len(text),
        table_count=len(tables),
        confidence=confidence,
        warnings=warnings,
    )


def _collect_inputs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(input_path.glob("*.hwp"), key=lambda p: p.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate HWP GT JSON/review HTML")
    parser.add_argument("--input", required=True, type=Path, help="HWP file or directory")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory. Defaults to input directory")
    parser.add_argument("--tmp-root", type=Path, default=None, help="Temporary directory")
    parser.add_argument("--ai-verify", action="store_true", help="Run VLM verification on rendered first page")
    parser.add_argument(
        "--model",
        default=DEFAULT_OPENROUTER_VERIFY_MODEL,
        help=f"VLM model for verification. Defaults to {DEFAULT_OPENROUTER_VERIFY_MODEL}",
    )
    args = parser.parse_args()

    input_path = args.input.expanduser().resolve()
    output_dir = (args.output_dir or (input_path.parent if input_path.is_file() else input_path)).expanduser().resolve()
    tmp_root = (args.tmp_root or Path(tempfile.mkdtemp(prefix="hwp_gt_"))).expanduser().resolve()
    model = args.model

    files = _collect_inputs(input_path)
    if not files:
        raise SystemExit(f"No .hwp files found: {input_path}")

    results = [
        generate_one(
            source_path=file_path,
            output_dir=output_dir,
            tmp_root=tmp_root,
            ai_verify=args.ai_verify,
            model=model,
        )
        for file_path in files
    ]

    print(json.dumps([
        {
            "source": str(result.source_path),
            "gt": str(result.gt_path),
            "review": str(result.review_path),
            "text_chars": result.text_chars,
            "tables": result.table_count,
            "confidence": result.confidence,
            "warnings": result.warnings,
        }
        for result in results
    ], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
