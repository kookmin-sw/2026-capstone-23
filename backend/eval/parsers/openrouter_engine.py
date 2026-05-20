"""Generic OpenRouter adapter for eval framework.

Usage example (PowerShell):
  $env:OPENROUTER_API_KEY="..."
  $env:OPENROUTER_MODEL="qwen/qwen3-vl-32b-instruct"
"""

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
DEFAULT_MODEL = "qwen/qwen3-vl-32b-instruct"

PROMPT = """Extract all visible text from the image accurately.

If there is a table, output strict HTML only:
- Use only <table>, <tr>, <th>, <td>
- Use colspan/rowspan when needed
- Use valid closing tags</th></td></tr></table>
- Do NOT wrap with markdown fences

If the input is mainly a table, output only table HTML.
"""


class OpenRouterEngineAdapter(BaseEngineAdapter):
    engine_name = "openrouter"

    def __init__(self, model: str | None = None):
        self.api_key = env_str("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable is not set.")

        self.model = model or env_str("OPENROUTER_MODEL", DEFAULT_MODEL)
        self.engine_name = f"openrouter:{self.model}"

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
        tmp_dir = Path(tempfile.mkdtemp(prefix="eval_openrouter_"))

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
            all_tables.extend(_extract_tables(text))

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
        tables = _extract_tables(text)

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
            "model": self.model,
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
        body = resp.json()
        return body["choices"][0]["message"]["content"]


def _encode_image(file_path: Path) -> str:
    with open(file_path, "rb") as file:
        return base64.b64encode(file.read()).decode("utf-8")


def _extract_tables(text: str) -> List[str]:
    cleaned = re.sub(r"```(?:html)?\s*", "", text, flags=re.IGNORECASE).replace("```", "")

    html_tables = re.findall(r"<table[\s\S]*?</table>", cleaned, re.IGNORECASE)
    if html_tables:
        return html_tables

    # Fallback: convert markdown table blocks to very basic HTML tables.
    tables: List[str] = []
    lines = cleaned.splitlines()
    i = 0
    while i + 1 < len(lines):
        if "|" not in lines[i] or "|" not in lines[i + 1]:
            i += 1
            continue

        border = lines[i + 1].strip().replace("|", "").replace(":", "").replace("-", "")
        if border:
            i += 1
            continue

        header = _split_md_row(lines[i])
        rows: List[List[str]] = []
        j = i + 2
        while j < len(lines) and "|" in lines[j]:
            row = _split_md_row(lines[j])
            if len(row) >= 2:
                rows.append(row)
            j += 1

        tables.append(_md_table_to_html(header, rows))
        i = j

    return tables


def _split_md_row(row: str) -> List[str]:
    raw = row.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [cell.strip() for cell in raw.split("|")]


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_table_to_html(header: List[str], rows: List[List[str]]) -> str:
    parts = ["<table><tr>"]
    for cell in header:
        parts.append(f"<th>{_escape_html(cell)}</th>")
    parts.append("</tr>")

    for row in rows:
        parts.append("<tr>")
        width = max(len(header), len(row))
        for idx in range(width):
            val = row[idx] if idx < len(row) else ""
            parts.append(f"<td>{_escape_html(val)}</td>")
        parts.append("</tr>")

    parts.append("</table>")
    return "".join(parts)
