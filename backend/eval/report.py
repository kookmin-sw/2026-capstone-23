"""평가 결과 출력 - 콘솔 / JSON / Markdown."""
import json
from pathlib import Path
from typing import List

from eval.runner import EvalResult


def _fmt(val, suffix="") -> str:
    if val is None:
        return "N/A"
    return f"{val}{suffix}"


def print_console(results: List[EvalResult]) -> None:
    print("\n" + "=" * 60)
    print("  성능 평가 결과 요약")
    print("=" * 60)

    for result in results:
        summary = result.summary()
        print(f"\n[ 엔진: {summary['engine']} ]")
        print(f"  파일 수         : {summary['total_files']} (실패: {summary['failed_files']})")
        print(f"  평균 CER        : {_fmt(summary['avg_cer'])}")
        print(f"  평균 NID        : {_fmt(summary.get('avg_nid'))}")
        print(f"  평균 TEDS       : {_fmt(summary.get('avg_teds'))}")
        print(f"  평균 TEDS-S     : {_fmt(summary.get('avg_teds_s'))}")
        print(f"  표 구조 정확도  : {_fmt(summary.get('avg_table_match'))}")
        print(f"  처리 속도       : {_fmt(summary['avg_sec_per_page'], 's/page')}")
        print(f"  총 비용 추정    : {_fmt(summary['total_estimated_usd'], ' USD')}")
        print(f"  다중페이지 표   : {summary['multipage_table_pass']}")


def save_json(results: List[EvalResult], output_path: Path) -> None:
    data = []
    for result in results:
        files = []
        for file_result in result.file_results:
            files.append(
                {
                    "file": file_result.file_name,
                    "engine": file_result.engine,
                    "error": file_result.error,
                    "cer": file_result.cer,
                    "nid": file_result.nid,
                    "teds": file_result.teds,
                    "teds_s": file_result.teds_s,
                    "table_match": file_result.table_match,
                    "multipage_table": file_result.multipage_table,
                    "speed": file_result.speed,
                    "cost": file_result.cost,
                    "pred_text": file_result.pred_text,
                    "pred_tables": file_result.pred_tables,
                }
            )

        data.append({"summary": result.summary(), "files": files})

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] JSON 저장 완료: {output_path}")


def save_markdown(results: List[EvalResult], output_path: Path) -> None:
    lines = ["# 성능 평가 보고서", ""]

    for result in results:
        summary = result.summary()
        lines.append(f"## 엔진: {summary['engine']}")
        lines.append("")
        lines.append("| 지표 | 값 |")
        lines.append("|---|---|")
        lines.append(f"| 파일 수 | {summary['total_files']} (실패: {summary['failed_files']}) |")
        lines.append(f"| 평균 CER | {_fmt(summary['avg_cer'])} |")
        lines.append(f"| 평균 NID | {_fmt(summary.get('avg_nid'))} |")
        lines.append(f"| 평균 TEDS | {_fmt(summary.get('avg_teds'))} |")
        lines.append(f"| 평균 TEDS-S | {_fmt(summary.get('avg_teds_s'))} |")
        lines.append(f"| 표 구조 정확도 (GT 비교) | {_fmt(summary.get('avg_table_match'))} |")
        lines.append(f"| 처리 속도 | {_fmt(summary['avg_sec_per_page'], 's/page')} |")
        lines.append(f"| 총 비용 추정 | {_fmt(summary['total_estimated_usd'], ' USD')} |")
        lines.append(f"| 다중페이지 표 | {summary['multipage_table_pass']} |")
        lines.append("")

        lines.append("### 파일별 결과")
        lines.append("")
        lines.append("| 파일 | CER | NID | TEDS | TEDS-S | 표 구조 정확도 | 속도(s/page) | 오류 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for file_result in result.file_results:
            score = file_result.table_match["score"] if file_result.table_match else "N/A"
            lines.append(
                f"| {file_result.file_name} | {_fmt(file_result.cer)} | {_fmt(file_result.nid)} | {_fmt(file_result.teds)} | {_fmt(file_result.teds_s)} | {score} | {file_result.speed['sec_per_page']} | {file_result.error or '-'} |"
            )
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[INFO] Markdown 저장 완료: {output_path}")
