"""Qwen3-VL adapter using OpenRouter API."""
import base64
import re
import tempfile
import time
from pathlib import Path
from typing import List

import requests

from core.env import env_str
from eval.parsers.base import BaseEngineAdapter
from eval.schema import ParsedDocument

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
QWEN_MODEL = "qwen/qwen3-vl-32b-instruct"

PROMPT = """Extract all visible text from the image accurately.

If there is a table, output it in strict HTML only:
- Use only <table>, <tr>, <th>, <td>
- Use colspan/rowspan when needed
- Every row must have a valid total column count after spans
- Do NOT use markdown code fences
- Write valid closing tags: </th>, </td>, </tr>, </table>

If the image is primarily a table, output only the table HTML without extra explanation.
If there is non-table text, include it as plain text.
"""


class QwenEngineAdapter(BaseEngineAdapter):
    engine_name = "qwen3-vl"

    def __init__(self):
        self.api_key = env_str("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable is not set.")

    def parse(self, file_path: Path) -> ParsedDocument:
        start = time.time()
        try:
            suffix = file_path.suffix.lower()
            if suffix in (".hwp", ".hwpx", ".pdf"):
                return self._parse_document(file_path, start)
            return self._parse_image(file_path, start)
        except Exception as exc:  # noqa: BLE001
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

    def _parse_document(self, file_path: Path, start: float) -> ParsedDocument:
        from core.converters import convert_hwp_to_pdf_via_libreoffice
        import fitz

        suffix = file_path.suffix.lower()
        tmp_dir = Path(tempfile.mkdtemp(prefix="eval_qwen_"))

        if suffix in (".hwp", ".hwpx"):
            pdf_path = convert_hwp_to_pdf_via_libreoffice(file_path, tmp_dir)
            if not pdf_path:
                raise RuntimeError("HWP to PDF conversion failed")
        else:
            pdf_path = file_path

        doc = fitz.open(pdf_path)
        page_count = len(doc)
        all_texts: List[str] = []
        all_tables: List[str] = []

        for page in doc:
            mat = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")

            text = self._call_api(img_b64, "image/png")
            all_texts.append(text)
            all_tables.extend(_extract_html_tables(text))

        doc.close()
        elapsed = time.time() - start

        return ParsedDocument(
            engine=self.engine_name,
            file_path=str(file_path),
            text="\n".join(all_texts),
            tables=all_tables,
            processing_time_sec=elapsed,
            page_count=page_count,
        )

    def _parse_image(self, file_path: Path, start: float) -> ParsedDocument:
        suffix = file_path.suffix.lower()
        mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
        img_b64 = _encode_image(file_path)

        text = self._call_api(img_b64, mime)
        elapsed = time.time() - start
        tables = _extract_html_tables(text)

        return ParsedDocument(
            engine=self.engine_name,
            file_path=str(file_path),
            text=text,
            tables=tables,
            processing_time_sec=elapsed,
            page_count=1,
        )

    def _call_api(self, img_b64: str, mime: str) -> str:
        payload = {
            "model": QWEN_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        resp = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=300)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _encode_image(file_path: Path) -> str:
    with open(file_path, "rb") as file:
        return base64.b64encode(file.read()).decode("utf-8")


def _extract_html_tables(text: str) -> List[str]:
    text = re.sub(r"```(?:html)?\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("```", "")
    return re.findall(r"<table[\s\S]*?</table>", text, re.IGNORECASE)
