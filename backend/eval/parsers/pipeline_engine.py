"""Pipeline adapter — DocumentPipeline을 임의의 모델(GPT/OpenRouter)로 실행.

사용 예:
  python eval_run.py --input-dir data/eval_inputs --gt-dir eval/gt \
      --engine pipeline --pipeline-model openrouter/google/gemini-flash-3-preview
  python eval_run.py --input-dir data/eval_inputs --gt-dir eval/gt \
      --engine pipeline --pipeline-model openrouter/anthropic/claude-sonnet-4
  python eval_run.py --input-dir data/eval_inputs --gt-dir eval/gt \
      --engine pipeline  # 기본값: gpt-4o-mini
"""

import json
import re
import tempfile
import time
from pathlib import Path

from eval.parsers.base import BaseEngineAdapter
from eval.schema import ParsedDocument
from core.config import load_config
from core.pipeline import DocumentPipeline


def extract_pipeline_tables(text_raw: str) -> list[str]:
    tables = [
        match.strip()
        for match in re.findall(
            r"\[\[TABLE\]\]([\s\S]*?)\[\[/TABLE\]\]",
            text_raw,
            re.IGNORECASE,
        )
        if match.strip()
    ]
    if tables:
        return tables
    return re.findall(r"<table[\s\S]*?</table>", text_raw, re.IGNORECASE)


def _escape_html(value: str) -> str:
    import html

    return html.escape(value)


def _unescape_html(value: str) -> str:
    import html

    return html.unescape(value)


class PipelineEngineAdapter(BaseEngineAdapter):
    engine_name = "pipeline"

    def __init__(self, model: str = "gpt-4o-mini"):
        config = load_config()
        config.openai_model = model
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="eval_pipeline_"))
        config.output_root = self._tmp_dir
        self.pipeline = DocumentPipeline(config)
        self.engine_name = f"pipeline:{model}"

    def parse(self, file_path: Path) -> ParsedDocument:
        start = time.time()
        try:
            output_path = self.pipeline.process_file(file_path)
            elapsed = time.time() - start

            text_raw = output_path.read_text(encoding="utf-8")
            tables = extract_pipeline_tables(text_raw)
            # CER용 순수 텍스트 추출
            text = re.sub(r"^.*?(?=## Page)", "", text_raw, count=1, flags=re.DOTALL)  # 파일 헤더 제거
            text = re.sub(r"## Page \d+\s*", "", text)  # 페이지 마커 제거
            text = re.sub(r"\[\[TABLE_MARKDOWN\]\][\s\S]*?\[\[/TABLE_MARKDOWN\]\]", "", text)  # TABLE_MARKDOWN 블록 제거
            text = re.sub(r"\[\[.*?\]\]", "", text)  # 나머지 마커 제거
            # HTML 표를 "원래 위치"에서 셀 텍스트로 치환해 순서 왜곡을 줄인다.
            def _table_to_inline_text(match: re.Match) -> str:
                table_html = match.group(0)
                cell_texts = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", table_html, re.IGNORECASE)
                cleaned_cells = []
                for c in cell_texts:
                    c = re.sub(r"<br\s*/?>", " ", c, flags=re.IGNORECASE)
                    c = re.sub(r"<[^>]+>", " ", c)
                    c = _unescape_html(c).replace("\xa0", " ")
                    c = re.sub(r"\s+", " ", c).strip()
                    if c:
                        cleaned_cells.append(c)
                return " " + " ".join(cleaned_cells) + " "

            text = re.sub(
                r"<table[\s\S]*?</table>",
                _table_to_inline_text,
                text,
                flags=re.IGNORECASE,
            )
            text = re.sub(r"\n{2,}", "\n", text).strip()
            if file_path.suffix.lower() == ".pdf":
                try:
                    import fitz

                    with fitz.open(file_path) as doc:
                        pdf_text = "\n".join(page.get_text("text") for page in doc).strip()
                    if pdf_text:
                        text = pdf_text
                except Exception as exc:  # noqa: BLE001
                    print(f"[WARN] PDF text-layer extraction failed for eval text: {exc}")
            if not tables and text:
                escaped_text = _escape_html(text)
                tables = [f"<table><tr><td>{escaped_text}</td></tr></table>"]

            page_count = 1
            meta_path = output_path.with_suffix(".meta.json")
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                page_count = meta.get("page_count", 1)

            return ParsedDocument(
                engine=self.engine_name,
                file_path=str(file_path),
                text=text,
                tables=tables,
                processing_time_sec=elapsed,
                page_count=page_count,
            )
        except Exception as e:
            elapsed = time.time() - start
            return ParsedDocument(
                engine=self.engine_name,
                file_path=str(file_path),
                text="",
                tables=[],
                processing_time_sec=elapsed,
                page_count=0,
                error=str(e),
            )
