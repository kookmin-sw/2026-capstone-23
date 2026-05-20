from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PROJECT_ROOT / "scripts"
BENCHMARK_SCRIPT = SCRIPT_DIR / "run_api_conversion_benchmark.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_api_conversion_benchmark import DEFAULT_INPUT, MODEL_RUNS  # noqa: E402


def _sec(ms: Any) -> float | None:
    if ms is None:
        return None
    return round(float(ms) / 1000.0, 3)


def _mean(values: list[float | int | None]) -> float | None:
    numeric = [float(value) for value in values if value is not None]
    if not numeric:
        return None
    return round(statistics.mean(numeric), 3)


def _stdev(values: list[float | int | None]) -> float | None:
    numeric = [float(value) for value in values if value is not None]
    if len(numeric) < 2:
        return None
    return round(statistics.stdev(numeric), 3)


def _status_ok(row: dict[str, Any]) -> bool:
    return (
        row.get("jobStatus") == "COMPLETED"
        and not row.get("error")
        and int(row.get("completedItems") or 0) == int(row.get("copies") or 0)
    )


def _with_repeat(rows: list[dict[str, Any]], repeat: int) -> list[dict[str, Any]]:
    repeated: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        copied["repeat"] = repeat
        repeated.append(copied)
    return repeated


def _write_raw_outputs(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "api_conversion_benchmark_repeats.json"
    csv_path = output_dir / "api_conversion_benchmark_repeats.csv"
    md_path = output_dir / "api_conversion_benchmark_repeats.md"

    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "repeat",
                "model",
                "modelId",
                "executionBackend",
                "parallelism",
                "copies",
                "jobId",
                "jobStatus",
                "completedItems",
                "failedItems",
                "wallClockMs",
                "wallSec",
                "throughputDocsPerMin",
                "queueWaitAvgSec",
                "processingAvgSec",
                "endToEndAvgSec",
                "error",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "repeat": row.get("repeat"),
                    "model": row.get("model"),
                    "modelId": row.get("modelId"),
                    "executionBackend": row.get("executionBackend"),
                    "parallelism": row.get("parallelism"),
                    "copies": row.get("copies"),
                    "jobId": row.get("jobId"),
                    "jobStatus": row.get("jobStatus"),
                    "completedItems": row.get("completedItems"),
                    "failedItems": row.get("failedItems"),
                    "wallClockMs": row.get("wallClockMs"),
                    "wallSec": _sec(row.get("wallClockMs")),
                    "throughputDocsPerMin": row.get("throughputDocsPerMin"),
                    "queueWaitAvgSec": _sec((row.get("queueWait") or {}).get("avgMs")),
                    "processingAvgSec": _sec((row.get("processing") or {}).get("avgMs")),
                    "endToEndAvgSec": _sec((row.get("endToEnd") or {}).get("avgMs")),
                    "error": row.get("error") or "",
                }
            )

    lines = [
        "# API 변환 벤치마크 반복 원시 결과",
        "",
        "| 회차 | 모델 | 백엔드 | 병렬도 | 문서 수 | 상태 | 완료 수 | 전체 소요 시간(초) | 처리량(문서/분) | 평균 대기 시간(초) | 평균 처리 시간(초) | 평균 End-to-End 시간(초) |",
        "| ---: | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {repeat} | {model} | {backend} | {parallelism} | {copies} | {status} | {completed} | {wall} | {throughput} | {queue} | {processing} | {e2e} |".format(
                repeat=row.get("repeat"),
                model=row.get("model"),
                backend=row.get("executionBackend"),
                parallelism=row.get("parallelism"),
                copies=row.get("copies"),
                status=row.get("jobStatus"),
                completed=row.get("completedItems"),
                wall=_sec(row.get("wallClockMs")),
                throughput=row.get("throughputDocsPerMin"),
                queue=_sec((row.get("queueWait") or {}).get("avgMs")),
                processing=_sec((row.get("processing") or {}).get("avgMs")),
                e2e=_sec((row.get("endToEnd") or {}).get("avgMs")),
            )
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"[repeats] wrote {json_path}")
    print(f"[repeats] wrote {csv_path}")
    print(f"[repeats] wrote {md_path}")


def _aggregate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, int, int], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("model") or ""),
            str(row.get("modelId") or ""),
            str(row.get("executionBackend") or ""),
            int(row.get("parallelism") or 0),
            int(row.get("copies") or 0),
        )
        groups.setdefault(key, []).append(row)

    summaries: list[dict[str, Any]] = []
    for (model, model_id, backend, parallelism, copies), grouped in sorted(groups.items(), key=lambda item: (item[0][3], item[0][0])):
        successful = [row for row in grouped if _status_ok(row)]
        metric_rows = successful or grouped
        wall_sec = [_sec(row.get("wallClockMs")) for row in metric_rows]
        throughput = [row.get("throughputDocsPerMin") for row in metric_rows]
        queue_sec = [_sec((row.get("queueWait") or {}).get("avgMs")) for row in metric_rows]
        processing_sec = [_sec((row.get("processing") or {}).get("avgMs")) for row in metric_rows]
        e2e_sec = [_sec((row.get("endToEnd") or {}).get("avgMs")) for row in metric_rows]

        summaries.append(
            {
                "model": model,
                "modelId": model_id,
                "executionBackend": backend,
                "parallelism": parallelism,
                "copies": copies,
                "runs": len(grouped),
                "successfulRuns": len(successful),
                "avgWallSec": _mean(wall_sec),
                "stdevWallSec": _stdev(wall_sec),
                "minWallSec": min(value for value in wall_sec if value is not None) if any(value is not None for value in wall_sec) else None,
                "maxWallSec": max(value for value in wall_sec if value is not None) if any(value is not None for value in wall_sec) else None,
                "avgThroughputDocsPerMin": _mean(throughput),
                "avgQueueWaitSec": _mean(queue_sec),
                "avgProcessingSec": _mean(processing_sec),
                "avgEndToEndSec": _mean(e2e_sec),
                "rawRepeats": [row.get("repeat") for row in grouped],
            }
        )
    return summaries


def _write_average_outputs(output_dir: Path, summaries: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "api_conversion_benchmark_average.json"
    csv_path = output_dir / "api_conversion_benchmark_average.csv"
    md_path = output_dir / "api_conversion_benchmark_average.md"

    json_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "model",
                "modelId",
                "executionBackend",
                "parallelism",
                "copies",
                "runs",
                "successfulRuns",
                "avgWallSec",
                "stdevWallSec",
                "minWallSec",
                "maxWallSec",
                "avgThroughputDocsPerMin",
                "avgQueueWaitSec",
                "avgProcessingSec",
                "avgEndToEndSec",
                "rawRepeats",
            ],
        )
        writer.writeheader()
        for summary in summaries:
            writer.writerow(summary)

    lines = [
        "# API 변환 벤치마크 5회 평균",
        "",
        "| 모델 | 백엔드 | 병렬도 | 문서 수 | 실행 횟수 | 성공 횟수 | 평균 전체 소요 시간(초) | 표준편차(초) | 최소(초) | 최대(초) | 평균 처리량(문서/분) | 평균 대기 시간(초) | 평균 처리 시간(초) | 평균 End-to-End 시간(초) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for summary in summaries:
        lines.append(
            "| {model} | {backend} | {parallelism} | {copies} | {runs} | {successful} | {wall} | {stdev} | {min_wall} | {max_wall} | {throughput} | {queue} | {processing} | {e2e} |".format(
                model=summary["model"],
                backend=summary["executionBackend"],
                parallelism=summary["parallelism"],
                copies=summary["copies"],
                runs=summary["runs"],
                successful=summary["successfulRuns"],
                wall=summary["avgWallSec"],
                stdev=summary["stdevWallSec"] if summary["stdevWallSec"] is not None else "",
                min_wall=summary["minWallSec"],
                max_wall=summary["maxWallSec"],
                throughput=summary["avgThroughputDocsPerMin"],
                queue=summary["avgQueueWaitSec"],
                processing=summary["avgProcessingSec"],
                e2e=summary["avgEndToEndSec"],
            )
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"[repeats] wrote {json_path}")
    print(f"[repeats] wrote {csv_path}")
    print(f"[repeats] wrote {md_path}")


def _write_all_outputs(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    _write_raw_outputs(output_dir, rows)
    _write_average_outputs(output_dir, _aggregate_rows(rows))


def _expected_rows_per_repeat(models: list[str] | None, parallelisms: list[int]) -> int:
    selected_model_count = len(models) if models else len(MODEL_RUNS)
    return selected_model_count * len([value for value in parallelisms if value >= 1])


def _load_existing_rows(output_dir: Path) -> list[dict[str, Any]]:
    raw_path = output_dir / "api_conversion_benchmark_repeats.json"
    if not raw_path.exists():
        return []
    rows = json.loads(raw_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _run_repeat(
    *,
    repeat: int,
    output_dir: Path,
    input_file: Path,
    copies: int,
    parallelisms: list[int],
    timeout_seconds: int,
    models: list[str] | None,
) -> list[dict[str, Any]]:
    repeat_dir = output_dir / f"repeat_{repeat:02d}"
    repeat_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-u",
        str(BENCHMARK_SCRIPT),
        "--input-file",
        str(input_file),
        "--output-dir",
        str(repeat_dir),
        "--copies",
        str(copies),
        "--parallelism",
        *(str(value) for value in parallelisms),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    if models:
        cmd.extend(["--models", *models])

    print(f"[repeats] start repeat={repeat} output={repeat_dir}")
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env, check=False)
    if completed.returncode != 0:
        print(f"[repeats] repeat={repeat} child exited with {completed.returncode}")

    result_path = repeat_dir / "api_conversion_benchmark.json"
    if not result_path.exists():
        return [
            {
                "repeat": repeat,
                "model": "ALL",
                "modelId": "",
                "executionBackend": "",
                "parallelism": 0,
                "copies": copies,
                "jobId": None,
                "jobStatus": "FAILED_TO_RUN",
                "completedItems": 0,
                "failedItems": copies,
                "wallClockMs": 0,
                "throughputDocsPerMin": None,
                "queueWait": {"minMs": None, "avgMs": None, "maxMs": None},
                "processing": {"minMs": None, "avgMs": None, "maxMs": None},
                "endToEnd": {"minMs": None, "avgMs": None, "maxMs": None},
                "outputPaths": [],
                "error": f"repeat output json missing: {result_path}",
                "items": [],
            }
        ]

    rows = json.loads(result_path.read_text(encoding="utf-8"))
    return _with_repeat(list(rows), repeat)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repeated API conversion benchmark and average the results.")
    parser.add_argument("--input-file", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports" / f"api_conversion_repeats_{datetime.now():%Y%m%d_%H%M%S}")
    parser.add_argument("--copies", type=int, default=2)
    parser.add_argument("--parallelism", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--models", nargs="+", default=None)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()

    input_file = args.input_file.resolve()
    if not input_file.exists():
        print(f"[repeats] input file not found: {input_file}", file=sys.stderr)
        return 2
    if args.repeats < 1:
        print("[repeats] --repeats must be >= 1", file=sys.stderr)
        return 2

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict[str, Any]] = _load_existing_rows(output_dir)
    expected_rows = _expected_rows_per_repeat(args.models, args.parallelism)
    if all_rows:
        print(f"[repeats] resume with existing rows={len(all_rows)}")
        _write_all_outputs(output_dir, all_rows)

    for repeat in range(1, args.repeats + 1):
        existing_for_repeat = [row for row in all_rows if int(row.get("repeat") or 0) == repeat]
        if len(existing_for_repeat) >= expected_rows:
            print(f"[repeats] skip existing repeat={repeat} rows={len(existing_for_repeat)}")
            continue

        rows = _run_repeat(
            repeat=repeat,
            output_dir=output_dir,
            input_file=input_file,
            copies=args.copies,
            parallelisms=args.parallelism,
            timeout_seconds=args.timeout_seconds,
            models=args.models,
        )
        all_rows.extend(rows)
        _write_all_outputs(output_dir, all_rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
