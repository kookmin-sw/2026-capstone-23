"""GPT raw adapter — DocumentPipeline 없이 OpenAI API 직접 호출.

모델 비교 분석용. Qwen/Gemini/Claude raw 결과와 공평하게 비교하기 위해 사용.

Usage:
  python eval_run.py --input-dir data/eval_inputs --gt-dir eval/gt --engine gpt-raw
"""

import base64
import re
import time
from pathlib import Path
from typing import List

from openai import OpenAI

from core.env import env_str
from eval.parsers.base import BaseEngineAdapter
from eval.schema import ParsedDocument

PROMPT = """Extract all visible text from the image accurately.

If there is a table, output strict HTML only:
- Use only <table>, <tr>, <th>, <td>
- Use colspan/rowspan when needed
- Use valid closing tags</th></td></tr></table>
- Do NOT wrap with markdown fences

If the input is mainly a table, output only table HTML.
"""


class GPTRawEngineAdapter(BaseEngineAdapter):
    engine_name = "gpt-raw"

    def __init__(self, model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=env_str("OPENAI_API_KEY"))
        self.model = model
        self.engine_name = f"gpt-raw:{model}"

    def parse(self, file_path: Path) -> ParsedDocument:
        start = time.time()
        try:
            suffix = file_path.suffix.lower()
            mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
            img_b64 = _encode_image(file_path)

            text = self._call_api(img_b64, mime)
            elapsed = time.time() - start
            tables = _extract_tables(text)

            return ParsedDocument(
                engine=self.engine_name,
                file_path=str(file_path),
                text=text,
                tables=tables,
                processing_time_sec=elapsed,
                page_count=1,
            )
        except Exception as exc:
            elapsed = time.time() - start
            return ParsedDocument(
                engine=self.engine_name,
                file_path=str(file_path),
                text="",
                tables=[],
                processing_time_sec=elapsed,
                page_count=0,
                error=str(exc),
            )

    def _call_api(self, img_b64: str, mime: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        )
        return response.choices[0].message.content


def _encode_image(file_path: Path) -> str:
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _extract_tables(text: str) -> List[str]:
    cleaned = re.sub(r"```(?:html)?\s*", "", text, flags=re.IGNORECASE).replace("```", "")
    html_tables = re.findall(r"<table[\s\S]*?</table>", cleaned, re.IGNORECASE)
    return html_tables
