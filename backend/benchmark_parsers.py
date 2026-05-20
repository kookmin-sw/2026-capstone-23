from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import AppConfig, load_config
from core.converters import (
    SUPPORTED_DOC,
    ConversionError,
    convert_to_pdf,
    extract_text_from_hwp_pyhwp,
    parse_hwpx_tables,
)
from core.env import env_str, load_project_env
from core.pipeline import DocumentPipeline

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".gif", ".webp"}
UPSTAGE_NATIVE_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tiff",
    ".tif",
    ".docx",
    ".pptx",
    ".xlsx",
}
BENCHMARK_SUPPORTED_EXTENSIONS = SUPPORTED_DOC.union(IMAGE_EXTENSIONS).union({".pdf"})
SHARED_INPUT_AUTO_EXTENSIONS = {".hwp", ".hwpx"}
STRUCTURE_MARKER_RE = re.compile(r"^\[\[/?[A-Z_]+\]\]$")
PAGE_HEADER_RE = re.compile(r"^##\s*Page\s+\d+\s*$")
TABLE_BLOCK_RE = re.compile(r"\[\[TABLE\]\]\s*(.*?)\s*\[\[/TABLE\]\]", re.DOTALL)
MARKDOWN_TABLE_SEPARATOR_RE = re.compile(r"^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$")


def load_environment() -> None:
    load_project_env()


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            continue


def clone_config(config: AppConfig, **updates: Any) -> AppConfig:
    if hasattr(config, "model_dump"):
        data = config.model_dump()
    else:
        data = config.dict()
    data.update(updates)
    return AppConfig(**data)


def find_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found

    candidate = Path(sys.executable).resolve().parent / name
    if candidate.exists():
        return str(candidate)

    if os.name == "nt":
        candidate_exe = candidate.with_suffix(".exe")
        if candidate_exe.exists():
            return str(candidate_exe)

    return name


def collect_source_files(inputs: Iterable[str]) -> List[Path]:
    files: List[Path] = []
    seen: set[Path] = set()

    for raw_path in inputs:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"입력 경로를 찾을 수 없습니다: {path}")

        if path.is_file():
            if path.suffix.lower() in BENCHMARK_SUPPORTED_EXTENSIONS:
                if path not in seen:
                    files.append(path)
                    seen.add(path)
            continue

        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            if child.suffix.lower() not in BENCHMARK_SUPPORTED_EXTENSIONS:
                continue
            child = child.resolve()
            if child in seen:
                continue
            files.append(child)
            seen.add(child)

    if not files:
        raise ValueError("벤치마크할 지원 파일이 없습니다.")
    return files


def compute_common_base(files: List[Path]) -> Path:
    common = Path(os.path.commonpath([str(path) for path in files]))
    if common.is_file():
        return common.parent
    return common


def slugify_path(path: Path) -> str:
    raw = path.stem or path.name or "file"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return slug or "file"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def strip_html_tags(text: str) -> str:
    if "<" not in text or ">" not in text:
        return text
    if BeautifulSoup is not None:
        return BeautifulSoup(text, "html.parser").get_text("\n")
    return re.sub(r"<[^>]+>", " ", text)


def strip_markdown(text: str) -> str:
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = re.sub(r"[|]", " ", text)
    return text


def normalize_local_output(text: str) -> str:
    lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith("원본 파일:"):
            continue
        if stripped.startswith("페이지 수:"):
            continue
        if set(stripped) == {"-"}:
            continue
        if STRUCTURE_MARKER_RE.fullmatch(stripped):
            continue
        if PAGE_HEADER_RE.fullmatch(stripped):
            continue
        lines.append(line)

    cleaned = "\n".join(lines)
    cleaned = strip_html_tags(cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()


def extract_upstage_raw_text(response_body: dict[str, Any]) -> str:
    content = response_body.get("content") or {}
    text_value = content.get("text")
    if isinstance(text_value, str) and text_value.strip():
        return text_value

    markdown_value = content.get("markdown")
    if isinstance(markdown_value, str) and markdown_value.strip():
        return strip_markdown(markdown_value)

    html_value = content.get("html")
    if isinstance(html_value, str) and html_value.strip():
        return strip_html_tags(html_value)

    return ""


def normalize_upstage_output(response_body: dict[str, Any]) -> str:
    text = html.unescape(extract_upstage_raw_text(response_body))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def normalize_reference_text(text: str) -> str:
    text = strip_html_tags(text)
    text = strip_markdown(text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower(), flags=re.UNICODE)


def build_comparison(current_text: str, upstage_text: str) -> dict[str, Any]:
    current_tokens = set(tokenize(current_text))
    upstage_tokens = set(tokenize(upstage_text))
    union = current_tokens | upstage_tokens
    intersection = current_tokens & upstage_tokens

    current_lines = {line.strip() for line in current_text.splitlines() if line.strip()}
    upstage_lines = {line.strip() for line in upstage_text.splitlines() if line.strip()}
    line_union = current_lines | upstage_lines
    line_intersection = current_lines & upstage_lines

    sequence_similarity = None
    if max(len(current_text), len(upstage_text)) <= 20000:
        try:
            from difflib import SequenceMatcher

            sequence_similarity = round(
                SequenceMatcher(None, current_text, upstage_text).ratio(),
                4,
            )
        except Exception:
            sequence_similarity = None

    return {
        "current_char_count": len(current_text),
        "upstage_char_count": len(upstage_text),
        "current_word_count": len(tokenize(current_text)),
        "upstage_word_count": len(tokenize(upstage_text)),
        "token_jaccard": round(len(intersection) / len(union), 4) if union else 1.0,
        "line_jaccard": round(len(line_intersection) / len(line_union), 4) if line_union else 1.0,
        "sequence_similarity": sequence_similarity,
    }


def levenshtein_distance(reference: str, hypothesis: str) -> int:
    if reference == hypothesis:
        return 0
    if not reference:
        return len(hypothesis)
    if not hypothesis:
        return len(reference)

    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference

    previous = list(range(len(hypothesis) + 1))
    for i, ref_char in enumerate(reference, start=1):
        current = [i]
        for j, hyp_char in enumerate(hypothesis, start=1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + (0 if ref_char == hyp_char else 1)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def compute_cer(reference: str, hypothesis: str) -> Optional[float]:
    if reference is None:
        return None
    if len(reference) == 0:
        return 0.0 if len(hypothesis) == 0 else 1.0
    return round(levenshtein_distance(reference, hypothesis) / len(reference), 6)


def tokenize_header(value: str) -> set[str]:
    return set(tokenize(value))


def normalize_cell_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_html_tables_from_text(text: str) -> list[str]:
    if not text:
        return []
    return re.findall(r"<table\b[^>]*>.*?</table>", text, flags=re.DOTALL | re.IGNORECASE)


def extract_markdown_tables(text: str) -> list[list[list[str]]]:
    if not text:
        return []

    tables: list[list[list[str]]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if line.startswith("|") and MARKDOWN_TABLE_SEPARATOR_RE.match(next_line):
            rows: list[list[str]] = []
            rows.append([normalize_cell_text(cell) for cell in line.strip("|").split("|")])
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([normalize_cell_text(cell) for cell in lines[i].strip().strip("|").split("|")])
                i += 1
            if rows:
                tables.append(rows)
            continue
        i += 1
    return tables


def analyze_html_table(table_html: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "format": "html",
        "valid_grid": False,
        "rows": 0,
        "cols": 0,
        "header_rows": 0,
        "cell_count": 0,
        "header_text": "",
        "errors": [],
        "grid_preview": [],
    }

    if BeautifulSoup is None:
        result["errors"].append("beautifulsoup4가 없어 HTML 표 검증을 수행할 수 없습니다.")
        return result

    try:
        soup = BeautifulSoup(table_html, "html.parser")
        table = soup.find("table")
        if table is None:
            result["errors"].append("table 태그를 찾지 못했습니다.")
            return result

        active_spans: list[dict[str, int]] = []
        grid: list[list[str]] = []
        max_cols = 0

        for row_index, tr in enumerate(table.find_all("tr")):
            row: list[str] = []
            column_index = 0
            next_spans: list[dict[str, int]] = []

            for span in active_spans:
                while len(row) <= span["col"]:
                    row.append("")
                row[span["col"]] = span["text"]
                if span["remaining"] > 1:
                    next_spans.append(
                        {
                            "col": span["col"],
                            "remaining": span["remaining"] - 1,
                            "text": span["text"],
                        }
                    )
                column_index = max(column_index, span["col"] + 1)

            cells = tr.find_all(["th", "td"], recursive=False)
            if cells and all(cell.name == "th" for cell in cells):
                result["header_rows"] += 1

            for cell in cells:
                while column_index < len(row) and row[column_index] != "":
                    column_index += 1

                rowspan = max(1, int(cell.get("rowspan", 1) or 1))
                colspan = max(1, int(cell.get("colspan", 1) or 1))
                text = normalize_cell_text(cell.get_text(" ", strip=True))
                result["cell_count"] += 1

                for offset in range(colspan):
                    target_col = column_index + offset
                    while len(row) <= target_col:
                        row.append("")
                    if row[target_col] != "":
                        result["errors"].append(f"row {row_index + 1}, col {target_col + 1} 겹침")
                    row[target_col] = text
                    if rowspan > 1:
                        next_spans.append(
                            {
                                "col": target_col,
                                "remaining": rowspan - 1,
                                "text": text,
                            }
                        )
                column_index += colspan

            max_cols = max(max_cols, len(row))
            grid.append(row)
            active_spans = next_spans

        if active_spans:
            final_row: list[str] = []
            next_spans: list[dict[str, int]] = []
            for span in active_spans:
                while len(final_row) <= span["col"]:
                    final_row.append("")
                final_row[span["col"]] = span["text"]
                if span["remaining"] > 1:
                    next_spans.append(
                        {
                            "col": span["col"],
                            "remaining": span["remaining"] - 1,
                            "text": span["text"],
                        }
                    )
            while next_spans:
                result["errors"].append("rowspan이 마지막 행 이후까지 이어집니다.")
                next_round: list[dict[str, int]] = []
                extra_row: list[str] = []
                for span in next_spans:
                    while len(extra_row) <= span["col"]:
                        extra_row.append("")
                    extra_row[span["col"]] = span["text"]
                    if span["remaining"] > 1:
                        next_round.append(
                            {
                                "col": span["col"],
                                "remaining": span["remaining"] - 1,
                                "text": span["text"],
                            }
                        )
                grid.append(extra_row)
                max_cols = max(max_cols, len(extra_row))
                next_spans = next_round

        normalized_grid: list[list[str]] = []
        for row in grid:
            normalized_row = list(row) + [""] * (max_cols - len(row))
            normalized_grid.append(normalized_row)

        header_rows = normalized_grid[: result["header_rows"]] if result["header_rows"] else normalized_grid[:1]
        header_text = " | ".join(
            normalize_cell_text(cell)
            for row in header_rows
            for cell in row
            if normalize_cell_text(cell)
        )

        result["rows"] = len(normalized_grid)
        result["cols"] = max_cols
        result["header_text"] = header_text
        result["grid_preview"] = normalized_grid[:5]
        result["valid_grid"] = bool(normalized_grid) and not result["errors"] and max_cols > 0
        return result
    except Exception as exc:
        result["errors"].append(str(exc))
        return result


def analyze_markdown_table(rows: list[list[str]]) -> dict[str, Any]:
    normalized_rows = [list(row) for row in rows if row]
    cols = max((len(row) for row in normalized_rows), default=0)
    normalized_rows = [row + [""] * (cols - len(row)) for row in normalized_rows]
    header_text = " | ".join(normalized_rows[0]) if normalized_rows else ""
    return {
        "format": "markdown",
        "valid_grid": bool(normalized_rows and cols > 0),
        "rows": len(normalized_rows),
        "cols": cols,
        "header_rows": 1 if normalized_rows else 0,
        "cell_count": sum(len(row) for row in normalized_rows),
        "header_text": header_text,
        "errors": [],
        "grid_preview": normalized_rows[:5],
    }


def extract_current_tables(raw_text: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    current_page: Optional[int] = None
    inside_table = False
    table_lines: list[str] = []

    for line in raw_text.splitlines():
        stripped = line.strip()
        page_match = re.match(r"^##\s*Page\s+(\d+)\s*$", stripped)
        if page_match:
            current_page = int(page_match.group(1))
            continue

        if stripped == "[[TABLE]]":
            inside_table = True
            table_lines = []
            continue

        if stripped == "[[/TABLE]]":
            html_text = "\n".join(table_lines).strip()
            analysis = analyze_html_table(html_text)
            analysis["page"] = current_page
            analysis["source"] = "current"
            analysis["html"] = html_text
            tables.append(analysis)
            inside_table = False
            table_lines = []
            continue

        if inside_table:
            table_lines.append(line)

    return tables


def extract_upstage_tables(response_body: dict[str, Any]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []

    pages = response_body.get("pages")
    if isinstance(pages, list):
        for index, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                continue
            page_no = page.get("page") or page.get("page_no") or index
            candidates: list[str] = []

            if isinstance(page.get("content"), dict):
                page_content = page["content"]
                for key in ("html", "markdown", "text"):
                    value = page_content.get(key)
                    if isinstance(value, str) and value.strip():
                        candidates.append(value)

            for key in ("html", "markdown", "text"):
                value = page.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value)

            for candidate in candidates:
                for table_html in extract_html_tables_from_text(candidate):
                    analysis = analyze_html_table(table_html)
                    analysis["page"] = int(page_no) if str(page_no).isdigit() else page_no
                    analysis["source"] = "upstage"
                    analysis["html"] = table_html
                    tables.append(analysis)

                if not any("<table" in candidate.lower() for candidate in [candidate]):
                    for markdown_rows in extract_markdown_tables(candidate):
                        analysis = analyze_markdown_table(markdown_rows)
                        analysis["page"] = int(page_no) if str(page_no).isdigit() else page_no
                        analysis["source"] = "upstage"
                        analysis["html"] = None
                        tables.append(analysis)

    if tables:
        return tables

    content = response_body.get("content") or {}
    global_html = content.get("html")
    if isinstance(global_html, str) and global_html.strip():
        for table_html in extract_html_tables_from_text(global_html):
            analysis = analyze_html_table(table_html)
            analysis["page"] = None
            analysis["source"] = "upstage"
            analysis["html"] = table_html
            tables.append(analysis)

    global_markdown = content.get("markdown")
    if not tables and isinstance(global_markdown, str) and global_markdown.strip():
        for markdown_rows in extract_markdown_tables(global_markdown):
            analysis = analyze_markdown_table(markdown_rows)
            analysis["page"] = None
            analysis["source"] = "upstage"
            analysis["html"] = None
            tables.append(analysis)

    return tables


def resolve_gt_path(
    source: Path,
    source_base_dir: Path,
    gt_root: Optional[Path],
    suffix: str,
) -> Path:
    rel = source.resolve().relative_to(source_base_dir.resolve())
    base_dir = gt_root.resolve() if gt_root is not None else source.parent
    target_dir = (base_dir / rel.parent) if gt_root is not None else base_dir
    return target_dir / f"{source.stem}{suffix}"


def load_ground_truth(
    source: Path,
    source_base_dir: Path,
    gt_root: Optional[Path],
    gt_text_suffix: str,
    gt_table_suffix: str,
) -> dict[str, Any]:
    gt: dict[str, Any] = {
        "text_path": None,
        "tables_path": None,
        "reference_text": None,
        "tables": [],
    }

    text_path = resolve_gt_path(source, source_base_dir, gt_root, gt_text_suffix)
    if text_path.exists():
        gt["text_path"] = str(text_path)
        gt["reference_text"] = text_path.read_text(encoding="utf-8").strip()

    tables_path = resolve_gt_path(source, source_base_dir, gt_root, gt_table_suffix)
    if tables_path.exists():
        gt["tables_path"] = str(tables_path)
        payload = json.loads(tables_path.read_text(encoding="utf-8"))
        tables = payload.get("tables") if isinstance(payload, dict) else payload
        if isinstance(tables, list):
            normalized_tables: list[dict[str, Any]] = []
            for item in tables:
                # Preferred schema: already structured dict
                if isinstance(item, dict):
                    normalized_tables.append(item)
                    continue

                # Backward-compatible schema: raw HTML table string
                if isinstance(item, str):
                    analysis = analyze_html_table(item)
                    analysis["source"] = "gt"
                    analysis["html"] = item
                    normalized_tables.append(analysis)

            gt["tables"] = normalized_tables

    return gt


def build_auto_reference_text(source: Path, tmp_root: Path) -> Optional[str]:
    if source.suffix.lower() not in SHARED_INPUT_AUTO_EXTENSIONS:
        return None

    if source.suffix.lower() == ".hwp" and BeautifulSoup is not None:
        html_dir = tmp_root / f"{source.stem}_ref_html"
        try:
            if html_dir.exists():
                shutil.rmtree(html_dir, ignore_errors=True)

            result = subprocess.run(
                [find_tool("hwp5html"), str(source), "--output", str(html_dir)],
                capture_output=True,
                text=True,
                timeout=180,
            )
            index_html = html_dir / "index.xhtml"
            if result.returncode == 0 and index_html.exists():
                normalized = normalize_reference_text(index_html.read_text(encoding="utf-8"))
                if normalized:
                    return normalized
        except Exception:
            pass
        finally:
            if html_dir.exists():
                shutil.rmtree(html_dir, ignore_errors=True)

    try:
        extracted = extract_text_from_hwp_pyhwp(source)
    except Exception:
        return None

    normalized = normalize_reference_text(extracted)
    return normalized or None


def build_auto_table_ground_truth(source: Path, tmp_root: Path) -> list[dict[str, Any]]:
    ext = source.suffix.lower()
    tables: list[dict[str, Any]] = []
    html_candidates: list[str] = []

    if ext == ".hwpx":
        try:
            html_candidates = [table.get("html", "") for table in parse_hwpx_tables(source)]
        except Exception:
            html_candidates = []
    elif ext == ".hwp":
        if BeautifulSoup is None:
            return []

        html_dir = tmp_root / f"{source.stem}_gt_html"
        try:
            if html_dir.exists():
                shutil.rmtree(html_dir, ignore_errors=True)

            result = subprocess.run(
                [find_tool("hwp5html"), str(source), "--output", str(html_dir)],
                capture_output=True,
                text=True,
                timeout=180,
            )
            index_html = html_dir / "index.xhtml"
            if result.returncode == 0 and index_html.exists():
                html_candidates = extract_html_tables_from_text(index_html.read_text(encoding="utf-8"))
        except Exception:
            html_candidates = []
        finally:
            if html_dir.exists():
                shutil.rmtree(html_dir, ignore_errors=True)

    for index, table_html in enumerate(html_candidates, start=1):
        analysis = analyze_html_table(table_html)
        if not analysis.get("valid_grid"):
            continue
        if int(analysis.get("rows") or 0) < 2 or int(analysis.get("cols") or 0) < 2:
            continue

        header_row = []
        grid_preview = analysis.get("grid_preview") or []
        if grid_preview:
            header_row = [cell for cell in grid_preview[0] if cell]

        tables.append(
            {
                "id": f"auto_t{index}",
                "rows": analysis.get("rows"),
                "cols": analysis.get("cols"),
                "header": header_row,
                "auto_generated": True,
            }
        )

    return tables


def should_use_shared_input(source: Path, mode: str) -> bool:
    ext = source.suffix.lower()
    if mode == "never":
        return False
    if mode == "always":
        return ext in BENCHMARK_SUPPORTED_EXTENSIONS
    return ext in SHARED_INPUT_AUTO_EXTENSIONS


def build_pdf_reference_text(pdf_path: Path) -> Optional[str]:
    try:
        import fitz

        doc = fitz.open(pdf_path)
        try:
            raw_text = "\n".join(page.get_text("text") for page in doc)
        finally:
            doc.close()
    except Exception:
        return None

    normalized = normalize_reference_text(raw_text)
    return normalized or None


def gt_table_pages(gt_table: dict[str, Any]) -> list[int]:
    pages = gt_table.get("pages")
    if isinstance(pages, list):
        return [int(page) for page in pages if str(page).isdigit()]

    page_start = gt_table.get("page_start")
    page_end = gt_table.get("page_end")
    if page_start is not None and page_end is not None:
        try:
            start = int(page_start)
            end = int(page_end)
            if end >= start:
                return list(range(start, end + 1))
        except (TypeError, ValueError):
            return []
    return []


def gt_header_text(gt_table: dict[str, Any]) -> str:
    header = gt_table.get("header")
    if isinstance(header, list):
        return " | ".join(str(item) for item in header if str(item).strip())
    if isinstance(header, str):
        return header
    return ""


def score_table_match(predicted: dict[str, Any], gt_table: dict[str, Any]) -> float:
    gt_rows = int(gt_table.get("rows") or 0)
    gt_cols = int(gt_table.get("cols") or 0)
    pred_rows = int(predicted.get("rows") or 0)
    pred_cols = int(predicted.get("cols") or 0)

    row_score = 1.0
    if gt_rows > 0 and pred_rows > 0:
        row_score = 1.0 - (abs(gt_rows - pred_rows) / max(gt_rows, pred_rows))

    col_score = 1.0
    if gt_cols > 0 and pred_cols > 0:
        col_score = 1.0 - (abs(gt_cols - pred_cols) / max(gt_cols, pred_cols))

    gt_header = gt_header_text(gt_table)
    pred_header = predicted.get("header_text", "")
    header_score = 0.5
    if gt_header:
        gt_tokens = tokenize_header(gt_header)
        pred_tokens = tokenize_header(pred_header)
        header_union = gt_tokens | pred_tokens
        header_score = (len(gt_tokens & pred_tokens) / len(header_union)) if header_union else 1.0

    page_score = 0.5
    gt_pages = set(gt_table_pages(gt_table))
    pred_page = predicted.get("page")
    if gt_pages and pred_page is not None and str(pred_page).isdigit():
        page_score = 1.0 if int(pred_page) in gt_pages else 0.0

    return round((row_score * 0.35) + (col_score * 0.35) + (header_score * 0.2) + (page_score * 0.1), 4)


def match_tables(predicted_tables: list[dict[str, Any]], gt_tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    used_indices: set[int] = set()

    for gt_index, gt_table in enumerate(gt_tables):
        best_index = None
        best_score = -1.0

        for pred_index, predicted in enumerate(predicted_tables):
            if pred_index in used_indices:
                continue
            score = score_table_match(predicted, gt_table)
            if score > best_score:
                best_index = pred_index
                best_score = score

        if best_index is not None:
            used_indices.add(best_index)
            matches.append(
                {
                    "gt_index": gt_index,
                    "pred_index": best_index,
                    "score": best_score,
                    "gt_table": gt_table,
                    "predicted_table": predicted_tables[best_index],
                }
            )

    return matches


def evaluate_grid_validation(predicted_tables: list[dict[str, Any]], gt_tables: list[dict[str, Any]]) -> dict[str, Any]:
    predicted_count = len(predicted_tables)
    syntactic_valid = sum(1 for table in predicted_tables if table.get("valid_grid"))

    result = {
        "predicted_table_count": predicted_count,
        "syntactic_valid_table_count": syntactic_valid,
        "syntactic_valid_rate": round(syntactic_valid / predicted_count, 4) if predicted_count else None,
        "gt_table_count": len(gt_tables),
        "matched_table_count": 0,
        "exact_dimension_match_count": 0,
        "exact_dimension_match_rate": None,
        "matches": [],
    }

    if not gt_tables:
        return result

    matches = match_tables(predicted_tables, gt_tables)
    result["matched_table_count"] = len(matches)

    exact_match_count = 0
    for match in matches:
        gt_table = match["gt_table"]
        predicted = match["predicted_table"]
        gt_rows = int(gt_table.get("rows") or 0)
        gt_cols = int(gt_table.get("cols") or 0)
        exact_dim_match = predicted.get("valid_grid") and gt_rows == predicted.get("rows") and gt_cols == predicted.get("cols")
        if exact_dim_match:
            exact_match_count += 1

        result["matches"].append(
            {
                "gt_index": match["gt_index"],
                "pred_index": match["pred_index"],
                "score": match["score"],
                "gt_rows": gt_rows,
                "gt_cols": gt_cols,
                "pred_rows": predicted.get("rows"),
                "pred_cols": predicted.get("cols"),
                "pred_page": predicted.get("page"),
                "valid_grid": predicted.get("valid_grid"),
                "exact_dimension_match": exact_dim_match,
            }
        )

    result["exact_dimension_match_count"] = exact_match_count
    result["exact_dimension_match_rate"] = round(exact_match_count / len(gt_tables), 4) if gt_tables else None
    return result


def evaluate_multi_page_tables(predicted_tables: list[dict[str, Any]], gt_tables: list[dict[str, Any]]) -> dict[str, Any]:
    multi_page_gt = [table for table in gt_tables if len(gt_table_pages(table)) > 1]
    result = {
        "gt_multi_page_table_count": len(multi_page_gt),
        "exact_merge_success_count": 0,
        "exact_merge_success_rate": None,
        "details": [],
    }

    if not multi_page_gt:
        return result

    success_count = 0
    for gt_table in multi_page_gt:
        gt_header = gt_header_text(gt_table)
        gt_header_tokens = tokenize_header(gt_header)
        candidates: list[dict[str, Any]] = []

        for predicted in predicted_tables:
            pred_header_tokens = tokenize_header(predicted.get("header_text", ""))
            header_overlap = 0.0
            union = gt_header_tokens | pred_header_tokens
            if union:
                header_overlap = len(gt_header_tokens & pred_header_tokens) / len(union)

            same_shape = predicted.get("rows") == int(gt_table.get("rows") or 0) and predicted.get("cols") == int(gt_table.get("cols") or 0)
            if same_shape or header_overlap >= 0.5:
                candidates.append(
                    {
                        "page": predicted.get("page"),
                        "rows": predicted.get("rows"),
                        "cols": predicted.get("cols"),
                        "valid_grid": predicted.get("valid_grid"),
                        "header_overlap": round(header_overlap, 4),
                        "same_shape": same_shape,
                    }
                )

        exact_candidates = [candidate for candidate in candidates if candidate["same_shape"] and candidate["valid_grid"]]
        exact_success = len(exact_candidates) == 1
        if exact_success:
            success_count += 1

        result["details"].append(
            {
                "id": gt_table.get("id"),
                "pages": gt_table_pages(gt_table),
                "rows": gt_table.get("rows"),
                "cols": gt_table.get("cols"),
                "candidate_count": len(candidates),
                "exact_merge_success": exact_success,
                "candidates": candidates,
            }
        )

    result["exact_merge_success_count"] = success_count
    result["exact_merge_success_rate"] = round(success_count / len(multi_page_gt), 4) if multi_page_gt else None
    return result


def evaluate_provider_metrics(
    normalized_text: Optional[str],
    predicted_tables: list[dict[str, Any]],
    ground_truth: dict[str, Any],
) -> dict[str, Any]:
    reference_text = ground_truth.get("reference_text")
    gt_tables = ground_truth.get("tables") or []

    text_metrics = {
        "reference_char_count": len(reference_text) if isinstance(reference_text, str) else None,
        "prediction_char_count": len(normalized_text) if isinstance(normalized_text, str) else None,
        "cer": compute_cer(reference_text, normalized_text or "") if isinstance(reference_text, str) else None,
    }

    return {
        "text": text_metrics,
        "grid_validation": evaluate_grid_validation(predicted_tables, gt_tables),
        "multi_page_table_handling": evaluate_multi_page_tables(predicted_tables, gt_tables),
    }


def compact_current_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = dict(result)
    compact.pop("raw_text", None)
    compact.pop("normalized_text", None)
    return compact


def compact_upstage_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = dict(result)
    compact.pop("response_body", None)
    compact.pop("content_text", None)
    compact.pop("content_markdown", None)
    compact.pop("content_html", None)
    compact.pop("normalized_text", None)
    return compact


def clear_pipeline_cache(pipeline: DocumentPipeline) -> None:
    vlm = getattr(pipeline, "vlm", None)
    cache = getattr(vlm, "_image_cache", None)
    if isinstance(cache, dict):
        cache.clear()


def prepare_upstage_input(source: Path, tmp_root: Path) -> tuple[Path, str]:
    source = source.resolve()
    ext = source.suffix.lower()
    tmp_root.mkdir(parents=True, exist_ok=True)

    if ext in UPSTAGE_NATIVE_EXTENSIONS:
        return source, "original"

    if ext in IMAGE_EXTENSIONS:
        from PIL import Image

        converted_path = tmp_root / f"{source.stem}.png"
        image = Image.open(source)
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        image.save(converted_path, format="PNG")
        return converted_path, f"converted_image:{source.suffix.lower()}->.png"

    if ext in SUPPORTED_DOC:
        result = convert_to_pdf(source, tmp_root)
        if isinstance(result, tuple):
            pdf_path = result[0]
        else:
            pdf_path = result
        return Path(pdf_path).resolve(), f"converted_document:{source.suffix.lower()}->.pdf"

    raise ConversionError(f"Upstage 입력으로 준비할 수 없는 형식입니다: {source}")


def run_current_parser(
    pipeline: DocumentPipeline,
    source: Path,
    repeat: int,
    language: str,
    output_base_dir: Path,
) -> dict[str, Any]:
    durations: list[float] = []
    output_path: Path | None = None
    error: str | None = None

    for _ in range(repeat):
        clear_pipeline_cache(pipeline)
        started_at = time.perf_counter()
        try:
            output_path = pipeline.process_file(
                source,
                language=language,
                output_base_dir=output_base_dir,
            )
            durations.append(round(time.perf_counter() - started_at, 4))
        except Exception as exc:
            error = str(exc)
            break

    if error is not None or output_path is None:
        return {
            "ok": False,
            "error": error or "현재 파서 결과가 없습니다.",
            "durations_seconds": durations,
        }

    raw_text = output_path.read_text(encoding="utf-8")
    normalized_text = normalize_local_output(raw_text)

    meta_path = output_path.with_suffix(".meta.json")
    page_count = None
    if meta_path.exists():
        try:
            page_count = json.loads(meta_path.read_text(encoding="utf-8")).get("page_count")
        except json.JSONDecodeError:
            page_count = None

    return {
        "ok": True,
        "model": pipeline.config.openai_model,
        "durations_seconds": durations,
        "avg_duration_seconds": round(sum(durations) / len(durations), 4),
        "output_path": str(output_path),
        "meta_path": str(meta_path) if meta_path.exists() else None,
        "page_count": page_count,
        "raw_char_count": len(raw_text),
        "normalized_char_count": len(normalized_text),
        "raw_text": raw_text,
        "normalized_text": normalized_text,
    }


def call_upstage(
    prepared_path: Path,
    api_key: str,
    model: str,
    mode: str,
    ocr: str,
    output_formats: list[str],
    timeout_seconds: int,
) -> dict[str, Any]:
    content_type = mimetypes.guess_type(prepared_path.name)[0] or "application/octet-stream"
    data = {
        "model": model,
        "mode": mode,
        "ocr": ocr,
        "output_formats": json.dumps(output_formats),
    }

    with prepared_path.open("rb") as handle:
        response = requests.post(
            "https://api.upstage.ai/v1/document-digitization",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"document": (prepared_path.name, handle, content_type)},
            data=data,
            timeout=timeout_seconds,
        )

    response.raise_for_status()
    return response.json()


def run_upstage_parser(
    prepared_path: Path,
    api_key: str,
    model: str,
    mode: str,
    ocr: str,
    output_formats: list[str],
    repeat: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    durations: list[float] = []
    response_body: dict[str, Any] | None = None
    error: str | None = None

    for _ in range(repeat):
        started_at = time.perf_counter()
        try:
            response_body = call_upstage(
                prepared_path=prepared_path,
                api_key=api_key,
                model=model,
                mode=mode,
                ocr=ocr,
                output_formats=output_formats,
                timeout_seconds=timeout_seconds,
            )
            durations.append(round(time.perf_counter() - started_at, 4))
        except Exception as exc:
            error = str(exc)
            break

    if error is not None or response_body is None:
        return {
            "ok": False,
            "error": error or "Upstage 응답이 없습니다.",
            "durations_seconds": durations,
        }

    content = response_body.get("content") or {}
    normalized_text = normalize_upstage_output(response_body)

    return {
        "ok": True,
        "model": model,
        "mode": mode,
        "ocr": ocr,
        "durations_seconds": durations,
        "avg_duration_seconds": round(sum(durations) / len(durations), 4),
        "api_version": response_body.get("api") or response_body.get("apiVersion"),
        "page_count": len(response_body.get("pages", [])) or None,
        "text_char_count": len(content.get("text") or ""),
        "markdown_char_count": len(content.get("markdown") or ""),
        "html_char_count": len(content.get("html") or ""),
        "normalized_char_count": len(normalized_text),
        "response_body": response_body,
        "content_text": content.get("text"),
        "content_markdown": content.get("markdown"),
        "content_html": content.get("html"),
        "normalized_text": normalized_text,
    }


def build_markdown_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# Parser Benchmark Summary",
        "",
        f"- 생성 시각: {summary['created_at']}",
        f"- 현재 파서 모델: {summary['current_parser_model']}",
        f"- 공통 입력 모드: {summary.get('shared_input_mode', 'auto')}",
        f"- Upstage 모델: {summary['upstage']['model']} ({summary['upstage']['mode']}, OCR={summary['upstage']['ocr']})",
        "",
        "| 파일 | 현재(s) | Upstage(s) | CER(current) | CER(upstage) | Grid(current) | Grid(upstage) | Multi(current) | Multi(upstage) | 비고 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    for item in summary["files"]:
        note_parts: list[str] = []
        if item.get("upstage_input_note"):
            note_parts.append(str(item["upstage_input_note"]))
        gt_info = item.get("ground_truth") or {}
        if gt_info.get("reference_source"):
            note_parts.append(f"text_gt={gt_info['reference_source']}")
        if gt_info.get("tables_source"):
            note_parts.append(f"table_gt={gt_info['tables_source']}")
        note = ", ".join(note_parts) if note_parts else "-"
        if not item.get("current", {}).get("ok"):
            note = f"current_error: {item['current'].get('error')}"
        elif not item.get("upstage", {}).get("ok"):
            note = f"upstage_error: {item['upstage'].get('error')}"

        current_avg = item.get("current", {}).get("avg_duration_seconds")
        upstage_avg = item.get("upstage", {}).get("avg_duration_seconds")
        current_eval = item.get("evaluation", {}).get("current") or {}
        upstage_eval = item.get("evaluation", {}).get("upstage") or {}
        current_cer = current_eval.get("text", {}).get("cer")
        upstage_cer = upstage_eval.get("text", {}).get("cer")
        current_grid_eval = current_eval.get("grid_validation", {}) or {}
        upstage_grid_eval = upstage_eval.get("grid_validation", {}) or {}
        current_grid = current_grid_eval.get("exact_dimension_match_rate")
        upstage_grid = upstage_grid_eval.get("exact_dimension_match_rate")
        if current_grid is None:
            current_grid = current_grid_eval.get("syntactic_valid_rate")
        if upstage_grid is None:
            upstage_grid = upstage_grid_eval.get("syntactic_valid_rate")
        current_multi = current_eval.get("multi_page_table_handling", {}).get("exact_merge_success_rate")
        upstage_multi = upstage_eval.get("multi_page_table_handling", {}).get("exact_merge_success_rate")

        lines.append(
            "| {file} | {current_avg} | {upstage_avg} | {current_cer} | {upstage_cer} | {current_grid} | {upstage_grid} | {current_multi} | {upstage_multi} | {note} |".format(
                file=item["source"],
                current_avg=f"{current_avg:.4f}" if isinstance(current_avg, (int, float)) else "-",
                upstage_avg=f"{upstage_avg:.4f}" if isinstance(upstage_avg, (int, float)) else "-",
                current_cer=f"{current_cer:.4f}" if isinstance(current_cer, (int, float)) else "-",
                upstage_cer=f"{upstage_cer:.4f}" if isinstance(upstage_cer, (int, float)) else "-",
                current_grid=f"{current_grid:.4f}" if isinstance(current_grid, (int, float)) else "-",
                upstage_grid=f"{upstage_grid:.4f}" if isinstance(upstage_grid, (int, float)) else "-",
                current_multi=f"{current_multi:.4f}" if isinstance(current_multi, (int, float)) else "-",
                upstage_multi=f"{upstage_multi:.4f}" if isinstance(upstage_multi, (int, float)) else "-",
                note=note,
            )
        )

    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="현재 파서와 Upstage Document Parse를 같은 입력으로 비교합니다.",
    )
    parser.add_argument("inputs", nargs="+", help="파일 또는 디렉터리 경로")
    parser.add_argument("--language", default="한국어", help="현재 파서에 전달할 언어")
    parser.add_argument("--current-model", default=None, help="현재 파서 모델 override")
    parser.add_argument("--upstage-model", default="document-parse", help="Upstage model")
    parser.add_argument(
        "--upstage-mode",
        default="enhanced",
        choices=["standard", "enhanced", "auto"],
        help="Upstage parsing mode",
    )
    parser.add_argument(
        "--upstage-ocr",
        default="auto",
        choices=["auto", "force"],
        help="Upstage OCR mode",
    )
    parser.add_argument(
        "--upstage-output-format",
        dest="upstage_output_formats",
        action="append",
        choices=["text", "markdown", "html"],
        help="Upstage output format. 여러 번 지정 가능",
    )
    parser.add_argument("--repeat", type=int, default=1, help="각 파서를 몇 번 실행할지")
    parser.add_argument("--timeout", type=int, default=600, help="Upstage 요청 timeout 초")
    parser.add_argument("--gt-root", default=None, help="정답 파일 루트 디렉터리")
    parser.add_argument("--gt-text-suffix", default=".gt.txt", help="정답 텍스트 suffix")
    parser.add_argument("--gt-table-suffix", default=".gt.tables.json", help="정답 표 suffix")
    parser.add_argument("--require-ground-truth", action="store_true", help="정답 파일이 없으면 실패 처리")
    parser.add_argument(
        "--shared-input",
        default="auto",
        choices=["auto", "always", "never"],
        help="현재 파서와 Upstage에 같은 변환 입력을 사용할지 설정",
    )
    parser.add_argument(
        "--no-auto-reference-from-source",
        dest="auto_reference_from_source",
        action="store_false",
        help="정답 텍스트가 없을 때 원본 문서에서 자동 참조 텍스트를 만들지 않음",
    )
    parser.add_argument(
        "--no-auto-table-ground-truth",
        dest="auto_table_ground_truth",
        action="store_false",
        help="정답 표가 없을 때 원본 문서에서 자동 표 구조 GT를 만들지 않음",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="결과 저장 디렉터리. 기본값: data/benchmarks/<timestamp>",
    )
    parser.set_defaults(auto_reference_from_source=True, auto_table_ground_truth=True)
    return parser.parse_args()


def main() -> int:
    configure_console_encoding()
    load_environment()
    args = parse_args()

    api_key = env_str("UPSTAGE_API_KEY")
    if not api_key:
        print("UPSTAGE_API_KEY 환경변수가 필요합니다.", file=sys.stderr)
        return 1

    try:
        source_files = collect_source_files(args.inputs)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_root = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else (PROJECT_ROOT / "data" / "benchmarks" / timestamp).resolve()
    )
    report_root.mkdir(parents=True, exist_ok=True)

    output_base_dir = compute_common_base(source_files)
    gt_root = Path(args.gt_root).expanduser().resolve() if args.gt_root else None
    base_config = load_config()
    if args.current_model:
        base_config.openai_model = args.current_model

    benchmark_output_root = report_root / "current_outputs"
    benchmark_tmp_root = report_root / "tmp" / "current"
    benchmark_config = clone_config(
        base_config,
        output_root=benchmark_output_root,
        tmp_root=benchmark_tmp_root,
    )
    pipeline = DocumentPipeline(benchmark_config)

    output_formats = args.upstage_output_formats or ["text", "markdown", "html"]
    summary: dict[str, Any] = {
        "created_at": datetime.now().isoformat(),
        "report_root": str(report_root),
        "current_parser_model": pipeline.config.openai_model,
        "shared_input_mode": args.shared_input,
        "upstage": {
            "model": args.upstage_model,
            "mode": args.upstage_mode,
            "ocr": args.upstage_ocr,
            "output_formats": output_formats,
        },
        "files": [],
    }

    for index, source in enumerate(source_files, start=1):
        artifact_dir = report_root / f"{index:03d}_{slugify_path(source)}"
        artifact_dir.mkdir(parents=True, exist_ok=True)

        file_result: dict[str, Any] = {
            "source": str(source),
            "artifact_dir": str(artifact_dir),
        }

        ground_truth = load_ground_truth(
            source=source,
            source_base_dir=output_base_dir,
            gt_root=gt_root,
            gt_text_suffix=args.gt_text_suffix,
            gt_table_suffix=args.gt_table_suffix,
        )

        if args.auto_reference_from_source and ground_truth.get("reference_text") is None:
            auto_reference_text = build_auto_reference_text(
                source,
                report_root / "tmp" / "ground_truth" / f"{index:03d}",
            )
            if auto_reference_text:
                ground_truth["reference_text"] = auto_reference_text
                ground_truth["reference_source"] = "auto_source_text"

        if args.auto_table_ground_truth and not ground_truth.get("tables"):
            auto_tables = build_auto_table_ground_truth(
                source,
                report_root / "tmp" / "ground_truth" / f"{index:03d}",
            )
            if auto_tables:
                ground_truth["tables"] = auto_tables
                ground_truth["tables_source"] = "auto_source_tables"

        if args.require_ground_truth and not ground_truth.get("reference_text") and not ground_truth.get("tables"):
            raise FileNotFoundError(f"정답 파일이 없습니다: {source}")

        file_result["ground_truth"] = {
            "text_path": ground_truth.get("text_path"),
            "tables_path": ground_truth.get("tables_path"),
            "has_text": ground_truth.get("reference_text") is not None,
            "table_count": len(ground_truth.get("tables") or []),
            "reference_source": ground_truth.get("reference_source") or ("file" if ground_truth.get("text_path") else None),
            "tables_source": ground_truth.get("tables_source") or ("file" if ground_truth.get("tables_path") else None),
        }

        current_source = source
        shared_input_path = None
        shared_input_note = None
        shared_input_error = None
        if should_use_shared_input(source, args.shared_input):
            try:
                shared_input_path, shared_input_note = prepare_upstage_input(
                    source=source,
                    tmp_root=report_root / "tmp" / "shared" / f"{index:03d}",
                )
                current_source = shared_input_path
            except Exception as exc:
                shared_input_error = str(exc)

        file_result["current_input_path"] = str(current_source.resolve())
        file_result["current_input_note"] = shared_input_note or "original"
        file_result["shared_input_path"] = str(shared_input_path) if shared_input_path else None
        file_result["shared_input_note"] = shared_input_note
        file_result["shared_input_error"] = shared_input_error

        if (
            args.auto_reference_from_source
            and shared_input_path is not None
            and shared_input_path.suffix.lower() == ".pdf"
            and len(ground_truth.get("reference_text") or "") < 200
        ):
            pdf_reference_text = build_pdf_reference_text(shared_input_path)
            if pdf_reference_text and len(pdf_reference_text) > len(ground_truth.get("reference_text") or ""):
                ground_truth["reference_text"] = pdf_reference_text
                ground_truth["reference_source"] = "auto_shared_pdf_text"

        file_result["ground_truth"] = {
            "text_path": ground_truth.get("text_path"),
            "tables_path": ground_truth.get("tables_path"),
            "has_text": ground_truth.get("reference_text") is not None,
            "table_count": len(ground_truth.get("tables") or []),
            "reference_source": ground_truth.get("reference_source") or ("file" if ground_truth.get("text_path") else None),
            "tables_source": ground_truth.get("tables_source") or ("file" if ground_truth.get("tables_path") else None),
        }

        current_result = run_current_parser(
            pipeline=pipeline,
            source=current_source,
            repeat=max(1, args.repeat),
            language=args.language,
            output_base_dir=output_base_dir,
        )
        file_result["current"] = compact_current_result(current_result)

        prepared_path = shared_input_path
        upstage_note = shared_input_note
        upstage_result: dict[str, Any]
        try:
            if prepared_path is None:
                prepared_path, upstage_note = prepare_upstage_input(
                    source=source,
                    tmp_root=report_root / "tmp" / "upstage" / f"{index:03d}",
                )
            upstage_result = run_upstage_parser(
                prepared_path=prepared_path,
                api_key=api_key,
                model=args.upstage_model,
                mode=args.upstage_mode,
                ocr=args.upstage_ocr,
                output_formats=output_formats,
                repeat=max(1, args.repeat),
                timeout_seconds=args.timeout,
            )
        except Exception as exc:
            upstage_result = {
                "ok": False,
                "error": str(exc),
                "durations_seconds": [],
            }

        file_result["upstage"] = compact_upstage_result(upstage_result)
        file_result["upstage_input_path"] = str(prepared_path) if prepared_path else None
        file_result["upstage_input_note"] = upstage_note

        if current_result.get("ok"):
            write_text(artifact_dir / "current_raw.txt", current_result["raw_text"])
            write_text(artifact_dir / "current_normalized.txt", current_result["normalized_text"])
            current_tables = extract_current_tables(current_result["raw_text"])
            write_json(artifact_dir / "current_tables.json", current_tables)
        else:
            current_tables = []

        if upstage_result.get("ok"):
            response_body = upstage_result["response_body"]
            write_json(artifact_dir / "upstage_response.json", response_body)
            if upstage_result.get("content_text"):
                write_text(artifact_dir / "upstage_text.txt", upstage_result["content_text"])
            if upstage_result.get("content_markdown"):
                write_text(artifact_dir / "upstage_markdown.md", upstage_result["content_markdown"])
            if upstage_result.get("content_html"):
                write_text(artifact_dir / "upstage_html.html", upstage_result["content_html"])
            write_text(artifact_dir / "upstage_normalized.txt", upstage_result["normalized_text"])
            upstage_tables = extract_upstage_tables(response_body)
            write_json(artifact_dir / "upstage_tables.json", upstage_tables)
        else:
            upstage_tables = []

        if current_result.get("ok") and upstage_result.get("ok"):
            comparison = build_comparison(
                current_result["normalized_text"],
                upstage_result["normalized_text"],
            )
            file_result["comparison"] = comparison
        else:
            file_result["comparison"] = None

        file_result["evaluation"] = {
            "current": evaluate_provider_metrics(
                current_result.get("normalized_text"),
                current_tables,
                ground_truth,
            ) if current_result.get("ok") else None,
            "upstage": evaluate_provider_metrics(
                upstage_result.get("normalized_text"),
                upstage_tables,
                ground_truth,
            ) if upstage_result.get("ok") else None,
        }

        write_json(artifact_dir / "result.json", file_result)
        summary["files"].append(file_result)

        current_status = "ok" if current_result.get("ok") else "fail"
        upstage_status = "ok" if upstage_result.get("ok") else "fail"
        print(f"[{index}/{len(source_files)}] {source.name}: current={current_status}, upstage={upstage_status}")

    write_json(report_root / "summary.json", summary)
    write_text(report_root / "summary.md", build_markdown_summary(summary))

    print(f"summary.json: {report_root / 'summary.json'}")
    print(f"summary.md: {report_root / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
