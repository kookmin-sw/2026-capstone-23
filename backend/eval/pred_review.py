"""Prediction vs GT HTML preview for parser evaluation."""
import argparse
import html as html_module
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config import SUPPORTED_EXTENSIONS
from core.output import normalize_plain_text_output
from eval.metrics.cer import compute_cer
from eval.metrics.dpbench import compute_nid, compute_teds, compute_teds_s
from eval.metrics.table_match import compute_table_match
from eval.parsers.base import BaseEngineAdapter
from eval.schema import GroundTruth, ParsedDocument


def _load_gt(gt_dir: Path, file_name: str) -> Optional[GroundTruth]:
    stem = Path(file_name).stem
    candidates = [gt_dir / f"{stem}.json"]
    candidates.extend(gt_dir.rglob(f"{stem}.json"))
    for path in candidates:
        if path.exists():
            try:
                return GroundTruth(**json.loads(path.read_text(encoding="utf-8-sig")))
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] GT 로드 실패 ({path}): {exc}")
                return None
    return None


def _collect_files(input_dir: Path, match: Optional[str]) -> List[Path]:
    files = [
        p for p in sorted(input_dir.iterdir())
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if match:
        files = [p for p in files if match in p.name or match in p.stem]
    return files


def _build_adapter(engine: str, pipeline_model: str, openrouter_model: Optional[str]) -> BaseEngineAdapter:
    if engine == "pipeline":
        from eval.parsers.pipeline_engine import PipelineEngineAdapter

        return PipelineEngineAdapter(model=pipeline_model)
    if engine == "gpt":
        from eval.parsers.gpt_engine import GPTEngineAdapter

        return GPTEngineAdapter()
    if engine == "gpt-raw":
        from eval.parsers.gpt_raw_engine import GPTRawEngineAdapter

        return GPTRawEngineAdapter()
    if engine == "deepseek":
        from eval.parsers.deepseek_engine import DeepSeekEngineAdapter

        return DeepSeekEngineAdapter()
    if engine == "qwen":
        from eval.parsers.qwen_engine import QwenEngineAdapter

        return QwenEngineAdapter()
    if engine == "openrouter":
        from eval.parsers.openrouter_engine import OpenRouterEngineAdapter

        return OpenRouterEngineAdapter(model=openrouter_model)
    raise ValueError(f"Unsupported engine: {engine}")


def _safe_float(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return str(round(value, 4))


def _metrics(doc: ParsedDocument, gt: Optional[GroundTruth]) -> Dict[str, Any]:
    if gt is None or doc.error:
        return {"cer": None, "nid": None, "teds": None, "teds_s": None, "table_score": None}

    cer = None
    nid = None
    if gt.text:
        pred_text = normalize_plain_text_output(doc.text or "")
        gt_text = normalize_plain_text_output(gt.text or "")
        cer = compute_cer(pred_text, gt_text)
        nid = compute_nid(pred_text, gt_text)

    teds = None
    teds_s = None
    table_score = None
    if gt.tables:
        teds = compute_teds(doc.tables, gt.tables)
        teds_s = compute_teds_s(doc.tables, gt.tables)
        table_match = compute_table_match(doc.tables, gt.tables)
        table_score = table_match.get("score")

    return {
        "cer": cer,
        "nid": nid,
        "teds": teds,
        "teds_s": teds_s,
        "table_score": table_score,
    }


def _render_source(file_path: Path, output_path: Path) -> str:
    suffix = file_path.suffix.lower()
    rel = html_module.escape(os.path.relpath(file_path.resolve(), output_path.parent.resolve()))
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
        return f'<img src="{rel}" loading="lazy">'
    if suffix == ".pdf":
        return f'<iframe src="{rel}"></iframe>'
    return (
        f'<div class="file-box">'
        f'<b>{html_module.escape(file_path.name)}</b><br>'
        f'{file_path.stat().st_size:,} bytes<br>'
        f'<span>원본 미리보기는 PDF/이미지 파일만 직접 표시됩니다.</span>'
        f'</div>'
    )


def _render_tables(tables: List[str]) -> str:
    if not tables:
        return '<span class="empty">표 없음</span>'
    chunks = []
    for idx, table in enumerate(tables, 1):
        chunks.append(f'<div class="table-wrap"><span class="label">표 {idx}</span>{table}</div>')
    return "\n".join(chunks)


def _style() -> str:
    return """
<style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Malgun Gothic', sans-serif; margin: 18px; background: #f5f5f5; color: #111; }
h1 { margin: 0 0 12px; }
.meta { color: #666; margin-bottom: 20px; line-height: 1.5; }
.card { background: #fff; border-radius: 10px; padding: 18px; margin: 18px 0; box-shadow: 0 2px 10px rgba(0,0,0,.08); }
.grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 18px; align-items: start; }
h2 { font-size: 18px; margin: 0 0 12px; }
.metric { background: #eaf4ff; border: 1px solid #b9ddff; border-radius: 6px; padding: 8px; margin-bottom: 12px; font-weight: 600; }
img, iframe { width: 100%; border: 1px solid #ddd; border-radius: 6px; background: #fff; }
iframe { height: 640px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border: 1px solid #999; padding: 6px 8px; vertical-align: top; }
th { background: #e8e8e8; }
.table-wrap { border: 2px solid #48ad53; border-radius: 8px; padding: 10px; margin: 10px 0; overflow-x: auto; }
.label { display: inline-block; background: #48ad53; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin-bottom: 6px; }
pre { white-space: pre-wrap; word-break: break-word; background: #fafafa; border: 1px solid #ddd; border-radius: 8px; padding: 12px; max-height: 380px; overflow: auto; font-size: 13px; }
.empty { color: #888; font-style: italic; }
.file-box { border: 1px solid #ddd; border-radius: 8px; padding: 14px; background: #fafafa; line-height: 1.6; }
.error { color: #b00020; font-weight: 700; margin-bottom: 10px; }
</style>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Render prediction vs GT preview HTML")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--gt-dir", required=True, type=Path)
    parser.add_argument("--engine", default="pipeline", choices=["gpt", "gpt-raw", "pipeline", "deepseek", "qwen", "openrouter"])
    parser.add_argument("--pipeline-model", default="gpt-4o-mini")
    parser.add_argument("--openrouter-model", default=None)
    parser.add_argument("--match", default=None)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    files = _collect_files(args.input_dir, args.match)
    adapter = _build_adapter(args.engine, args.pipeline_model, args.openrouter_model)

    blocks = []
    for file_path in files:
        print(f"[INFO] Processing: {file_path.name}")
        gt = _load_gt(args.gt_dir, file_path.name)
        doc = adapter.parse(file_path)
        metrics = _metrics(doc, gt)
        metric_text = " | ".join(
            [
                f"CER: {_safe_float(metrics['cer'])}",
                f"NID: {_safe_float(metrics['nid'])}",
                f"TEDS: {_safe_float(metrics['teds'])}",
                f"TEDS-S: {_safe_float(metrics['teds_s'])}",
                f"구조점수: {_safe_float(metrics['table_score'])}",
            ]
        )

        gt_tables = gt.tables if gt and gt.tables else []
        gt_text = gt.text if gt and gt.text else ""
        pred_tables = doc.tables or []
        pred_text = doc.text or ""
        error_html = f'<div class="error">{html_module.escape(doc.error)}</div>' if doc.error else ""

        blocks.append(
            f"""
<div class="card">
  <div class="grid">
    <section>
      <h2>원본: {html_module.escape(file_path.name)}</h2>
      {_render_source(file_path, args.output)}
    </section>
    <section>
      <h2>GT: {html_module.escape((gt.file if gt else 'GT 없음'))}</h2>
      <h3>GT 표 ({len(gt_tables)}개)</h3>
      {_render_tables(gt_tables)}
      <h3>GT 텍스트 ({len(gt_text)}자)</h3>
      <pre>{html_module.escape(gt_text)}</pre>
    </section>
    <section>
      <h2>예측: {html_module.escape(adapter.engine_name)}</h2>
      <div class="metric">{html_module.escape(metric_text)}</div>
      {error_html}
      <h3>예측 표 ({len(pred_tables)}개)</h3>
      {_render_tables(pred_tables)}
      <h3>예측 텍스트 ({len(pred_text)}자)</h3>
      <pre>{html_module.escape(pred_text)}</pre>
    </section>
  </div>
</div>
"""
        )

    body = "\n".join(blocks) if blocks else '<div class="card">대상 파일이 없습니다.</div>'
    html = f"""<!doctype html>
<html lang="ko">
<head><meta charset="utf-8"><title>Prediction vs GT Review</title>{_style()}</head>
<body>
<h1>Prediction vs GT Review</h1>
<div class="meta">
input: {html_module.escape(args.input_dir.as_posix())}<br>
gt: {html_module.escape(args.gt_dir.as_posix())}<br>
engine: {html_module.escape(adapter.engine_name)}
</div>
{body}
</body>
</html>
"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"[INFO] HTML 저장 완료: {args.output}")


if __name__ == "__main__":
    main()
