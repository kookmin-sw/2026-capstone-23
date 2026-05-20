from abc import ABC, abstractmethod
from pathlib import Path
from eval.schema import ParsedDocument


class BaseEngineAdapter(ABC):
    engine_name: str

    @abstractmethod
    def parse(self, file_path: Path) -> ParsedDocument:
        """파일을 파싱하여 ParsedDocument 반환"""
        ...
