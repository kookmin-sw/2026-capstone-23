import json
import re
import time
import tempfile
from pathlib import Path
from typing import List

from eval.parsers.base import BaseEngineAdapter
from eval.schema import ParsedDocument
from core.config import load_config
from core.pipeline import DocumentPipeline


class GPTEngineAdapter(BaseEngineAdapter):
    engine_name = "gpt-5-mini"

    def __init__(self):
        config = load_config()
        config.openai_model = "gpt-5-mini"
        # eval 전용 출력 디렉토리 (기존 outputs와 분리)
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="eval_gpt_"))
        config.output_root = self._tmp_dir
        self.pipeline = DocumentPipeline(config)

    def parse(self, file_path: Path) -> ParsedDocument:
        start = time.time()
        try:
            output_path = self.pipeline.process_file(file_path)
            elapsed = time.time() - start

            text = output_path.read_text(encoding="utf-8")
            tables = _extract_html_tables(text)

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


def _extract_html_tables(text: str) -> List[str]:
    """출력 텍스트에서 HTML 표 추출"""
    tables = re.findall(r'<table[\s\S]*?</table>', text, re.IGNORECASE)
    return tables
