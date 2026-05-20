from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import fitz
import requests
from openai import OpenAI

from core.env import env_str


DEFAULT_INPUT = next((PROJECT_ROOT / "data" / "inputs").glob("B1-ICT-*.pdf"))
DEFAULT_PIPELINE_REPORT = PROJECT_ROOT / "reports" / "api_conversion_20260515_codex_5repeat"

MODEL_CONFIGS: dict[str, dict[str, str]] = {
    "gpt-5.2": {
        "provider": "openrouter",
        "api_model": "openai/gpt-5.2",
        "pipeline_model": "openrouter/openai/gpt-5.2",
        "pipeline_run": "gpt_5_2_p1",
    },
    "qwen3-vl-8b": {
        "provider": "openrouter",
        "api_model": "qwen/qwen3-vl-8b-instruct",
        "pipeline_model": "openrouter/qwen3-vl-8b",
        "pipeline_run": "qwen3_vl_8b_p1",
    },
    "qwen3-vl-32b": {
        "provider": "openrouter",
        "api_model": "qwen/qwen3-vl-32b-instruct",
        "pipeline_model": "openrouter/qwen3-vl-32b",
        "pipeline_run": "qwen3_vl_32b_p1",
    },
}

DIRECT_PROMPT = """PDF page image를 구조화된 텍스트로 변환하라.

규칙:
- 페이지에서 보이는 텍스트를 빠짐없이 출력한다.
- 표가 있으면 반드시 HTML <table>, <tr>, <th>, <td> 구조로 출력한다.
- 행/열 병합이 보이면 rowspan/colspan을 사용한다.
- 표를 단순 공백 텍스트나 Markdown 표로만 출력하지 않는다.
- 이미지/그림/도식이 있으면 [[IMAGE]] ... [[/IMAGE]] 블록으로 간단히 설명한다.
- 코드 펜스와 분석 과정은 출력하지 않는다.
"""


class _StructureCounter(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.table_count = 0
        self.row_count = 0
        self.cell_count = 0
        self._table_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self.table_count += 1
            self._table_depth += 1
            return
        if self._table_depth <= 0:
            return
        if tag == "tr":
            self.row_count += 1
        elif tag in {"td", "th"}:
            self.cell_count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "table" and self._table_depth > 0:
            self._table_depth -= 1


@dataclass
class StructureMetrics:
    char_count: int
    table_count: int
    row_count: int
    cell_count: int
    image_count: int


@dataclass
class RunRecord:
    model: str
    mode: str
    repeat: int
    output_path: str
    elapsed_sec: float | None
    error: str | None
    char_count: int
    table_count: int
    row_count: int
    cell_count: int
    image_count: int


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", value).strip("_")


def _extract_content_text(text: str) -> str:
    return text.strip()


def count_structure(text: str) -> StructureMetrics:
    content = _extract_content_text(text)
    parser = _StructureCounter()
    parser.feed(content)
    return StructureMetrics(
        char_count=len(content),
        table_count=parser.table_count,
        row_count=parser.row_count,
        cell_count=parser.cell_count,
        image_count=len(re.findall(r"\[\[IMAGE\]\]", content, flags=re.IGNORECASE)),
    )


def _find_pipeline_output(
    pipeline_report: Path,
    model: str,
    repeat: int,
    copy_index: int,
) -> Path | None:
    run_name = MODEL_CONFIGS[model]["pipeline_run"]
    run_dir = pipeline_report / f"repeat_{repeat:02d}" / "runs" / run_name
    if not run_dir.exists():
        return None
    matches = sorted(run_dir.rglob(f"*copy{copy_index}.txt"))
    return matches[0] if matches else None


def collect_pipeline_records(
    *,
    pipeline_report: Path,
    models: list[str],
    repeats: int,
    copy_index: int,
) -> list[RunRecord]:
    records: list[RunRecord] = []
    for model in models:
        for repeat in range(1, repeats + 1):
            output_path = _find_pipeline_output(pipeline_report, model, repeat, copy_index)
            if output_path is None:
                records.append(
                    RunRecord(
                        model=model,
                        mode="P",
                        repeat=repeat,
                        output_path="",
                        elapsed_sec=None,
                        error=f"pipeline output not found for repeat={repeat}",
                        char_count=0,
                        table_count=0,
                        row_count=0,
                        cell_count=0,
                        image_count=0,
                    )
                )
                continue
            text = output_path.read_text(encoding="utf-8", errors="replace")
            metrics = count_structure(text)
            records.append(
                RunRecord(
                    model=model,
                    mode="P",
                    repeat=repeat,
                    output_path=str(output_path),
                    elapsed_sec=None,
                    error=None,
                    **asdict(metrics),
                )
            )
    return records


def run_pipeline_once(
    *,
    input_file: Path,
    output_dir: Path,
    model: str,
    repeat: int,
    max_workers: int,
) -> RunRecord:
    from core.config import load_config
    from core.pipeline import DocumentPipeline

    model_config = MODEL_CONFIGS[model]
    pipeline_model = model_config["pipeline_model"]
    model_dir = output_dir / "pipeline_outputs" / _safe_name(model)
    result_dir = model_dir / f"repeat_{repeat:02d}" / "outputs"
    tmp_dir = model_dir / f"repeat_{repeat:02d}" / "tmp"
    result_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(result_dir.rglob("*.txt"))
    if existing:
        output_path = existing[0]
        text = output_path.read_text(encoding="utf-8", errors="replace")
        metrics = count_structure(text)
        return RunRecord(
            model=model,
            mode="P",
            repeat=repeat,
            output_path=str(output_path),
            elapsed_sec=None,
            error=None,
            **asdict(metrics),
        )

    started = time.perf_counter()
    try:
        config = load_config()
        config.openai_model = pipeline_model
        config.output_root = result_dir
        config.tmp_root = tmp_dir
        config.vlm_device = "api"
        config.max_workers = max_workers
        config.vlm_max_concurrent = max_workers

        pipeline = DocumentPipeline(config)
        output_path = pipeline.process_file(input_file, language="ko")
        elapsed = round(time.perf_counter() - started, 3)
        text = output_path.read_text(encoding="utf-8", errors="replace")
        metrics = count_structure(text)
        return RunRecord(
            model=model,
            mode="P",
            repeat=repeat,
            output_path=str(output_path),
            elapsed_sec=elapsed,
            error=None,
            **asdict(metrics),
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = round(time.perf_counter() - started, 3)
        return RunRecord(
            model=model,
            mode="P",
            repeat=repeat,
            output_path="",
            elapsed_sec=elapsed,
            error=f"{exc.__class__.__name__}: {exc}",
            char_count=0,
            table_count=0,
            row_count=0,
            cell_count=0,
            image_count=0,
        )


def collect_pipeline_run_records(
    *,
    input_file: Path,
    output_dir: Path,
    models: list[str],
    repeats: int,
    max_workers: int,
) -> list[RunRecord]:
    records: list[RunRecord] = []
    for model in models:
        for repeat in range(1, repeats + 1):
            print(f"[structure] pipeline start model={model} repeat={repeat}", flush=True)
            record = run_pipeline_once(
                input_file=input_file,
                output_dir=output_dir,
                model=model,
                repeat=repeat,
                max_workers=max_workers,
            )
            print(
                "[structure] pipeline done "
                f"model={model} repeat={repeat} error={record.error or '-'} "
                f"chars={record.char_count} tables={record.table_count} "
                f"rows={record.row_count} cells={record.cell_count}",
                flush=True,
            )
            records.append(record)
            write_outputs(output_dir, records, append_existing=True)
    return records


def _render_pdf_pages(pdf_path: Path, scale: float) -> list[tuple[int, bytes]]:
    rendered: list[tuple[int, bytes]] = []
    with fitz.open(pdf_path) as doc:
        matrix = fitz.Matrix(scale, scale)
        for page_index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            rendered.append((page_index, pix.tobytes("png")))
    return rendered


def _call_openai(client: OpenAI, model: str, image_bytes: bytes) -> str:
    import base64

    encoded = base64.b64encode(image_bytes).decode("ascii")
    params: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                    {"type": "text", "text": DIRECT_PROMPT},
                ],
            }
        ],
        "timeout": 300.0,
    }
    if "gpt-5" in model.lower():
        params["max_completion_tokens"] = 12000
    else:
        params["max_tokens"] = 12000
        params["temperature"] = 0.0

    response = client.chat.completions.create(**params)
    return response.choices[0].message.content or ""


def _call_openrouter(api_key: str, model: str, image_bytes: bytes) -> str:
    import base64

    encoded = base64.b64encode(image_bytes).decode("ascii")
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 12000,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                        {"type": "text", "text": DIRECT_PROMPT},
                    ],
                }
            ],
        },
        timeout=300,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"] or ""


def run_direct_once(
    *,
    input_file: Path,
    output_dir: Path,
    model: str,
    repeat: int,
    scale: float,
    max_workers: int,
) -> RunRecord:
    model_config = MODEL_CONFIGS[model]
    provider = model_config["provider"]
    api_model = model_config["api_model"]
    model_dir = output_dir / "direct_outputs" / _safe_name(model)
    model_dir.mkdir(parents=True, exist_ok=True)
    output_path = model_dir / f"repeat_{repeat:02d}.txt"

    if output_path.exists():
        text = output_path.read_text(encoding="utf-8", errors="replace")
        metrics = count_structure(text)
        return RunRecord(
            model=model,
            mode="NP",
            repeat=repeat,
            output_path=str(output_path),
            elapsed_sec=None,
            error=None,
            **asdict(metrics),
        )

    pages = _render_pdf_pages(input_file, scale)
    started = time.perf_counter()
    page_results: dict[int, str] = {}

    try:
        if provider == "openai":
            client = OpenAI(api_key=env_str("OPENAI_API_KEY"))

            def call(page: tuple[int, bytes]) -> tuple[int, str]:
                page_no, image_bytes = page
                return page_no, _call_openai(client, api_model, image_bytes)

        else:
            api_key = env_str("OPENROUTER_API_KEY")
            if not api_key:
                raise RuntimeError("OPENROUTER_API_KEY is not set.")

            def call(page: tuple[int, bytes]) -> tuple[int, str]:
                page_no, image_bytes = page
                return page_no, _call_openrouter(api_key, api_model, image_bytes)

        workers = max(1, min(max_workers, len(pages)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(call, page) for page in pages]
            for future in as_completed(futures):
                page_no, text = future.result()
                page_results[page_no] = text

        output_text = "\n\n".join(
            f"## Page {page_no}\n{page_results.get(page_no, '').strip()}"
            for page_no, _ in pages
        )
        output_path.write_text(output_text, encoding="utf-8")
        elapsed = round(time.perf_counter() - started, 3)
        metrics = count_structure(output_text)
        return RunRecord(
            model=model,
            mode="NP",
            repeat=repeat,
            output_path=str(output_path),
            elapsed_sec=elapsed,
            error=None,
            **asdict(metrics),
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = round(time.perf_counter() - started, 3)
        return RunRecord(
            model=model,
            mode="NP",
            repeat=repeat,
            output_path=str(output_path),
            elapsed_sec=elapsed,
            error=f"{exc.__class__.__name__}: {exc}",
            char_count=0,
            table_count=0,
            row_count=0,
            cell_count=0,
            image_count=0,
        )


def collect_direct_records(
    *,
    input_file: Path,
    output_dir: Path,
    models: list[str],
    repeats: int,
    scale: float,
    max_workers: int,
) -> list[RunRecord]:
    records: list[RunRecord] = []
    for model in models:
        for repeat in range(1, repeats + 1):
            print(f"[structure] direct start model={model} repeat={repeat}", flush=True)
            record = run_direct_once(
                input_file=input_file,
                output_dir=output_dir,
                model=model,
                repeat=repeat,
                scale=scale,
                max_workers=max_workers,
            )
            print(
                "[structure] direct done "
                f"model={model} repeat={repeat} error={record.error or '-'} "
                f"chars={record.char_count} tables={record.table_count} "
                f"rows={record.row_count} cells={record.cell_count}",
                flush=True,
            )
            records.append(record)
            write_outputs(output_dir, records, append_existing=True)
    return records


def _mean(values: list[int | float]) -> float:
    return round(statistics.mean(values), 3) if values else 0.0


def _stdev(values: list[int | float]) -> float | None:
    return round(statistics.stdev(values), 3) if len(values) >= 2 else None


def summarize(records: list[RunRecord]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[RunRecord]] = {}
    for record in records:
        groups.setdefault((record.model, record.mode), []).append(record)

    summaries: list[dict[str, Any]] = []
    for (model, mode), grouped in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
        ok = [record for record in grouped if not record.error]
        rows = ok or grouped
        summaries.append(
            {
                "model": model,
                "mode": mode,
                "runs": len(grouped),
                "successfulRuns": len(ok),
                "avgCharCount": _mean([record.char_count for record in rows]),
                "stdevCharCount": _stdev([record.char_count for record in rows]),
                "avgTableCount": _mean([record.table_count for record in rows]),
                "stdevTableCount": _stdev([record.table_count for record in rows]),
                "avgRowCount": _mean([record.row_count for record in rows]),
                "stdevRowCount": _stdev([record.row_count for record in rows]),
                "avgCellCount": _mean([record.cell_count for record in rows]),
                "stdevCellCount": _stdev([record.cell_count for record in rows]),
                "avgImageCount": _mean([record.image_count for record in rows]),
                "stdevImageCount": _stdev([record.image_count for record in rows]),
            }
        )
    return summaries


def _load_existing_records(output_dir: Path) -> list[RunRecord]:
    path = output_dir / "structure_extraction_runs.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    records: list[RunRecord] = []
    for row in payload:
        records.append(RunRecord(**row))
    return records


def write_outputs(output_dir: Path, new_records: list[RunRecord], *, append_existing: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    records = _load_existing_records(output_dir) if append_existing else []
    existing_keys = {(record.model, record.mode, record.repeat) for record in records}
    for record in new_records:
        key = (record.model, record.mode, record.repeat)
        if key not in existing_keys:
            records.append(record)
            existing_keys.add(key)

    run_rows = [asdict(record) for record in records]
    summaries = summarize(records)

    (output_dir / "structure_extraction_runs.json").write_text(
        json.dumps(run_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "structure_extraction_average.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (output_dir / "structure_extraction_runs.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(run_rows[0].keys()) if run_rows else [])
        if run_rows:
            writer.writeheader()
            writer.writerows(run_rows)

    with (output_dir / "structure_extraction_average.csv").open("w", encoding="utf-8-sig", newline="") as file:
        fieldnames = list(summaries[0].keys()) if summaries else []
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if summaries:
            writer.writeheader()
            writer.writerows(summaries)

    lines = [
        "# Structure Extraction Benchmark",
        "",
        "| 모델 | 방식 | 실행 수 | 성공 수 | 평균 문자 수 | 평균 표 | 평균 행 | 평균 셀 | 평균 이미지 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in summaries:
        lines.append(
            "| {model} | {mode} | {runs} | {successfulRuns} | {avgCharCount} | {avgTableCount} | {avgRowCount} | {avgCellCount} | {avgImageCount} |".format(
                **summary
            )
        )
    (output_dir / "structure_extraction_average.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure extracted chars/tables/rows/cells/images for P vs NP runs.")
    parser.add_argument("--input-file", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports" / f"structure_extraction_{datetime.now():%Y%m%d_%H%M%S}")
    parser.add_argument("--pipeline-report", type=Path, default=DEFAULT_PIPELINE_REPORT)
    parser.add_argument("--models", nargs="+", default=list(MODEL_CONFIGS.keys()), choices=list(MODEL_CONFIGS.keys()))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--copy-index", type=int, default=1)
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--skip-pipeline", action="store_true")
    parser.add_argument("--skip-direct", action="store_true")
    args = parser.parse_args()

    input_file = args.input_file.resolve()
    output_dir = args.output_dir.resolve()

    if not input_file.exists():
        raise FileNotFoundError(input_file)
    if args.repeats < 1:
        raise ValueError("--repeats must be >= 1")

    records: list[RunRecord] = []
    if not args.skip_pipeline:
        records.extend(
            collect_pipeline_run_records(
                input_file=input_file,
                output_dir=output_dir,
                models=args.models,
                repeats=args.repeats,
                max_workers=args.max_workers,
            )
        )
        write_outputs(output_dir, records, append_existing=True)

    if not args.skip_direct:
        records.extend(
            collect_direct_records(
                input_file=input_file,
                output_dir=output_dir,
                models=args.models,
                repeats=args.repeats,
                scale=args.scale,
                max_workers=args.max_workers,
            )
        )

    write_outputs(output_dir, records, append_existing=True)
    print(f"[structure] wrote {output_dir / 'structure_extraction_average.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
