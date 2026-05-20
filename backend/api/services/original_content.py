from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterable

from defusedxml import ElementTree

import fitz

from core.converters import ConversionError, extract_text_from_excel, extract_text_from_hwp_pyhwp


_TEXT_READ_ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "euc-kr", "latin-1")
_TEXTLIKE_SUFFIXES = {
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".csv",
    ".log",
}
_OOXML_WORD_ENTRIES = ("word/document.xml", "word/header1.xml", "word/header2.xml", "word/footer1.xml", "word/footer2.xml")


def _read_text_with_fallback(path: Path) -> tuple[str, str]:
    raw_bytes = path.read_bytes()
    for encoding in _TEXT_READ_ENCODINGS:
        try:
            return raw_bytes.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    raise ConversionError(f"텍스트 인코딩 감지 실패: {path.name}")


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag.rsplit(":", 1)[-1]


def _extract_paragraphs_from_xml_bytes(xml_bytes: bytes, *, paragraph_tags: set[str], text_tags: set[str]) -> list[str]:
    root = ElementTree.fromstring(xml_bytes)
    paragraphs: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) not in paragraph_tags:
            continue
        texts = [node.text or "" for node in element.iter() if _local_name(node.tag) in text_tags and node.text]
        paragraph = "".join(texts).strip()
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs


def _extract_pdf_text(path: Path) -> str:
    doc = fitz.open(path)
    try:
        page_blocks: list[str] = []
        for page_index in range(len(doc)):
            text = doc.load_page(page_index).get_text("text").strip()
            if text:
                page_blocks.append(f"## Page {page_index + 1}\n{text}")
        return "\n\n".join(page_blocks).strip()
    finally:
        doc.close()


def _extract_docx_text(path: Path) -> str:
    paragraphs: list[str] = []
    with zipfile.ZipFile(path, "r") as archive:
        for entry_name in _OOXML_WORD_ENTRIES:
            if entry_name not in archive.namelist():
                continue
            with archive.open(entry_name) as handle:
                paragraphs.extend(
                    _extract_paragraphs_from_xml_bytes(
                        handle.read(),
                        paragraph_tags={"p"},
                        text_tags={"t"},
                    )
                )
    return "\n\n".join(paragraphs).strip()


def _slide_entry_sort_key(name: str) -> tuple[int, str]:
    stem = Path(name).stem
    digits = "".join(ch for ch in stem if ch.isdigit())
    return (int(digits) if digits else 0, name)


def _extract_pptx_text(path: Path) -> str:
    slides: list[str] = []
    with zipfile.ZipFile(path, "r") as archive:
        slide_entries = sorted(
            [name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")],
            key=_slide_entry_sort_key,
        )
        for index, entry_name in enumerate(slide_entries, start=1):
            with archive.open(entry_name) as handle:
                paragraphs = _extract_paragraphs_from_xml_bytes(
                    handle.read(),
                    paragraph_tags={"p"},
                    text_tags={"t"},
                )
            slide_text = "\n".join(paragraphs).strip()
            if slide_text:
                slides.append(f"## Slide {index}\n{slide_text}")
    return "\n\n".join(slides).strip()


def _extract_text_via_candidates(path: Path, candidates: Iterable[str]) -> str:
    for candidate in candidates:
        if candidate == "text":
            text, _encoding = _read_text_with_fallback(path)
            return text
        if candidate == "pdf":
            return _extract_pdf_text(path)
        if candidate == "hwp":
            return extract_text_from_hwp_pyhwp(path)
        if candidate == "excel":
            return extract_text_from_excel(path)
        if candidate == "docx":
            return _extract_docx_text(path)
        if candidate == "pptx":
            return _extract_pptx_text(path)
    raise ConversionError(f"지원하지 않는 원본 내용 추출 형식입니다: {path.suffix.lower()}")


def extract_original_document_content(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()

    if suffix in _TEXTLIKE_SUFFIXES:
        text, encoding = _read_text_with_fallback(path)
        return text, f"text-read:{encoding}"
    if suffix == ".pdf":
        return _extract_text_via_candidates(path, ("pdf",)), "pdf-text"
    if suffix in {".hwp", ".hwpx"}:
        return _extract_text_via_candidates(path, ("hwp",)), "hwp-text"
    if suffix in {".xlsx", ".xls"}:
        return _extract_text_via_candidates(path, ("excel",)), "excel-text"
    if suffix == ".docx":
        text = _extract_text_via_candidates(path, ("docx",))
        if not text:
            raise ConversionError(f"DOCX 원본 텍스트가 비어 있습니다: {path.name}")
        return text, "docx-ooxml"
    if suffix == ".pptx":
        text = _extract_text_via_candidates(path, ("pptx",))
        if not text:
            raise ConversionError(f"PPTX 원본 텍스트가 비어 있습니다: {path.name}")
        return text, "pptx-ooxml"
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif"}:
        raise ConversionError(f"이미지 파일은 원본 내부 텍스트를 직접 반환할 수 없습니다: {path.name}")
    if suffix in {".doc", ".ppt"}:
        raise ConversionError(f"구형 Office 바이너리 형식은 원본 내용 추출 API를 지원하지 않습니다: {path.name}")
    raise ConversionError(f"지원하지 않는 원본 내용 추출 형식입니다: {path.suffix.lower()}")
