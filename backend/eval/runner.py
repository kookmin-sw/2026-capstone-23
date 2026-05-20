"""Eval runner orchestrating parser adapters and metrics."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import SUPPORTED_EXTENSIONS
from core.output import normalize_plain_text_output
from eval.metrics.cer import compute_cer
from eval.metrics.cost import compute_cost
from eval.metrics.dpbench import compute_nid, compute_teds, compute_teds_s
from eval.metrics.multipage_table import compute_multipage_table
from eval.metrics.speed import compute_speed
from eval.metrics.table_match import compute_table_match
from eval.parsers.base import BaseEngineAdapter
from eval.schema import GroundTruth, ParsedDocument


@dataclass
class FileResult:
    file_name: str
    engine: str
    error: Optional[str]
    cer: Optional[float]
    nid: Optional[float]
    teds: Optional[float]
    teds_s: Optional[float]
    table_match: Optional[Dict[str, Any]]
    multipage_table: Optional[bool]
    speed: Dict[str, float]
    cost: Dict[str, Any]
    pred_text: Optional[str] = None
    pred_tables: Optional[List[str]] = field(default_factory=list)


@dataclass
class EvalResult:
    engine: str
    file_results: List[FileResult] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        valid = [r for r in self.file_results if not r.error]
        total = len(self.file_results)
        failed = total - len(valid)

        cer_values = [r.cer for r in valid if r.cer is not None]
        avg_cer = round(sum(cer_values) / len(cer_values), 4) if cer_values else None
        nid_values = [r.nid for r in valid if r.nid is not None]
        avg_nid = round(sum(nid_values) / len(nid_values), 4) if nid_values else None
        teds_values = [r.teds for r in valid if r.teds is not None]
        avg_teds = round(sum(teds_values) / len(teds_values), 4) if teds_values else None
        teds_s_values = [r.teds_s for r in valid if r.teds_s is not None]
        avg_teds_s = round(sum(teds_s_values) / len(teds_s_values), 4) if teds_s_values else None

        table_scores = [
            r.table_match["score"]
            for r in valid
            if r.table_match is not None and r.table_match.get("score") is not None
        ]
        avg_table_match = round(sum(table_scores) / len(table_scores), 4) if table_scores else None

        speed_values = [r.speed["sec_per_page"] for r in valid]
        avg_speed = round(sum(speed_values) / len(speed_values), 2) if speed_values else None

        cost_values = [r.cost["estimated_usd"] for r in valid if r.cost.get("estimated_usd") is not None]
        total_cost = round(sum(cost_values), 6) if cost_values else None

        multipage_results = [r.multipage_table for r in valid if r.multipage_table is not None]
        multipage_pass = sum(1 for x in multipage_results if x)
        multipage_total = len(multipage_results)

        return {
            "engine": self.engine,
            "total_files": total,
            "failed_files": failed,
            "avg_cer": avg_cer,
            "avg_nid": avg_nid,
            "avg_teds": avg_teds,
            "avg_teds_s": avg_teds_s,
            "avg_table_match": avg_table_match,
            "avg_sec_per_page": avg_speed,
            "total_estimated_usd": total_cost,
            "multipage_table_pass": f"{multipage_pass}/{multipage_total}" if multipage_total else "N/A (GT 없음)",
        }


def _load_gt(gt_dir: Path, file_name: str) -> Optional[GroundTruth]:
    stem = Path(file_name).stem
    gt_path = gt_dir / f"{stem}.json"
    if not gt_path.exists():
        candidates = list(gt_dir.rglob(f"{stem}.json"))
        if not candidates:
            return None
        gt_path = candidates[0]
    try:
        data = json.loads(gt_path.read_text(encoding="utf-8-sig"))
        return GroundTruth(**data)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] GT 로드 실패 ({gt_path}): {e}")
        return None


def _collect_input_files(input_dir: Path) -> List[Path]:
    files = []
    for file_path in sorted(input_dir.iterdir()):
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(file_path)
    return files


class EvalRunner:
    def __init__(
        self,
        adapters: List[BaseEngineAdapter],
        gt_dir: Optional[Path] = None,
        on_file_done: Optional[Any] = None,
    ):
        self.adapters = adapters
        self.gt_dir = gt_dir
        self.on_file_done = on_file_done

    def run(self, input_dir: Path) -> List[EvalResult]:
        files = _collect_input_files(input_dir)
        if not files:
            print(f"[WARN] No input files: {input_dir}")
            return []

        print(f"[INFO] Evaluating {len(files)} files with {len(self.adapters)} engine(s)")

        results = []
        for adapter in self.adapters:
            print(f"\n[INFO] === Engine: {adapter.engine_name} ===")
            eval_result = EvalResult(engine=adapter.engine_name)

            for file_path in files:
                print(f"[INFO]   Processing: {file_path.name}")
                gt = _load_gt(self.gt_dir, file_path.name) if self.gt_dir else None

                doc = adapter.parse(file_path)
                file_result = self._compute_metrics(doc, gt)
                eval_result.file_results.append(file_result)

                status = f"error: {doc.error}" if doc.error else f"{doc.processing_time_sec:.1f}s"
                print(f"[INFO]     done ({status})")

                if self.on_file_done:
                    self.on_file_done(results + [eval_result])

            results.append(eval_result)

        return results

    def _compute_metrics(self, doc: ParsedDocument, gt: Optional[GroundTruth]) -> FileResult:
        cer = None
        nid = None
        teds = None
        teds_s = None
        table_match = None
        multipage = None

        if gt is not None and not doc.error:
            if gt.text:
                pred_text_norm = normalize_plain_text_output(doc.text or "")
                gt_text_norm = normalize_plain_text_output(gt.text or "")
                cer = compute_cer(pred_text_norm, gt_text_norm)
                nid = compute_nid(pred_text_norm, gt_text_norm)
            if gt.tables:
                teds = compute_teds(doc.tables, gt.tables)
                teds_s = compute_teds_s(doc.tables, gt.tables)
                table_match = compute_table_match(doc.tables, gt.tables)
            multipage = compute_multipage_table(doc.tables, gt.has_multipage_table)

        speed = compute_speed(doc.processing_time_sec, doc.page_count)
        cost = compute_cost(doc.engine, doc.text)

        return FileResult(
            file_name=Path(doc.file_path).name,
            engine=doc.engine,
            error=doc.error,
            cer=round(cer, 4) if cer is not None else None,
            nid=round(nid, 4) if nid is not None else None,
            teds=round(teds, 4) if teds is not None else None,
            teds_s=round(teds_s, 4) if teds_s is not None else None,
            table_match=table_match,
            multipage_table=multipage,
            speed=speed,
            cost=cost,
            pred_text=doc.text,
            pred_tables=list(doc.tables) if doc.tables else [],
        )
