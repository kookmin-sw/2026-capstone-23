from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = (
    PROJECT_ROOT
    / "data"
    / "inputs"
    / "B1-ICT-절차-0003_00_충북본부 SCADA_운영화면_표준화_절차서_전문_pdf.pdf"
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MODEL_RUNS = [
    {
        "label": "gpt-5.2",
        "modelId": "m1",
        "executionBackend": "openrouter",
    },
    {
        "label": "qwen3-vl-8b",
        "modelId": "m4",
        "executionBackend": "openrouter",
    },
    {
        "label": "qwen3-vl-32b",
        "modelId": "m5",
        "executionBackend": "openrouter",
    },
]


def _configure_runtime(output_dir: Path, parallelism: int) -> None:
    runtime_root = output_dir / f"runtime_p{parallelism}"
    runtime_root.mkdir(parents=True, exist_ok=True)

    env_defaults = {
        "AUTH_DISABLED": "1",
        "QUEUE_BACKEND": "memory",
        "QUEUE_MEMORY_FALLBACK_ENABLED": "1",
        "STORE_BACKEND": "memory",
        "STATUS_CACHE_BACKEND": "none",
        "ENABLE_INLINE_EXEC_WORKER": "0",
        "ENABLE_INLINE_RECOVERY_WORKER": "0",
        "WORKER_MODE": "all",
        "WORKER_MAX_CONCURRENCY": str(parallelism),
        "INPUT_ROOT": str((runtime_root / "inputs").resolve()),
        "OUTPUT_ROOT": str((runtime_root / "outputs").resolve()),
        "TMP_ROOT": str((runtime_root / "tmp").resolve()),
        "STORE_SQLITE_PATH": str((runtime_root / "store.db").resolve()),
        "VLM_DEVICE": "api",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    for key, value in env_defaults.items():
        os.environ[key] = value


def _unwrap(response) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"non-json response: {response.status_code} {response.text[:500]}") from exc

    if response.status_code >= 400:
        raise RuntimeError(f"request failed: {response.status_code} {json.dumps(payload, ensure_ascii=False)}")

    if payload.get("success") is not True:
        raise RuntimeError(f"api returned failure: {json.dumps(payload, ensure_ascii=False)}")
    return dict(payload.get("data") or {})


def _submit_job(
    client: TestClient,
    *,
    input_path: Path,
    model_id: str,
    execution_backend: str,
    parallelism: int,
    copies: int,
) -> dict[str, Any]:
    opened = []
    try:
        files = []
        for index in range(1, copies + 1):
            handle = input_path.open("rb")
            opened.append(handle)
            files.append(
                (
                    "files",
                    (
                        f"{input_path.stem}_copy{index}{input_path.suffix}",
                        handle,
                        "application/pdf",
                    ),
                )
            )
        response = client.post(
            "/v1/parser/jobs",
            data={
                "userId": "benchmark",
                "modelId": model_id,
                "parallelism": str(parallelism),
                "executionBackend": execution_backend,
                "language": "ko",
            },
            files=files,
            timeout=60,
        )
        return _unwrap(response)
    finally:
        for handle in opened:
            handle.close()


def _poll_until_done(client: TestClient, job_id: str, timeout_seconds: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout_seconds
    last_job: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_job = _unwrap(client.get(f"/v1/parser/jobs/{job_id}", timeout=30))
        status = str(last_job.get("status") or "")
        if status in {"COMPLETED", "FAILED", "CANCELED"}:
            item_payload = _unwrap(client.get(f"/v1/parser/jobs/{job_id}/items", timeout=30))
            return last_job, list(item_payload.get("items") or [])
        time.sleep(2.0)

    raise TimeoutError(f"job did not finish within {timeout_seconds}s: {job_id}, last={last_job}")


def _ms_stats(values: list[int | None]) -> dict[str, float | None]:
    numeric = [int(value) for value in values if value is not None]
    if not numeric:
        return {"minMs": None, "avgMs": None, "maxMs": None}
    return {
        "minMs": min(numeric),
        "avgMs": round(statistics.mean(numeric), 2),
        "maxMs": max(numeric),
    }


def _summarize_run(
    *,
    model: dict[str, str],
    parallelism: int,
    copies: int,
    submit_payload: dict[str, Any] | None,
    job: dict[str, Any] | None,
    items: list[dict[str, Any]],
    wall_clock_ms: int,
    error: str | None,
) -> dict[str, Any]:
    completed = sum(1 for item in items if item.get("status") == "COMPLETED")
    failed = sum(1 for item in items if item.get("status") == "FAILED")
    throughput_docs_per_min = round((completed / (wall_clock_ms / 1000.0)) * 60, 3) if wall_clock_ms > 0 else None
    output_paths = [str(item.get("outputPath") or "") for item in items if item.get("outputPath")]

    return {
        "model": model["label"],
        "modelId": model["modelId"],
        "executionBackend": model["executionBackend"],
        "parallelism": parallelism,
        "copies": copies,
        "jobId": (submit_payload or {}).get("jobId") or (job or {}).get("jobId"),
        "jobStatus": (job or {}).get("status") or ("FAILED_TO_RUN" if error else "UNKNOWN"),
        "completedItems": completed,
        "failedItems": failed,
        "wallClockMs": wall_clock_ms,
        "throughputDocsPerMin": throughput_docs_per_min,
        "queueWait": _ms_stats([item.get("queueWaitMs") for item in items]),
        "processing": _ms_stats([item.get("processingTimeMs") for item in items]),
        "endToEnd": _ms_stats([item.get("endToEndMs") for item in items]),
        "outputPaths": output_paths,
        "error": error,
        "items": items,
    }


def _write_outputs(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "api_conversion_benchmark.json"
    csv_path = output_dir / "api_conversion_benchmark.csv"
    md_path = output_dir / "api_conversion_benchmark.md"

    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
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
                "throughputDocsPerMin",
                "queueWaitAvgMs",
                "processingAvgMs",
                "endToEndAvgMs",
                "error",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "model": row["model"],
                    "modelId": row["modelId"],
                    "executionBackend": row["executionBackend"],
                    "parallelism": row["parallelism"],
                    "copies": row["copies"],
                    "jobId": row["jobId"],
                    "jobStatus": row["jobStatus"],
                    "completedItems": row["completedItems"],
                    "failedItems": row["failedItems"],
                    "wallClockMs": row["wallClockMs"],
                    "throughputDocsPerMin": row["throughputDocsPerMin"],
                    "queueWaitAvgMs": row["queueWait"]["avgMs"],
                    "processingAvgMs": row["processing"]["avgMs"],
                    "endToEndAvgMs": row["endToEnd"]["avgMs"],
                    "error": row["error"] or "",
                }
            )

    lines = [
        "# API 변환 벤치마크",
        "",
        "| 모델 | 백엔드 | 병렬도 | 문서 수 | 상태 | 완료 수 | 전체 소요 시간(초) | 처리량(문서/분) | 평균 대기 시간(초) | 평균 처리 시간(초) | 평균 End-to-End 시간(초) |",
        "| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        def sec(value: Any) -> str:
            return "" if value is None else f"{float(value) / 1000:.2f}"

        lines.append(
            "| {model} | {backend} | {parallelism} | {copies} | {status} | {completed} | {wall} | {throughput} | {queue} | {processing} | {e2e} |".format(
                model=row["model"],
                backend=row["executionBackend"],
                parallelism=row["parallelism"],
                copies=row["copies"],
                status=row["jobStatus"],
                completed=row["completedItems"],
                wall=sec(row["wallClockMs"]),
                throughput=row["throughputDocsPerMin"] if row["throughputDocsPerMin"] is not None else "",
                queue=sec(row["queueWait"]["avgMs"]),
                processing=sec(row["processing"]["avgMs"]),
                e2e=sec(row["endToEnd"]["avgMs"]),
            )
        )
    lines.extend(["", "## 출력 파일", ""])
    for row in rows:
        lines.append(f"### {row['model']} p{row['parallelism']}")
        if row["error"]:
            lines.append(f"- 오류: {row['error']}")
        for output_path in row["outputPaths"]:
            lines.append(f"- {output_path}")
        if not row["outputPaths"] and not row["error"]:
            lines.append("- 기록된 출력 경로가 없습니다.")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"[benchmark] wrote {json_path}")
    print(f"[benchmark] wrote {csv_path}")
    print(f"[benchmark] wrote {md_path}")


def _select_models(model_names: list[str] | None) -> list[dict[str, str]]:
    if not model_names:
        return MODEL_RUNS

    by_name = {model["label"]: model for model in MODEL_RUNS}
    by_id = {model["modelId"]: model for model in MODEL_RUNS}
    selected: list[dict[str, str]] = []
    for name in model_names:
        model = by_name.get(name) or by_id.get(name)
        if model is None:
            valid = ", ".join(model["label"] for model in MODEL_RUNS)
            raise ValueError(f"unknown model '{name}', valid models: {valid}")
        if model not in selected:
            selected.append(model)
    return selected


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value).strip("_")


def _run_one(
    parallelism: int,
    input_path: Path,
    output_dir: Path,
    copies: int,
    timeout_seconds: int,
    models: list[dict[str, str]],
) -> list[dict[str, Any]]:
    _configure_runtime(output_dir, parallelism)

    from api import create_app
    from worker.runtime import run_worker

    results: list[dict[str, Any]] = []
    app = create_app()
    with TestClient(app) as client:
        stop_event = threading.Event()
        worker_thread = threading.Thread(
            target=run_worker,
            kwargs={
                "worker_id": f"benchmark-worker-p{parallelism}",
                "stop_event": stop_event,
                "max_concurrency": parallelism,
                "worker_mode": "all",
            },
            daemon=True,
        )
        worker_thread.start()
        try:
            for model in models:
                print(f"[benchmark] start model={model['label']} parallelism={parallelism} copies={copies}")
                started = time.perf_counter()
                submit_payload: dict[str, Any] | None = None
                job: dict[str, Any] | None = None
                items: list[dict[str, Any]] = []
                error: str | None = None
                try:
                    submit_payload = _submit_job(
                        client,
                        input_path=input_path,
                        model_id=model["modelId"],
                        execution_backend=model["executionBackend"],
                        parallelism=parallelism,
                        copies=copies,
                    )
                    job, items = _poll_until_done(client, str(submit_payload["jobId"]), timeout_seconds)
                    if str(job.get("status") or "") != "COMPLETED":
                        error = json.dumps(job, ensure_ascii=False)
                except Exception as exc:  # noqa: BLE001
                    error = f"{exc.__class__.__name__}: {exc}"
                    print(f"[benchmark] failed model={model['label']} parallelism={parallelism}: {error}")
                wall_clock_ms = int((time.perf_counter() - started) * 1000)
                results.append(
                    _summarize_run(
                        model=model,
                        parallelism=parallelism,
                        copies=copies,
                        submit_payload=submit_payload,
                        job=job,
                        items=items,
                        wall_clock_ms=wall_clock_ms,
                        error=error,
                    )
                )
                _write_outputs(output_dir, results)
        finally:
            stop_event.set()
            worker_thread.join(timeout=10)
    return results


def _run_isolated_children(
    *,
    script_path: Path,
    input_path: Path,
    output_dir: Path,
    copies: int,
    parallelisms: list[int],
    timeout_seconds: int,
    models: list[dict[str, str]],
) -> list[dict[str, Any]]:
    all_results: list[dict[str, Any]] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    child_timeout = timeout_seconds + 300

    for parallelism in parallelisms:
        if parallelism < 1:
            print(f"[benchmark] skip invalid parallelism={parallelism}")
            continue
        for model in models:
            child_dir = output_dir / "runs" / f"{_safe_name(model['label'])}_p{parallelism}"
            child_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable,
                "-u",
                str(script_path),
                "--single-run",
                "--input-file",
                str(input_path),
                "--output-dir",
                str(child_dir),
                "--copies",
                str(copies),
                "--parallelism",
                str(parallelism),
                "--timeout-seconds",
                str(timeout_seconds),
                "--models",
                model["label"],
            ]
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"

            print(f"[benchmark] isolated start model={model['label']} parallelism={parallelism}")
            started = time.perf_counter()
            error: str | None = None
            try:
                subprocess.run(
                    cmd,
                    cwd=str(PROJECT_ROOT),
                    env=env,
                    check=True,
                    timeout=child_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                error = f"TimeoutExpired: child exceeded {child_timeout}s"
                print(f"[benchmark] isolated timeout model={model['label']} parallelism={parallelism}: {exc}")
            except subprocess.CalledProcessError as exc:
                error = f"CalledProcessError: child exited with {exc.returncode}"
                print(f"[benchmark] isolated failed model={model['label']} parallelism={parallelism}: {exc}")

            child_json = child_dir / "api_conversion_benchmark.json"
            if child_json.exists():
                rows = json.loads(child_json.read_text(encoding="utf-8"))
                all_results.extend(rows)
            else:
                wall_clock_ms = int((time.perf_counter() - started) * 1000)
                all_results.append(
                    _summarize_run(
                        model=model,
                        parallelism=parallelism,
                        copies=copies,
                        submit_payload=None,
                        job=None,
                        items=[],
                        wall_clock_ms=wall_clock_ms,
                        error=error or "child did not produce benchmark json",
                    )
                )
            _write_outputs(output_dir, all_results)

    return all_results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run API model conversion benchmark through /v1/parser/jobs.")
    parser.add_argument("--input-file", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports" / f"api_conversion_{datetime.now():%Y%m%d_%H%M%S}")
    parser.add_argument("--copies", type=int, default=2)
    parser.add_argument("--parallelism", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--models", nargs="+", default=None)
    parser.add_argument("--single-run", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    input_path = args.input_file.resolve()
    if not input_path.exists():
        print(f"[benchmark] input file not found: {input_path}", file=sys.stderr)
        return 2
    if args.copies < 1:
        print("[benchmark] --copies must be >= 1", file=sys.stderr)
        return 2
    try:
        models = _select_models(args.models)
    except ValueError as exc:
        print(f"[benchmark] {exc}", file=sys.stderr)
        return 2

    output_dir = args.output_dir.resolve()
    if args.single_run:
        if len(args.parallelism) != 1:
            print("[benchmark] --single-run requires exactly one --parallelism value", file=sys.stderr)
            return 2
        _run_one(
            parallelism=args.parallelism[0],
            input_path=input_path,
            output_dir=output_dir,
            copies=args.copies,
            timeout_seconds=args.timeout_seconds,
            models=models,
        )
        return 0

    all_results: list[dict[str, Any]] = []
    all_results.extend(
        _run_isolated_children(
            script_path=Path(__file__).resolve(),
            input_path=input_path,
            output_dir=output_dir,
            copies=args.copies,
            parallelisms=args.parallelism,
            timeout_seconds=args.timeout_seconds,
            models=models,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
