"""Local server adapter — 로컬 GPU 서버 API를 호출하는 eval 어댑터."""
import re
import time
from pathlib import Path

import requests

from eval.parsers.base import BaseEngineAdapter
from eval.schema import ParsedDocument

SERVER_URL = "http://127.0.0.1:8002/v1/parser/convert"


class LocalServerEngineAdapter(BaseEngineAdapter):
    engine_name = "local-server"

    def __init__(self, model_id: str = "m3", server_url: str = SERVER_URL):
        self.model_id = model_id
        self.server_url = server_url
        self.engine_name = "qwen2.5-vl-local"

    def parse(self, file_path: Path) -> ParsedDocument:
        start = time.time()
        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    self.server_url,
                    files={"files": (file_path.name, f)},
                    data={"modelId": self.model_id, "language": "한국어", "duplicatePolicy": "OVERWRITE"},
                    timeout=300,
                )
            resp.raise_for_status()
            data = resp.json()

            item = data["data"]["items"][0]
            txt_path = Path(item["txt"]["path"])
            elapsed = time.time() - start

            text_raw = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""

            tables = re.findall(r"<table[\s\S]*?</table>", text_raw, re.IGNORECASE)

            md_blocks = re.findall(r"\[\[TABLE_MARKDOWN\]\]([\s\S]*?)\[\[/TABLE_MARKDOWN\]\]", text_raw)
            for md in md_blocks:
                html = _md_to_html(md.strip())
                if html:
                    tables.append(html)

            text = re.sub(r"\[\[TABLE\]\][\s\S]*?\[\[/TABLE\]\]", "", text_raw)
            text = re.sub(r"\[\[TABLE_MARKDOWN\]\]([\s\S]*?)\[\[/TABLE_MARKDOWN\]\]", r"\1", text)
            text = re.sub(r"\[\[.*?\]\]", "", text)
            text = re.sub(r"## Page \d+\s*", "", text)
            text = re.sub(r"^.*?(?=##)", "", text, count=1, flags=re.DOTALL)
            text = re.sub(r"\n{2,}", "\n", text).strip()

            return ParsedDocument(
                engine=self.engine_name,
                file_path=str(file_path),
                text=text,
                tables=tables,
                processing_time_sec=elapsed,
                page_count=1,
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


def _md_to_html(md: str) -> str:
    lines = [line for line in md.splitlines() if line.strip()]
    if not lines:
        return ""
    rows = []
    for line in lines:
        if re.match(r"^\|[-| :]+\|$", line.strip()):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return ""
    header = rows[0]
    parts = ["<table><tr>"] + [f"<th>{c}</th>" for c in header] + ["</tr>"]
    for row in rows[1:]:
        parts.append("<tr>")
        for i in range(max(len(header), len(row))):
            parts.append(f"<td>{row[i] if i < len(row) else ''}</td>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)
