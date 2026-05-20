from dataclasses import dataclass
from typing import List, Optional
from pydantic import BaseModel


class GroundTruth(BaseModel):
    """GT JSON 스키마 — eval/gt/{파일명}.json"""
    file: str                           # 입력 파일명 (예: "test.pdf")
    text: Optional[str] = None          # 기대 텍스트 전문 (CER 계산용)
    tables: Optional[List[str]] = None  # 기대 HTML 표 리스트 (table_accuracy용)
    has_multipage_table: bool = False   # 다중 페이지 표 포함 여부


@dataclass
class ParsedDocument:
    engine: str
    file_path: str
    text: str
    tables: List[str]
    processing_time_sec: float
    page_count: int
    error: Optional[str] = None
