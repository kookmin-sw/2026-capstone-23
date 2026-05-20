"""CLI entrypoint for parser evaluation."""
import argparse
from datetime import datetime
from pathlib import Path

from eval.report import print_console, save_json, save_markdown
from eval.runner import EvalRunner

RESULTS_DIR = Path(__file__).parent / "eval" / "results"


def _build_output_paths(input_dir: Path) -> tuple[Path, Path]:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    folder_name = input_dir.name
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    base = RESULTS_DIR / f"{timestamp}_{folder_name}"
    return base.with_suffix(".md"), base.with_suffix(".json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate parser engines on document files")
    parser.add_argument("--input-dir", required=True, type=Path, help="Input directory with files to evaluate")
    parser.add_argument("--gt-dir", type=Path, default=None, help="Ground-truth JSON root directory")
    parser.add_argument("--no-gt", action="store_true", help="Run without GT (speed/cost/grid only)")
    parser.add_argument(
        "--engine",
        choices=["gpt", "gpt-raw", "pipeline", "qwen", "openrouter", "all"],
        default="all",
        help="Engine selection",
    )
    parser.add_argument(
        "--pipeline-model",
        default="gpt-4o-mini",
        help="Model used when --engine pipeline",
    )
    parser.add_argument(
        "--openrouter-model",
        default=None,
        help="OpenRouter model id used when --engine openrouter/all",
    )
    args = parser.parse_args()

    if not args.input_dir.exists():
        print(f"[ERROR] Input directory does not exist: {args.input_dir}")
        return

    gt_dir = None if args.no_gt else args.gt_dir
    output_md, output_json = _build_output_paths(args.input_dir)
    print(f"[INFO] Output path: {output_md}")

    adapters = []
    if args.engine in ("gpt", "all"):
        from eval.parsers.gpt_engine import GPTEngineAdapter

        print("[INFO] Initializing GPT adapter...")
        adapters.append(GPTEngineAdapter())

    if args.engine in ("pipeline",):
        from eval.parsers.pipeline_engine import PipelineEngineAdapter

        print(f"[INFO] Initializing Pipeline adapter (model={args.pipeline_model})...")
        adapters.append(PipelineEngineAdapter(model=args.pipeline_model))

    if args.engine in ("gpt-raw",):
        from eval.parsers.gpt_raw_engine import GPTRawEngineAdapter

        print("[INFO] Initializing GPT-raw adapter...")
        adapters.append(GPTRawEngineAdapter())

    if args.engine in ("qwen", "all"):
        from eval.parsers.qwen_engine import QwenEngineAdapter

        print("[INFO] Initializing Qwen adapter...")
        adapters.append(QwenEngineAdapter())

    if args.engine in ("openrouter", "all"):
        from eval.parsers.openrouter_engine import OpenRouterEngineAdapter

        if args.openrouter_model:
            print(f"[INFO] Initializing OpenRouter adapter (model={args.openrouter_model})...")
        else:
            print("[INFO] Initializing OpenRouter adapter (model from env/default)...")
        adapters.append(OpenRouterEngineAdapter(model=args.openrouter_model))

    if not adapters:
        print("[ERROR] No adapters selected")
        return

    def _on_file_done(partial_results):
        save_json(partial_results, output_json)
        save_markdown(partial_results, output_md)

    runner = EvalRunner(adapters=adapters, gt_dir=gt_dir, on_file_done=_on_file_done)
    results = runner.run(args.input_dir)

    if not results:
        print("[WARN] No evaluation results")
        return

    print_console(results)
    save_json(results, output_json)
    save_markdown(results, output_md)


if __name__ == "__main__":
    main()
