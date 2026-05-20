import hashlib
import io
import re
import shlex
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
import shutil
import fitz  # PyMuPDF

from core.conversion_common import (
    ConversionError,
    find_libreoffice as _find_libreoffice,
    find_tool as _find_tool,
    resolve_html_resource as _resolve_html_resource,
    run_command as _run,
)
from core.csv_converters import parse_csv as parse_csv
from core.spreadsheet_parsers import parse_excel as parse_excel
from core.spreadsheet_table_extractors import parse_excel_tables as parse_excel_tables
from core.spreadsheet_text import extract_text_from_excel as extract_text_from_excel

SUPPORTED_DOC = {".hwp", ".hwpx", ".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".csv"}



def _read_hwp_fileheader_version(file_path: Path) -> tuple[int, int, int, int] | None:
    """Return HWP FileHeader version when the OLE2 header is readable."""
    try:
        import olefile
    except Exception:
        return None

    try:
        if not olefile.isOleFile(str(file_path)):
            return None
        ole = olefile.OleFileIO(str(file_path))
    except Exception:
        return None

    try:
        if not ole.exists("FileHeader"):
            return None
        header = ole.openstream("FileHeader").read(36)
        if len(header) < 36 or not header.startswith(b"HWP Document File"):
            return None
        packed = int.from_bytes(header[32:36], "little")
        return (
            (packed >> 24) & 0xFF,
            (packed >> 16) & 0xFF,
            (packed >> 8) & 0xFF,
            packed & 0xFF,
        )
    except Exception:
        return None
    finally:
        ole.close()


def _looks_like_legacy_raw_hwp(file_path: Path) -> bool:
    """Best-effort detection for pre-OLE HWP files carrying a textual HWP signature."""
    try:
        sample = file_path.read_bytes()[:512]
    except Exception:
        return False
    if sample.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or sample[:2] == b"PK":
        return False
    if b"HWP Document File" not in sample:
        return False
    return bool(re.search(rb"HWP Document File[^\x00\r\n]{0,80}V[0-4]\.", sample))


def _is_legacy_hwp_format(file_path: Path) -> bool:
    """Detect HWP versions older than HWP5 so pyhwp paths can be skipped."""
    if file_path.suffix.lower() != ".hwp" or _is_hwpx_format(file_path):
        return False
    version = _read_hwp_fileheader_version(file_path)
    if version is not None:
        return version[0] < 5
    return _looks_like_legacy_raw_hwp(file_path)


def _hwp_version_label(file_path: Path) -> str:
    version = _read_hwp_fileheader_version(file_path)
    if version is None:
        return "HWP v4 이하"
    return ".".join(str(part) for part in version)


def _is_pyhwp_unsupported_error(message: str) -> bool:
    """Return True for pyhwp parser failures that should go to PDF fallback."""
    lowered = (message or "").lower()
    markers = (
        "propertysetstream",
        "olestream",
        "summaryinfo.events",
        "xmlevents",
        "hwp5/xmlmodel.py",
        "pyhwp가 이 hwp 내부",
    )
    return any(marker in lowered for marker in markers)


def _is_hwpx_format(file_path: Path) -> bool:
    """HWP 파일이 HWPX(ZIP) 형식인지 확인 (OLE2가 아님)"""
    try:
        with open(file_path, 'rb') as f:
            magic = f.read(4)
        return magic[:2] == b'PK'  # ZIP 파일 시그니처
    except Exception:
        return False


def _sanitize_hwpx_text(text: str) -> str:
    """
    HWPX 추출 텍스트 정제: null(0x00) 및 기타 제어문자 제거
    - 차례(목차)의 채움 문자로 쓰이는 null 바이트가 NUL로 표시되는 문제 해결
    - 줄바꿈은 유지, 행 내 연속 공백만 정리
    """
    if not text:
        return text
    # null 바이트 → 공백
    sanitized = text.replace('\x00', ' ')
    # HWP 채움 문자 (PUA: U+E000–U+F8FF, U+0F0000–U+0FFFFF) → 공백
    def _replace_pua(c):
        o = ord(c)
        return ' ' if (0xE000 <= o <= 0xF8FF or 0x0F0000 <= o <= 0x0FFFFF) else c
    sanitized = ''.join(_replace_pua(c) for c in sanitized)
    # 행 단위로 연속 공백 하나로 정리 (줄바꿈 유지)
    lines = [' '.join(line.split()) if line.strip() else line for line in sanitized.split('\n')]
    return '\n'.join(lines).strip()


def _clean_hwp_bodytext_line(text: str) -> str:
    text = (text or "").replace("\x00", " ").replace("\r", "\n")
    text = "".join(char if char == "\n" or ord(char) >= 32 else " " for char in text)
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        meaningful_count = len(re.findall(r"[0-9A-Za-z가-힣○□※「」·.,:()~\-]", line))
        if meaningful_count >= 2:
            lines.append(line)
    return "\n".join(lines).strip()


def _extract_text_from_hwp_bodytext(hwp_path: Path) -> str:
    """Best-effort HWP 5 BodyText parser for pyhwp SummaryInfo failures."""
    try:
        import olefile
    except Exception as exc:  # noqa: BLE001
        raise ConversionError("olefile이 설치되지 않아 HWP BodyText fallback을 사용할 수 없습니다.") from exc

    try:
        ole = olefile.OleFileIO(str(hwp_path))
    except Exception as exc:  # noqa: BLE001
        raise ConversionError(f"HWP OLE 파일을 열 수 없습니다: {exc}") from exc

    try:
        file_header = ole.openstream("FileHeader").read()
        flags = int.from_bytes(file_header[36:40], "little") if len(file_header) >= 40 else 0
        compressed = bool(flags & 1)
        section_names = sorted(
            "/".join(entry)
            for entry in ole.listdir(streams=True, storages=False)
            if len(entry) == 2 and entry[0] == "BodyText" and entry[1].startswith("Section")
        )
        if not section_names:
            raise ConversionError("HWP BodyText 섹션을 찾을 수 없습니다.")

        paragraphs: list[str] = []
        for section_name in section_names:
            data = ole.openstream(section_name).read()
            if compressed:
                data = zlib.decompress(data, -15)

            pos = 0
            while pos + 4 <= len(data):
                header = int.from_bytes(data[pos:pos + 4], "little")
                pos += 4
                tag_id = header & 0x3FF
                size = (header >> 20) & 0xFFF
                if size == 0xFFF:
                    if pos + 4 > len(data):
                        break
                    size = int.from_bytes(data[pos:pos + 4], "little")
                    pos += 4
                payload = data[pos:pos + size]
                pos += size
                # HWPTAG_PARA_TEXT
                if tag_id != 67 or not payload:
                    continue
                cleaned = _clean_hwp_bodytext_line(payload.decode("utf-16le", errors="ignore"))
                if cleaned:
                    paragraphs.append(cleaned)

        text = "\n".join(paragraphs).strip()
        if not text:
            raise ConversionError("HWP BodyText에서 추출 가능한 텍스트가 없습니다.")
        print(f"[INFO] HWP BodyText fallback 텍스트 추출 성공: {len(text)}자")
        return text
    except ConversionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConversionError(f"HWP BodyText fallback 실패: {exc}") from exc
    finally:
        ole.close()


def _extract_text_from_hwpx(hwp_path: Path) -> str:
    """
    HWPX(ZIP) 형식 HWP 파일에서 텍스트 추출
    - section0.xml: 전체 본문 (20페이지 등) - 우선 사용
    - PrvText.txt: 미리보기용 (1~2페이지) - section0 없을 때만
    """
    try:
        from bs4 import BeautifulSoup

        with zipfile.ZipFile(hwp_path, 'r') as zf:
            # 1) Contents/section0.xml 우선 - 전체 페이지 텍스트 포함
            if 'Contents/section0.xml' in zf.namelist():
                with zf.open('Contents/section0.xml') as f:
                    soup = BeautifulSoup(f.read(), 'xml')
                    # hp:p(문단) 단위로 hp:t 텍스트 추출하여 조합
                    lines = []
                    for p in soup.find_all(['hp:p'], recursive=True):
                        t_texts = [t.get_text() for t in p.find_all(['hp:t'], recursive=True)]
                        line = _sanitize_hwpx_text(''.join(t_texts))
                        if line:
                            lines.append(line)
                    if lines:
                        text = '\n'.join(lines)
                        print(f"[DEBUG] HWPX section0.xml에서 {len(lines)}개 문단, {len(text)}자 추출")
                        return text
                    # hp:t가 없으면 get_text로 폴백
                    text = _sanitize_hwpx_text(soup.get_text(separator='\n', strip=True))
                    if text and len(text.strip()) > 50:
                        return text

            # 2) Preview/PrvText.txt (미리보기, 1~2페이지 분량)
            if 'Preview/PrvText.txt' in zf.namelist():
                with zf.open('Preview/PrvText.txt') as f:
                    return _sanitize_hwpx_text(f.read().decode('utf-8', errors='replace'))

            # 3) Contents/header.xml 등
            for name in ['Contents/header.xml']:
                if name in zf.namelist():
                    with zf.open(name) as f:
                        soup = BeautifulSoup(f.read(), 'xml')
                        text = _sanitize_hwpx_text(soup.get_text(separator='\n', strip=True))
                        if text and len(text.strip()) > 10:
                            return text
        raise ConversionError("HWPX에서 텍스트를 추출할 수 없습니다.")
    except zipfile.BadZipFile:
        raise ConversionError("유효한 HWPX(ZIP) 파일이 아닙니다.")
    except Exception as e:
        raise ConversionError(f"HWPX 텍스트 추출 실패: {e}")


def extract_text_from_hwp_pyhwp(hwp_path: Path) -> str:
    """HWP 파일에서 텍스트 추출 (pyhwp 사용)"""
    try:
        # 파일이 실제로 존재하는지 확인
        if not hwp_path.exists():
            raise ConversionError(f"HWP 파일을 찾을 수 없습니다: {hwp_path}")
        
        # 파일 크기 확인
        if hwp_path.stat().st_size == 0:
            raise ConversionError(f"HWP 파일이 비어있습니다: {hwp_path}")
        
        # hwp5txt 명령어 사용
        result = subprocess.run(
            [_find_tool("hwp5txt"), str(hwp_path)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=60
        )
        
        if result.returncode == 0:
            return result.stdout
        else:
            error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
            if _is_pyhwp_unsupported_error(error_msg):
                print(
                    "[WARNING] pyhwp가 이 HWP 내부 OLE/SummaryInfo 구조를 지원하지 않아 "
                    "BodyText 직접 파싱으로 폴백합니다."
                )
                return _extract_text_from_hwp_bodytext(hwp_path)
            
            # "Not an OLE2 Compound Binary File" 오류 = HWPX(ZIP) 형식
            if "Not an OLE2" in error_msg or "OLE2" in error_msg:
                print(f"[WARNING] hwp5txt가 OLE2 형식을 인식하지 못함 (HWPX 형식 가능성): {hwp_path.name}")
                # HWPX 형식이면 직접 추출 시도
                if _is_hwpx_format(hwp_path):
                    print("[INFO] HWPX(ZIP) 형식으로 감지됨. PrvText.txt/XML에서 텍스트 추출 시도")
                    return _extract_text_from_hwpx(hwp_path)
                # hwp5html로 대체 시도 (일부 OLE2 변형에서 성공할 수 있음)
                try:
                    html_dir = hwp_path.parent / f"{hwp_path.stem}_temp_html"
                    html_result = subprocess.run(
                        [_find_tool("hwp5html"), str(hwp_path), "--output", str(html_dir)],
                        capture_output=True,
                        text=True,
                        timeout=60
                    )
                    if html_result.returncode == 0:
                        index_html = html_dir / "index.xhtml"
                        if index_html.exists():
                            from bs4 import BeautifulSoup
                            with open(index_html, 'r', encoding='utf-8') as f:
                                soup = BeautifulSoup(f, 'xml')
                            text = soup.get_text(separator='\n', strip=True)
                            shutil.rmtree(html_dir, ignore_errors=True)
                            return text
                except Exception as html_e:
                    print(f"[WARNING] hwp5html 변환도 실패: {html_e}")
                # 최종: HWPX로 한 번 더 시도 (파일 시그니처 기반)
                if _is_hwpx_format(hwp_path):
                    return _extract_text_from_hwpx(hwp_path)
            
            raise ConversionError(f"hwp5txt 실행 실패: {error_msg}")
    except FileNotFoundError:
        raise ConversionError("pyhwp가 설치되지 않았습니다. pip install pyhwp")
    except subprocess.TimeoutExpired:
        raise ConversionError(f"HWP 텍스트 추출 시간 초과: {hwp_path}")
    except Exception as e:
        raise ConversionError(f"HWP 텍스트 추출 실패: {e}")


def convert_hwp_to_pdf_via_text(hwp_path: Path, dst_dir: Path) -> Path:
    """HWP 파일에서 텍스트 추출 후 간단한 PDF 생성"""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.utils import simpleSplit
    
    text = extract_text_from_hwp_pyhwp(hwp_path)
    
    # PDF 생성
    dst_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = dst_dir / f"{hwp_path.stem}.pdf"
    
    # 한글 폰트 등록 시도 (TTF → TTC → CID 순서로 폴백)
    font_name = 'Helvetica'
    try:
        # 1) TTF 폰트 시도
        font_paths = [
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/nanum/NanumMyeongjo.ttf",
            "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
            "C:\\Windows\\Fonts\\malgun.ttf",  # Windows 맑은 고딕
        ]
        for font_path in font_paths:
            if Path(font_path).exists():
                pdfmetrics.registerFont(TTFont('Korean', font_path))
                font_name = 'Korean'
                print(f"[DEBUG] 한글 TTF 폰트 로드 성공: {font_path}")
                break

        # 2) TTF 실패 시 CID 한글 폰트 사용 (ReportLab 내장, 별도 파일 불필요)
        if font_name == 'Helvetica':
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            pdfmetrics.registerFont(UnicodeCIDFont('HYGothic-Medium'))
            font_name = 'HYGothic-Medium'
            print("[DEBUG] 한글 CID 폰트 로드 성공: HYGothic-Medium")
    except Exception as e:
        print(f"한글 폰트 로드 실패: {e}, 기본 폰트 사용")
    
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    width, height = A4
    
    # 텍스트를 페이지별로 분할
    lines = text.split('\n')
    y = height - 40
    line_height = 14
    font_size = 10
    max_width = width - 80  # 좌우 여백 40씩
    
    c.setFont(font_name, font_size)
    
    for line in lines:
        if not line.strip():
            y -= line_height / 2  # 빈 줄은 절반 높이
            continue
            
        if y < 40:  # 페이지 끝
            c.showPage()
            c.setFont(font_name, font_size)
            y = height - 40
        
        try:
            # 긴 라인은 자동 줄바꿈
            if font_name != 'Helvetica':
                # 한글 폰트 사용 시 줄바꿈 처리 (TTF 'Korean' 또는 CID 'HYGothic-Medium')
                wrapped_lines = simpleSplit(line, font_name, font_size, max_width)
            else:
                # 기본 폰트는 간단히 자르기
                wrapped_lines = [line[i:i+100] for i in range(0, len(line), 100)]
            
            for wrapped_line in wrapped_lines:
                if y < 40:
                    c.showPage()
                    c.setFont(font_name, font_size)
                    y = height - 40
                c.drawString(40, y, wrapped_line)
                y -= line_height
        except Exception as e:
            # 폰트가 문자를 지원하지 않으면 스킵
            print(f"라인 렌더링 실패: {e}")
            y -= line_height
    
    c.save()
    return pdf_path


def _table_to_markdown_from_rows(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    max_cols = max((len(row) for row in rows), default=0)
    if max_cols == 0:
        return ""
    normalized = [row + [""] * (max_cols - len(row)) for row in rows]
    header = normalized[0]
    lines = [
        "| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in row) + " |")
    return "\n".join(lines)


def _parse_hwp_html_table(table) -> tuple[str, str, str]:
    from bs4 import BeautifulSoup

    table_copy = BeautifulSoup(str(table), "html.parser").find("table")
    if table_copy is None:
        return "", "", ""

    for attr in ("class", "style", "cellspacing", "cellpadding"):
        table_copy.attrs.pop(attr, None)
    rows: list[list[str]] = []
    for tag in table_copy.find_all(True):
        tag.attrs = {
            key: value
            for key, value in tag.attrs.items()
            if key in {"rowspan", "colspan"}
        }
    for tr in table_copy.find_all("tr"):
        row: list[str] = []
        for cell in tr.find_all(["td", "th"], recursive=False):
            row.append(" ".join(cell.get_text(" ", strip=True).split()))
        if any(row):
            rows.append(row)

    markdown = _table_to_markdown_from_rows(rows)
    flat_text = "\n".join(" ".join(row).strip() for row in rows if any(row)).strip()
    return str(table_copy), markdown, flat_text


def _parse_hwp_document_from_html(hwp_path: Path, *, tmp_dir: Path | None = None) -> dict:
    from bs4 import BeautifulSoup

    html_root = tmp_dir or hwp_path.parent
    html_root.mkdir(parents=True, exist_ok=True)
    html_dir = html_root / f"{hwp_path.stem}_direct_html"
    if html_dir.exists():
        shutil.rmtree(html_dir, ignore_errors=True)

    result = subprocess.run(
        [_find_tool("hwp5html"), str(hwp_path), "--output", str(html_dir)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    index_html = html_dir / "index.xhtml"
    if result.returncode != 0 or not index_html.exists():
        stderr = result.stderr.strip() if result.stderr else result.stdout.strip()
        if _is_pyhwp_unsupported_error(stderr):
            raise ConversionError(
                "pyhwp가 이 HWP 내부 OLE/SummaryInfo 구조를 지원하지 않습니다. "
                "PDF/LibreOffice 변환 경로로 전환해야 합니다."
            )
        raise ConversionError(f"hwp5html 실행 실패: {stderr}")

    try:
        soup = BeautifulSoup(index_html.read_text(encoding="utf-8"), "html.parser")
        page_root = soup.select_one(".Page") or soup.body or soup
        elements: list[dict] = []
        tables: list[dict] = []
        seen_tables: set[int] = set()

        def append_paragraph(node) -> None:
            for nested_table in node.find_all("table"):
                seen_tables.add(id(nested_table))
            text = " ".join(node.get_text(" ", strip=True).split())
            if text:
                elements.append({"type": "text", "content": text, "bbox": [0, 0, 0, 0]})

        def append_table(node) -> None:
            table_html, markdown, flat_text = _parse_hwp_html_table(node)
            if not table_html:
                return
            seen_tables.add(id(node))
            table = {
                "html": table_html,
                "markdown": markdown,
                "flat_text": flat_text,
                "first_line": flat_text.splitlines()[0] if flat_text else "",
            }
            tables.append(table)
            elements.append(
                {
                    "type": "table",
                    "content": table_html,
                    "markdown": markdown,
                    "bbox": [0, 0, 0, 0],
                }
            )

        for child in page_root.find_all(["p", "table"], recursive=False):
            if child.name == "p":
                inline_tables = child.find_all("table")
                if inline_tables:
                    text = " ".join(child.get_text(" ", strip=True).split())
                    for table in inline_tables:
                        table_text = " ".join(table.get_text(" ", strip=True).split())
                        if table_text:
                            text = text.replace(table_text, " ").strip()
                    if text:
                        elements.append({"type": "text", "content": text, "bbox": [0, 0, 0, 0]})
                    for table in inline_tables:
                        append_table(table)
                else:
                    append_paragraph(child)
            elif child.name == "table" and id(child) not in seen_tables:
                append_table(child)

        if not elements:
            raise ConversionError("hwp5html 결과에서 추출 가능한 요소가 없습니다.")

        text = "\n".join(
            str(element.get("content", ""))
            for element in elements
            if element.get("type") == "text"
        )
        return {
            "page_count": 1,
            "pages": [
                {
                    "page": 1,
                    "text": text,
                    "images": [],
                    "elements": elements,
                }
            ],
            "images": [],
            "tables": tables,
            "source_type": "hwp_direct_html",
        }
    finally:
        shutil.rmtree(html_dir, ignore_errors=True)


def parse_hwp_document_direct(hwp_path: Path, *, lines_per_page: int = 80, tmp_dir: Path | None = None) -> dict:
    """
    HWP 바이너리를 PDF로 렌더링하지 않고 hwp5html XHTML을 우선 파싱한다.
    hwp5html이 실패하면 pyhwp 텍스트와 BinData 이미지만 직접 추출한다.
    """
    if _is_hwpx_format(hwp_path):
        raise ConversionError("HWPX는 parse_hwpx_document 경로를 사용해야 합니다.")
    if _is_legacy_hwp_format(hwp_path):
        raise ConversionError(f"{_hwp_version_label(hwp_path)} 문서는 PDF 변환 경로를 사용해야 합니다.")

    source_type = "hwp_direct_pyhwp"
    try:
        return _parse_hwp_document_from_html(hwp_path, tmp_dir=tmp_dir)
    except Exception as exc:  # noqa: BLE001
        if _is_pyhwp_unsupported_error(str(exc)):
            print(f"[WARNING] hwp5html direct 파싱 실패, BodyText fallback 사용: {exc}")
            text = _extract_text_from_hwp_bodytext(hwp_path)
            source_type = "hwp_direct_bodytext"
        else:
            print(f"[WARNING] hwp5html direct 파싱 실패, hwp5txt fallback 사용: {exc}")
            text = extract_text_from_hwp_pyhwp(hwp_path)
    normalized_lines = [line.rstrip() for line in text.splitlines()]
    nonempty_lines = [line for line in normalized_lines if line.strip()]

    try:
        raw_images = extract_images_from_hwp(hwp_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARNING] HWP 이미지 직접 추출 실패: {exc}")
        raw_images = []

    images: list[dict] = []
    for idx, image_data in enumerate(raw_images):
        image_bytes = image_data.get("bytes") if isinstance(image_data, dict) else None
        if image_bytes is None and isinstance(image_data, bytes):
            image_bytes = image_data
        if not image_bytes:
            continue
        image_id = image_data.get("image_id") if isinstance(image_data, dict) else None
        if not image_id:
            image_id = f"hwp-img-{idx + 1}"
        images.append(
            {
                "image_id": image_id,
                "page": 1,
                "bbox": image_data.get("bbox", [0, 0, 0, 0]) if isinstance(image_data, dict) else [0, 0, 0, 0],
                "bytes": image_bytes,
                "sha1": hashlib.sha1(image_bytes, usedforsecurity=False).hexdigest(),
            }
        )

    if not nonempty_lines and not images:
        raise ConversionError("HWP에서 직접 추출 가능한 텍스트/이미지가 없습니다.")

    pages: list[dict] = []
    line_chunks = [
        nonempty_lines[idx:idx + lines_per_page]
        for idx in range(0, len(nonempty_lines), lines_per_page)
    ] or [[]]
    page_count = max(1, len(line_chunks))

    for page_index, chunk in enumerate(line_chunks, start=1):
        elements: list[dict] = []
        if chunk:
            elements.append(
                {
                    "type": "text",
                    "content": "\n".join(chunk),
                    "bbox": [0, 0, 0, 0],
                }
            )
        pages.append(
            {
                "page": page_index,
                "text": "\n".join(chunk),
                "images": [],
                "elements": elements,
            }
        )

    for image in images:
        parsed_page = image.get("page")
        if not isinstance(parsed_page, int) or parsed_page < 1:
            parsed_page = 1
        target_page = min(parsed_page, page_count)
        image["page"] = target_page
        target = pages[target_page - 1]
        target["images"].append(image)
        target["elements"].append(
            {
                "type": "image",
                "image_id": image["image_id"],
                "bbox": image.get("bbox", [0, 0, 0, 0]),
            }
        )

    return {
        "page_count": page_count,
        "pages": pages,
        "images": images,
        "tables": [],
        "source_type": source_type,
    }


def _extract_hwpx_pdf_elements(hwp_path: Path) -> list[dict]:
    """HWPX section XML에서 PDF fallback용 텍스트/이미지 요소를 문서 순서대로 추출."""
    from bs4 import BeautifulSoup
    from bs4 import NavigableString

    def local_name(node) -> str:
        name = getattr(node, "name", "") or ""
        return name.split(":")[-1] if ":" in name else name

    def section_sort_key(name: str) -> tuple[int, str]:
        match = re.search(r"section(\d+)\.xml$", name)
        return (int(match.group(1)) if match else sys.maxsize, name)

    def image_element(pic_node, names: list[str], bin_refs: dict[str, str], zf: zipfile.ZipFile) -> Optional[dict]:
        img = pic_node.find(lambda item: local_name(item) == "img")
        if not img:
            return None
        ref = img.get("binaryItemIDRef")
        image_path = bin_refs.get(ref)
        if image_path and image_path in names:
            return {
                "type": "image",
                "ref": ref,
                "name": Path(image_path).name,
                "bytes": zf.read(image_path),
            }
        return None

    def append_ordered_paragraph_elements(node, names: list[str], bin_refs: dict[str, str], zf: zipfile.ZipFile) -> None:
        text_parts = []

        def flush_text() -> None:
            text = _sanitize_hwpx_text("".join(text_parts))
            text_parts.clear()
            if text:
                elements.append({"type": "text", "text": text})

        for child in node.descendants:
            if isinstance(child, NavigableString):
                continue
            tag = local_name(child)
            if tag == "t":
                text_parts.append(child.get_text())
            elif tag == "pic":
                flush_text()
                elem = image_element(child, names, bin_refs, zf)
                if elem:
                    elements.append(elem)
        flush_text()

    elements = []
    with zipfile.ZipFile(hwp_path, "r") as zf:
        names = zf.namelist()
        section_names = sorted(
            (
                name
                for name in names
                if name.startswith("Contents/section") and name.endswith(".xml")
            ),
            key=section_sort_key,
        )
        if not section_names:
            raise ConversionError("HWPX section XML을 찾을 수 없습니다.")

        bin_refs = _hwpx_bin_ref_map(zf)
        for section_name in section_names:
            soup = BeautifulSoup(zf.read(section_name), "xml")
            root = soup.find()
            if root is None:
                continue

            for node in root.descendants:
                if isinstance(node, NavigableString):
                    continue
                tag = local_name(node)
                if tag == "p":
                    # 중첩 문단(표 셀 등)은 상위 문단에서 처리되지 않도록 직접 자식 기준으로만 다룬다.
                    parent_p = node.find_parent(lambda p: local_name(p) == "p")
                    if parent_p is not None:
                        continue
                    append_ordered_paragraph_elements(node, names, bin_refs, zf)
                elif tag == "pic" and node.find_parent(lambda p: local_name(p) == "p") is None:
                    elem = image_element(node, names, bin_refs, zf)
                    if elem:
                        elements.append(elem)
    return elements


def _hwpx_bin_ref_map(zf: zipfile.ZipFile) -> dict[str, str]:
    """HWPX binaryItemIDRef 값을 ZIP 내부 BinData 경로로 매핑."""
    from bs4 import BeautifulSoup

    mapping = {}
    names = zf.namelist()
    if "Contents/content.hpf" in names:
        soup = BeautifulSoup(zf.read("Contents/content.hpf"), "xml")
        for item in soup.find_all(True):
            item_id = item.get("id")
            href = item.get("href")
            if item_id and href and href.startswith("BinData/"):
                mapping[item_id] = href

    bin_names = [
        name
        for name in names
        if name.startswith("BinData/") and not name.endswith("/")
    ]
    for idx, name in enumerate(bin_names, 1):
        stem = Path(name).stem
        mapping.setdefault(stem, name)
        mapping.setdefault(f"BinData{idx}", name)
    return mapping


def convert_hwpx_to_pdf_via_elements(hwp_path: Path, dst_dir: Path) -> Path:
    """LibreOffice 실패 시 HWPX 텍스트와 BinData 이미지를 직접 PDF로 렌더링."""
    elements = _extract_hwpx_pdf_elements(hwp_path)
    if not elements:
        raise ConversionError("HWPX에서 PDF로 렌더링할 요소를 찾을 수 없습니다.")

    dst_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = dst_dir / f"{hwp_path.stem}.pdf"

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    fontname = "helv"
    try:
        page.insert_textbox(fitz.Rect(0, 0, 80, 20), "한글", fontname="korea", fontsize=8)
        fontname = "korea"
        doc.delete_page(0)
        page = doc.new_page(width=595, height=842)
    except Exception:
        doc.delete_page(0)
        page = doc.new_page(width=595, height=842)

    margin_x = 40
    margin_y = 40
    y = margin_y
    page_w = 595
    page_h = 842
    usable_w = page_w - (margin_x * 2)
    line_height = 15
    font_size = 10

    def ensure_space(required_h: float):
        nonlocal page, y
        if y + required_h > page_h - margin_y:
            page = doc.new_page(width=page_w, height=page_h)
            y = margin_y

    for elem in elements:
        if elem["type"] == "text":
            text = elem.get("text", "")
            if not text.strip():
                y += line_height / 2
                continue
            chunks = []
            for raw_line in text.split("\n"):
                chunks.extend([raw_line[i:i + 58] for i in range(0, len(raw_line), 58)] or [""])
            ensure_space(max(line_height, len(chunks) * line_height))
            for line in chunks:
                ensure_space(line_height)
                if line.strip():
                    page.insert_text(
                        (margin_x, y),
                        line,
                        fontname=fontname,
                        fontsize=font_size,
                        color=(0, 0, 0),
                    )
                y += line_height
            y += 4
        elif elem["type"] == "image":
            try:
                image_doc = fitz.open(stream=elem["bytes"], filetype=Path(elem.get("name", "")).suffix.lstrip(".") or None)
                rect = image_doc[0].rect
                image_w = rect.width
                image_h = rect.height
                image_doc.close()
            except Exception:
                try:
                    from PIL import Image
                    img = Image.open(io.BytesIO(elem["bytes"]))
                    image_w, image_h = img.size
                except Exception as e:
                    print(f"[WARNING] HWPX 이미지 크기 확인 실패 ({elem.get('name')}): {e}")
                    continue
            draw_w = usable_w
            draw_h = draw_w * (image_h / max(image_w, 1))
            max_h = page_h - (margin_y * 2)
            if draw_h > max_h:
                scale = max_h / draw_h
                draw_w *= scale
                draw_h = max_h
            ensure_space(draw_h + 12)
            rect = fitz.Rect(margin_x, y, margin_x + draw_w, y + draw_h)
            page.insert_image(rect, stream=elem["bytes"])
            y += draw_h + 12

    doc.save(pdf_path, garbage=4, deflate=True)
    doc.close()
    return pdf_path


def extract_images_from_hwp(hwp_path: Path) -> list:
    """HWP 파일에서 이미지 추출 (OLE2 및 HWPX 형식 지원)"""
    # HWPX(ZIP) 형식 먼저 시도
    if _is_hwpx_format(hwp_path):
        try:
            return _extract_images_from_hwpx(hwp_path)
        except Exception as e:
            print(f"HWPX 이미지 추출 실패: {e}")
            return []

    # OLE2 형식
    try:
        import olefile
        ole = olefile.OleFileIO(str(hwp_path))
        images = []
        
        for entry in ole.listdir():
            if entry[0] == 'BinData':
                try:
                    stream_name = '/'.join(entry)
                    stream = ole.openstream(entry)
                    img_bytes = stream.read()
                    if img_bytes[:8] == b'\x89PNG\r\n\x1a\n' or img_bytes[:2] == b'\xff\xd8':
                        images.append({
                            'name': entry[-1],
                            'bytes': img_bytes,
                            'stream': stream_name
                        })
                except Exception as e:
                    print(f"이미지 추출 오류: {e}")
                    continue
        ole.close()
        return images
    except Exception as e:
        print(f"HWP 이미지 추출 실패: {e}")
        return []


def _convert_wmf_to_png(wmf_bytes: bytes) -> Optional[bytes]:
    """WMF/EMF 파일을 PNG로 변환 (LibreOffice 또는 Pillow 사용)"""
    import tempfile
    import subprocess
    
    try:
        # 1차: Pillow로 시도 (일부 환경에서 WMF 지원)
        from PIL import Image
        import io
        try:
            img = Image.open(io.BytesIO(wmf_bytes))
            png_buffer = io.BytesIO()
            img.save(png_buffer, format='PNG')
            return png_buffer.getvalue()
        except Exception:
            pass
        
        # 2차: LibreOffice로 변환 시도
        _lo = _find_libreoffice()
        if _lo:
            with tempfile.TemporaryDirectory() as tmpdir:
                wmf_path = Path(tmpdir) / "input.wmf"
                wmf_path.write_bytes(wmf_bytes)

                # LibreOffice로 PNG 변환
                subprocess.run(
                    [_lo, "--headless", "--convert-to", "png",
                     "--outdir", tmpdir, str(wmf_path)],
                    capture_output=True,
                    timeout=30
                )
                
                png_path = Path(tmpdir) / "input.png"
                if png_path.exists():
                    return png_path.read_bytes()
        
        # 3차: ImageMagick으로 시도
        if shutil.which("convert"):
            with tempfile.TemporaryDirectory() as tmpdir:
                wmf_path = Path(tmpdir) / "input.wmf"
                png_path = Path(tmpdir) / "output.png"
                wmf_path.write_bytes(wmf_bytes)
                
                subprocess.run(
                    ["convert", str(wmf_path), str(png_path)],
                    capture_output=True,
                    timeout=30
                )
                
                if png_path.exists():
                    return png_path.read_bytes()
        
        print("[WARNING] WMF 변환 실패: 지원 도구 없음")
        return None
    except Exception as e:
        print(f"[WARNING] WMF 변환 오류: {e}")
        return None


def _extract_images_from_hwpx(hwp_path: Path) -> list:
    """HWPX(ZIP) 형식에서 BinData 이미지 추출 (문서 순서 유지, WMF 포함)"""
    images = []
    with zipfile.ZipFile(hwp_path, 'r') as zf:
        names = [n for n in zf.namelist() if n.startswith('BinData/') and not n.endswith('/')]
        # image1, image2, ... 순서로 정렬
        def sort_key(n):
            m = re.search(r'image(\d+)', n.split('/')[-1], re.I)
            return int(m.group(1)) if m else 9999
        names = sorted(names, key=sort_key)
        for name in names:
            try:
                with zf.open(name) as f:
                    img_bytes = f.read()
            except Exception as e:
                print(f"HWPX 이미지 추출 오류 {name}: {e}")
                continue
            sig = img_bytes[:2] if len(img_bytes) >= 2 else b''
            sig4 = img_bytes[:4] if len(img_bytes) >= 4 else b''
            sig8 = img_bytes[:8] if len(img_bytes) >= 8 else b''
            
            # PNG, JPEG, BMP - 직접 지원
            if sig8 == b'\x89PNG\r\n\x1a\n' or sig == b'\xff\xd8' or sig == b'BM':
                images.append({
                    'name': name.split('/')[-1],
                    'bytes': img_bytes,
                    'stream': name
                })
            # WMF/EMF - PNG로 변환 후 추가
            elif sig4 == b'\xd7\xcd\xc6\x9a' or sig4 == b'\x01\x00\x09\x00' or \
                 sig4 == b'\x01\x00\x00\x00' or name.lower().endswith(('.wmf', '.emf')):
                print(f"[DEBUG] WMF/EMF 이미지 발견: {name}, PNG 변환 시도...")
                png_bytes = _convert_wmf_to_png(img_bytes)
                if png_bytes:
                    images.append({
                        'name': name.split('/')[-1].rsplit('.', 1)[0] + '.png',
                        'bytes': png_bytes,
                        'stream': name,
                        'converted_from': 'wmf'
                    })
                    print(f"[DEBUG] WMF → PNG 변환 성공: {len(png_bytes)} bytes")
                else:
                    # 변환 실패해도 원본 바이트로 추가 (VLM에서 처리 시도)
                    images.append({
                        'name': name.split('/')[-1],
                        'bytes': img_bytes,
                        'stream': name,
                        'format': 'wmf'
                    })
                    print(f"[WARNING] WMF 변환 실패, 원본으로 추가: {name}")
    return images


def get_hwpx_image_page_mapping(
    hwp_path: Path, page_count: int, extracted_images: Optional[list] = None
) -> dict:
    """
    HWPX section0.xml에서 이미지 위치를 파싱하여 페이지별 이미지 인덱스 매핑 반환
    - extracted_images: _extract_images_from_hwpx 결과. 있으면 추출된 이미지 인덱스로 변환
    - 반환: {page_num: [extracted_image_idx, ...]} (1-based page)
    """
    if not _is_hwpx_format(hwp_path) or page_count < 1:
        return {}
    try:
        from bs4 import BeautifulSoup
        from bs4 import NavigableString

        with zipfile.ZipFile(hwp_path, 'r') as zf:
            if 'Contents/section0.xml' not in zf.namelist():
                return {}
            with zf.open('Contents/section0.xml') as f:
                soup = BeautifulSoup(f.read(), 'xml')

        cumulative_y = 0
        image_positions = []
        pic_order = 0

        for child in soup.descendants:
            if isinstance(child, NavigableString):
                continue
            tag = child.name.split(':')[-1] if child.name and ':' in str(child.name) else (child.name or '')
            if tag == 'lineseg':
                vs = int(child.get('vertsize', 0) or 0)
                cumulative_y += vs
            elif tag == 'pic':
                pic_order += 1
                image_positions.append((pic_order - 1, cumulative_y))

        if not image_positions:
            return {}
        total_y = cumulative_y
        page_height = total_y / page_count if total_y else 1
        mapping = {p: [] for p in range(1, page_count + 1)}

        if extracted_images:
            names = [img.get('name', '') for img in extracted_images]
            for doc_pic_idx, y in image_positions:
                expected_num = doc_pic_idx + 1
                extracted_idx = None
                for i, name in enumerate(names):
                    base = name.rsplit('.', 1)[0].lower()
                    if base == f'image{expected_num}':
                        extracted_idx = i
                        break
                if extracted_idx is not None:
                    page_num = min(page_count, max(1, int(y / page_height) + 1))
                    mapping[page_num].append(extracted_idx)
        else:
            for img_idx, y in image_positions:
                page_num = min(page_count, max(1, int(y / page_height) + 1))
                mapping[page_num].append(img_idx)
        return mapping
    except Exception as e:
        print(f"[WARNING] HWPX 이미지 페이지 매핑 실패: {e}")
        return {}


def _tag_local(tag) -> str:
    """XML 태그의 로컬 이름 반환 (hp:tbl -> tbl)"""
    if not getattr(tag, "name", None):
        return ""
    return tag.name.split(":")[-1] if ":" in str(tag.name) else (tag.name or "")


def parse_hwpx_tables(hwp_path: Path) -> List[dict]:
    """
    HWPX section0.xml에서 hp:tbl 표를 문서 순서대로 추출하여 HTML/마크다운 정보 반환.
    - 반환: [{"html": "<table>...</table>", "markdown": "...", "flat_text": "셀1\\n셀2...", "first_line": "첫행텍스트"}, ...]
    """
    if not _is_hwpx_format(hwp_path):
        return []
    try:
        from bs4 import BeautifulSoup
        from bs4 import NavigableString

        with zipfile.ZipFile(hwp_path, "r") as zf:
            if "Contents/section0.xml" not in zf.namelist():
                return []
            with zf.open("Contents/section0.xml") as f:
                soup = BeautifulSoup(f.read(), "xml")

        tables = []
        for node in soup.descendants:
            if isinstance(node, NavigableString):
                continue
            if _tag_local(node) != "tbl":
                continue

            rows = []
            flat_lines = []
            tr_list = [c for c in node.children if getattr(c, "name", None) and _tag_local(c) == "tr"]
            for tr in tr_list:
                cells = []
                tc_list = [c for c in tr.children if getattr(c, "name", None) and _tag_local(c) == "tc"]
                for tc in tc_list:
                    # 셀 텍스트: subList > p > (run >) t
                    cell_text_parts = []
                    for t in tc.find_all(True):
                        if _tag_local(t) == "t":
                            cell_text_parts.append(t.get_text().strip())
                    cell_text = " ".join(cell_text_parts).strip()
                    # PUA/채움문자 제거
                    cell_text = _sanitize_hwpx_text(cell_text) if cell_text else ""
                    flat_lines.append(cell_text)

                    col_span = 1
                    row_span = 1
                    for span in tc.find_all(True):
                        if _tag_local(span).lower() == "cellspan":
                            col_span = int(span.get("colspan") or span.get("colSpan") or 1)
                            row_span = int(span.get("rowspan") or span.get("rowSpan") or 1)
                            break
                    # header 셀 여부 (헤더면 th)
                    header = tc.get("header") == "1"
                    tag = "th" if header else "td"
                    span_attr = ""
                    if col_span > 1:
                        span_attr += f' colspan="{col_span}"'
                    if row_span > 1:
                        span_attr += f' rowspan="{row_span}"'
                    cells.append((tag, span_attr, cell_text))
                if cells:
                    rows.append(cells)

            if not rows:
                continue

            # HTML 생성
            html_parts = ["<table>"]
            for row in rows:
                cell_strs = []
                for tag, span_attr, text in row:
                    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    cell_strs.append(f"<{tag}{span_attr}>{escaped}</{tag}>")
                html_parts.append("  <tr>\n    " + "\n    ".join(cell_strs) + "\n  </tr>")
            html_parts.append("</table>")
            table_html = "\n".join(html_parts)

            # Markdown: 첫 행을 헤더로, | --- | 구분선
            md_lines = []
            for i, row in enumerate(rows):
                row_texts = [t for (_, _, t) in row]
                md_lines.append("| " + " | ".join(row_texts) + " |")
                if i == 0:
                    md_lines.append("| " + " | ".join("---" for _ in row_texts) + " |")
            table_markdown = "\n".join(md_lines)

            flat_text = "\n".join(flat_lines).strip()
            first_line = flat_lines[0].strip() if flat_lines else ""

            tables.append({
                "html": table_html,
                "markdown": table_markdown,
                "flat_text": flat_text,
                "first_line": first_line,
            })
        print(f"[DEBUG] HWPX section0.xml에서 {len(tables)}개 표 추출")
        return tables
    except Exception as e:
        print(f"[WARNING] HWPX 표 파싱 실패: {e}")
        return []


def parse_hwpx_document(hwp_path: Path) -> dict:
    """
    HWPX ZIP/XML을 직접 파싱해 output.py가 소비하는 page/elements 구조로 변환.
    PDF 렌더링이나 VLM 호출 없이 본문 텍스트와 XML 표 구조를 보존한다.
    """
    if not _is_hwpx_format(hwp_path):
        raise ConversionError("HWPX ZIP/XML 형식이 아닙니다.")

    try:
        from bs4 import BeautifulSoup
        from bs4 import NavigableString

        def section_sort_key(name: str) -> tuple[int, str]:
            match = re.search(r"section(\d+)\.xml$", name)
            return (int(match.group(1)) if match else sys.maxsize, name)

        def has_ancestor(node, names: set[str]) -> bool:
            parent = getattr(node, "parent", None)
            while parent is not None:
                if getattr(parent, "name", None) and _tag_local(parent) in names:
                    return True
                parent = getattr(parent, "parent", None)
            return False

        def has_ancestor_between(node, stop_node, names: set[str]) -> bool:
            parent = getattr(node, "parent", None)
            while parent is not None and parent is not stop_node:
                if getattr(parent, "name", None) and _tag_local(parent) in names:
                    return True
                parent = getattr(parent, "parent", None)
            return False

        def first_attr(node, attr_name: str) -> str | None:
            if node.has_attr(attr_name):
                return str(node[attr_name])
            suffix = f":{attr_name}"
            for key, value in node.attrs.items():
                if str(key).endswith(suffix):
                    return str(value)
            return None

        def first_descendant_attr(node, attr_name: str) -> str | None:
            for descendant in node.find_all(True):
                value = first_attr(descendant, attr_name)
                if value:
                    return value
            return None

        def direct_child_attr(node, child_name: str, attr_name: str) -> str | None:
            for child in getattr(node, "children", []):
                if getattr(child, "name", None) and _tag_local(child) == child_name:
                    value = first_attr(child, attr_name)
                    if value:
                        return value
            return None

        def direct_children(node, name: str) -> list:
            return [
                child
                for child in getattr(node, "children", [])
                if getattr(child, "name", None) and _tag_local(child) == name
            ]

        def node_text(node) -> str:
            parts = []
            for text_node in node.find_all(True):
                if _tag_local(text_node) == "t":
                    parts.append(text_node.get_text())
            return _sanitize_hwpx_text("".join(parts)).strip()

        def node_text_without_nested_tables(node) -> str:
            parts = []
            for text_node in node.find_all(True):
                if _tag_local(text_node) == "t" and not has_ancestor_between(text_node, node, {"tbl"}):
                    parts.append(text_node.get_text())
            return _sanitize_hwpx_text("".join(parts)).strip()

        def render_cell_content(cell_node) -> str:
            containers = direct_children(cell_node, "subList") or [cell_node]
            parts = []
            for container in containers:
                for child in getattr(container, "children", []):
                    if not getattr(child, "name", None):
                        continue
                    tag = _tag_local(child)
                    if tag == "p":
                        text = node_text_without_nested_tables(child)
                        if text:
                            escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                            parts.append(f"<p>{escaped}</p>")
                        for nested_table_node in child.find_all(lambda tag: _tag_local(tag) == "tbl"):
                            if has_ancestor_between(nested_table_node, child, {"tbl"}):
                                continue
                            nested = render_table(nested_table_node)
                            if nested:
                                parts.append(nested["html"])
                    elif tag == "tbl":
                        nested = render_table(child)
                        if nested:
                            parts.append(nested["html"])
            if parts:
                return "".join(parts)
            escaped = node_text_without_nested_tables(cell_node).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return escaped

        def render_table(table_node) -> dict | None:
            rows = []
            flat_lines = []
            table_rows = [
                tr
                for tr in table_node.find_all(lambda tag: _tag_local(tag) == "tr")
                if not has_ancestor_between(tr, table_node, {"tbl"})
            ]
            for tr in table_rows:
                cells = []
                for tc in direct_children(tr, "tc"):
                    cell_text = node_text_without_nested_tables(tc)
                    flat_lines.append(cell_text)

                    col_span = first_attr(tc, "colSpan") or direct_child_attr(tc, "cellSpan", "colSpan") or "1"
                    row_span = first_attr(tc, "rowSpan") or direct_child_attr(tc, "cellSpan", "rowSpan") or "1"
                    attrs = []
                    if col_span.isdigit() and int(col_span) > 1:
                        attrs.append(f'colspan="{col_span}"')
                    if row_span.isdigit() and int(row_span) > 1:
                        attrs.append(f'rowspan="{row_span}"')
                    attrs_text = f" {' '.join(attrs)}" if attrs else ""
                    tag = "th" if first_attr(tc, "header") == "1" else "td"
                    cells.append((tag, attrs_text, render_cell_content(tc), cell_text))
                if cells:
                    rows.append(cells)

            if not rows:
                return None

            html_parts = ["<table>"]
            for row in rows:
                cell_html = []
                for tag, attrs_text, content_html, _text in row:
                    cell_html.append(f"<{tag}{attrs_text}>{content_html}</{tag}>")
                html_parts.append("  <tr>\n    " + "\n    ".join(cell_html) + "\n  </tr>")
            html_parts.append("</table>")

            markdown_lines = []
            for row_index, row in enumerate(rows):
                row_texts = [text.replace("|", "\\|") for _, _, _, text in row]
                markdown_lines.append("| " + " | ".join(row_texts) + " |")
                if row_index == 0:
                    markdown_lines.append("| " + " | ".join("---" for _ in row_texts) + " |")

            flat_text = "\n".join(line for line in flat_lines if line).strip()
            return {
                "html": "\n".join(html_parts),
                "markdown": "\n".join(markdown_lines),
                "flat_text": flat_text,
                "first_line": next((line for line in flat_lines if line), ""),
            }

        def image_element(pic_node, image_index: int, names: list[str], bin_refs: dict[str, str], zf: zipfile.ZipFile) -> dict | None:
            img = pic_node.find(lambda item: _tag_local(item) == "img")
            if img is None:
                return None
            ref = first_attr(img, "binaryItemIDRef")
            image_path = bin_refs.get(ref or "")
            if not image_path or image_path not in names:
                return None

            image_bytes = zf.read(image_path)
            image_id = f"hwpx-img-{image_index}"
            return {
                "type": "image",
                "image_id": image_id,
                "page": 1,
                "bbox": [0, 0, 0, 0],
                "bytes": image_bytes,
                "name": Path(image_path).name,
                "sha1": hashlib.sha1(image_bytes, usedforsecurity=False).hexdigest(),
                "is_table": False,
            }

        def append_ordered_paragraph_elements(paragraph_node, names: list[str], bin_refs: dict[str, str], zf: zipfile.ZipFile) -> None:
            text_parts = []

            def flush_text() -> None:
                text = _sanitize_hwpx_text("".join(text_parts)).strip()
                text_parts.clear()
                if not text:
                    return
                plain_text_lines.append(text)
                elements.append({
                    "type": "text",
                    "content": text,
                    "bbox": [0, 0, 0, 0],
                })

            nonlocal image_count
            for child in paragraph_node.descendants:
                if isinstance(child, NavigableString) or not getattr(child, "name", None):
                    continue
                tag = _tag_local(child)
                if tag == "tbl" or has_ancestor(child, {"tbl"}):
                    continue
                if tag == "t":
                    text_parts.append(child.get_text())
                elif tag == "lineBreak":
                    text_parts.append("\n")
                elif tag == "pic":
                    flush_text()
                    image_count += 1
                    image = image_element(child, image_count, names, bin_refs, zf)
                    if image:
                        images.append(image)
                        elements.append({
                            "type": "image",
                            "image_id": image["image_id"],
                            "bbox": image["bbox"],
                        })
            flush_text()

        elements = []
        plain_text_lines = []
        tables = []
        images = []
        image_count = 0

        with zipfile.ZipFile(hwp_path, "r") as zf:
            names = zf.namelist()
            bin_refs = _hwpx_bin_ref_map(zf)
            section_names = sorted(
                (
                    name
                    for name in names
                    if name.startswith("Contents/section") and name.endswith(".xml")
                ),
                key=section_sort_key,
            )
            if not section_names:
                raise ConversionError("HWPX section XML을 찾을 수 없습니다.")

            for section_name in section_names:
                soup = BeautifulSoup(zf.read(section_name), "xml")
                root = soup.find()
                if root is None:
                    continue

                for node in root.descendants:
                    if isinstance(node, NavigableString) or not getattr(node, "name", None):
                        continue

                    tag = _tag_local(node)
                    if tag == "tbl":
                        if has_ancestor(node, {"tbl"}):
                            continue
                        table = render_table(node)
                        if table:
                            tables.append(table)
                            elements.append({
                                "type": "table",
                                "content": table["html"],
                                "markdown": table["markdown"],
                                "bbox": [0, 0, 0, 0],
                            })
                    elif tag == "p":
                        if has_ancestor(node, {"p", "tbl"}):
                            continue
                        append_ordered_paragraph_elements(node, names, bin_refs, zf)
                    elif tag == "pic":
                        if has_ancestor(node, {"p", "tbl"}):
                            continue
                        image_count += 1
                        image = image_element(node, image_count, names, bin_refs, zf)
                        if image:
                            images.append(image)
                            elements.append({
                                "type": "image",
                                "image_id": image["image_id"],
                                "bbox": image["bbox"],
                            })

        print(f"[DEBUG] HWPX XML 직접 파싱: 요소 {len(elements)}개, 표 {len(tables)}개, 이미지 {len(images)}개")
        return {
            "page_count": 1,
            "pages": [
                {
                    "page": 1,
                    "text": "\n".join(plain_text_lines),
                    "images": images,
                    "elements": elements,
                }
            ],
            "tables": tables,
        }
    except ConversionError:
        raise
    except Exception as e:
        raise ConversionError(f"HWPX XML 직접 파싱 실패: {e}") from e


def _normalize_ws(s: str) -> str:
    """공백/줄바꿈 정규화 (표 매칭용)"""
    if not s:
        return ""
    return " ".join(s.split())


def merge_hwpx_tables_into_elements(
    elements: List[dict],
    hwpx_tables: List[dict],
    table_index_ref: List[int],
) -> List[dict]:
    """
    페이지 요소 목록에서 HWPX XML 표와 매칭되는 텍스트 블록을 [[TABLE]]용 요소로 치환.
    - elements: [{type:"text", content:"..."}, ...]
    - hwpx_tables: parse_hwpx_tables() 반환값
    - table_index_ref: [현재 사용할 표 인덱스] (함수 내부에서 증가)
    """
    if not hwpx_tables or table_index_ref[0] >= len(hwpx_tables):
        return elements
    result = []
    i = 0
    while i < len(elements):
        elem = elements[i]
        if elem.get("type") != "text":
            result.append(elem)
            i += 1
            continue
        idx = table_index_ref[0]
        if idx >= len(hwpx_tables):
            result.append(elem)
            i += 1
            continue
        # i부터 이어지는 텍스트만 모아서 이 페이지 구간
        acc_from_i = []
        for k in range(i, len(elements)):
            if elements[k].get("type") != "text":
                break
            acc_from_i.append(elements[k].get("content", ""))
        page_segment = _normalize_ws("\n".join(acc_from_i))
        if not page_segment.strip():
            result.append(elem)
            i += 1
            continue
        # 현재 표(idx)부터 순서대로 매칭 시도. 앞쪽 표가 목차 등으로 PDF에서 다른 형태면 건너뛰고 뒤 표 매칭
        matched_tbl = None
        matched_idx = None
        for try_idx in range(idx, min(idx + 15, len(hwpx_tables))):
            tbl = hwpx_tables[try_idx]
            first_line = _normalize_ws(tbl.get("first_line", ""))
            flat_text = _normalize_ws(tbl.get("flat_text", ""))
            if not first_line and not flat_text:
                continue
            if first_line and first_line in page_segment:
                matched_tbl = tbl
                matched_idx = try_idx
                break
            if flat_text and len(flat_text) > 20 and (flat_text[:80] in page_segment or flat_text[:60] in page_segment):
                matched_tbl = tbl
                matched_idx = try_idx
                break
        if not matched_tbl:
            result.append(elem)
            i += 1
            continue
        tbl = matched_tbl
        table_index_ref[0] = matched_idx + 1
        first_line = _normalize_ws(tbl.get("first_line", ""))
        flat_text = _normalize_ws(tbl.get("flat_text", ""))
        # 표에 해당하는 텍스트 블록 구간 찾기: first_line 또는 flat_text 앞부분 포함하고 표 길이의 25% 이상
        acc_from_i = []
        end_idx = i
        for k in range(i, len(elements)):
            if elements[k].get("type") != "text":
                break
            acc_from_i.append(elements[k].get("content", ""))
            segment = _normalize_ws("\n".join(acc_from_i))
            end_idx = k
            if (first_line and first_line in segment) or (flat_text and flat_text[:60] in segment):
                if not flat_text or len(segment) >= max(50, len(flat_text) * 0.25):
                    break
        # i ~ end_idx 를 하나의 표 요소로 치환 (bbox는 첫 텍스트 블록 것 사용해 정렬 유지)
        result.append({
            "type": "table",
            "content": tbl.get("html", ""),
            "markdown": tbl.get("markdown", ""),
            "bbox": elements[i].get("bbox", [0, 0, 0, 0]),
        })
        i = end_idx + 1
    return result


def analyze_html_tables(html_path: Path) -> dict:
    """
    HTML에서 표를 분석하여 간단한 표와 복잡한 표를 구분
    반환: {"simple_tables": [(index, text)], "complex_tables": [(index, text, row_count)]}
    """
    try:
        from bs4 import BeautifulSoup
        
        with open(html_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'xml')
        
        tables = soup.find_all('table')
        simple_tables = []
        complex_tables = []
        
        for idx, table in enumerate(tables):
            rows = table.find_all('tr')
            row_count = len(rows)
            text = table.get_text(strip=True, separator=' ')
            
            # 간단한 표 기준:
            # 1. 1행만 있는 경우 (제목)
            # 2. 2행 이하이고 셀이 1-3개 이하이고 텍스트가 짧은 경우 (< 100자)
            # 3. 3행 이하이고 제목 표처럼 보이는 경우 (텍스트가 짧고 구조가 단순)
            if row_count <= 1:
                simple_tables.append((idx, text))
                print(f"[DEBUG] 간단한 표 {idx}: {text[:50]}...")
            elif row_count <= 2:
                total_cells = sum(len(row.find_all(['td', 'th'])) for row in rows)
                if total_cells <= 3 and len(text) < 100:
                    simple_tables.append((idx, text))
                    print(f"[DEBUG] 간단한 표 {idx}: {text[:50]}...")
                else:
                    complex_tables.append((idx, text, row_count))
                    print(f"[DEBUG] 복잡한 표 {idx}: {row_count}행, {text[:50]}...")
            elif row_count <= 3:
                # 3행 이하이고 텍스트가 짧으면 간단한 표로 간주 (제목 표 등)
                total_cells = sum(len(row.find_all(['td', 'th'])) for row in rows)
                if len(text) < 150 and total_cells <= 6:
                    simple_tables.append((idx, text))
                    print(f"[DEBUG] 간단한 표 {idx} (3행 이하): {text[:50]}...")
                else:
                    complex_tables.append((idx, text, row_count))
                    print(f"[DEBUG] 복잡한 표 {idx}: {row_count}행, {text[:50]}...")
            else:
                # 4행 이상은 복잡한 표
                complex_tables.append((idx, text, row_count))
                print(f"[DEBUG] 복잡한 표 {idx}: {row_count}행, {text[:50]}...")
        
        print(f"[DEBUG] HTML 표 분석: 간단 {len(simple_tables)}개, 복잡 {len(complex_tables)}개")
        return {
            "simple_tables": simple_tables,
            "complex_tables": complex_tables  # (index, text, row_count) 리스트
        }
    except Exception as e:
        print(f"[WARNING] HTML 표 분석 실패: {e}")
        return {"simple_tables": [], "complex_tables": []}


def _cluster_positions(values: List[float], tolerance: float = 3.0) -> List[float]:
    """Close PDF coordinates often differ by sub-point noise; collapse them."""
    if not values:
        return []
    clusters: List[List[float]] = []
    for value in sorted(values):
        if not clusters or abs(value - (sum(clusters[-1]) / len(clusters[-1]))) > tolerance:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _rects_overlap_or_touch(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
    tolerance: float = 4.0,
) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (
        ax1 + tolerance < bx0
        or bx1 + tolerance < ax0
        or ay1 + tolerance < by0
        or by1 + tolerance < ay0
    )


def _text_intersects_bbox(
    text_blocks: List[Dict[str, Any]],
    bbox: Tuple[float, float, float, float],
    tolerance: float = 4.0,
) -> bool:
    x0, y0, x1, y1 = bbox
    for block in text_blocks:
        bx0, by0, bx1, by1 = block["bbox"]
        if not (bx1 < x0 - tolerance or bx0 > x1 + tolerance or by1 < y0 - tolerance or by0 > y1 + tolerance):
            return True
    return False


def _detect_grid_table_regions_from_drawings(
    page,
    text_blocks: List[Dict[str, Any]],
) -> List[Tuple[float, float, float, float]]:
    """Detect visually ruled table grids, including mostly-empty tables.

    Text-only detection misses empty rows because they contain no spans. PDF
    drawing objects still preserve the table ruling lines, so this fallback
    extracts horizontal/vertical line grids from vector paths and rectangles.
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return []

    segments: List[Dict[str, Any]] = []
    min_len = 8.0

    def add_segment(x0: float, y0: float, x1: float, y1: float) -> None:
        if abs(y1 - y0) <= 1.5 and abs(x1 - x0) >= min_len:
            sx0, sx1 = sorted((float(x0), float(x1)))
            y = (float(y0) + float(y1)) / 2
            segments.append({"orientation": "h", "bbox": (sx0, y, sx1, y), "pos": y})
        elif abs(x1 - x0) <= 1.5 and abs(y1 - y0) >= min_len:
            sy0, sy1 = sorted((float(y0), float(y1)))
            x = (float(x0) + float(x1)) / 2
            segments.append({"orientation": "v", "bbox": (x, sy0, x, sy1), "pos": x})

    for drawing in drawings:
        for item in drawing.get("items", []):
            kind = item[0] if item else None
            if kind == "l" and len(item) >= 3:
                p0, p1 = item[1], item[2]
                add_segment(p0.x, p0.y, p1.x, p1.y)
            elif kind == "re" and len(item) >= 2:
                rect = item[1]
                add_segment(rect.x0, rect.y0, rect.x1, rect.y0)
                add_segment(rect.x0, rect.y1, rect.x1, rect.y1)
                add_segment(rect.x0, rect.y0, rect.x0, rect.y1)
                add_segment(rect.x1, rect.y0, rect.x1, rect.y1)

    if len(segments) < 4:
        return []

    # Build connected components of touching/intersecting ruling segments.
    parent = list(range(len(segments)))

    def find(idx: int) -> int:
        while parent[idx] != idx:
            parent[idx] = parent[parent[idx]]
            idx = parent[idx]
        return idx

    def union(a: int, b: int) -> None:
        root_a = find(a)
        root_b = find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for i, seg_i in enumerate(segments):
        for j in range(i + 1, len(segments)):
            if _rects_overlap_or_touch(seg_i["bbox"], segments[j]["bbox"]):
                union(i, j)

    components: Dict[int, List[Dict[str, Any]]] = {}
    for idx, segment in enumerate(segments):
        components.setdefault(find(idx), []).append(segment)

    regions: List[Tuple[float, float, float, float]] = []
    page_rect = page.rect
    for component in components.values():
        horizontal = [s for s in component if s["orientation"] == "h"]
        vertical = [s for s in component if s["orientation"] == "v"]
        x_lines = _cluster_positions([s["pos"] for s in vertical])
        y_lines = _cluster_positions([s["pos"] for s in horizontal])

        # Accept either a normal multi-column grid or a single-column ruled
        # container table with at least two row bands. This catches outer form
        # tables that wrap the whole document body.
        is_multi_column_grid = len(x_lines) >= 3 and len(y_lines) >= 2
        is_single_column_container = len(x_lines) >= 2 and len(y_lines) >= 3
        if not (is_multi_column_grid or is_single_column_container):
            continue

        x0 = min(min(s["bbox"][0], s["bbox"][2]) for s in component)
        y0 = min(min(s["bbox"][1], s["bbox"][3]) for s in component)
        x1 = max(max(s["bbox"][0], s["bbox"][2]) for s in component)
        y1 = max(max(s["bbox"][1], s["bbox"][3]) for s in component)
        width = x1 - x0
        height = y1 - y0
        area = width * height
        if width < 80 or height < 18 or area < 1500:
            continue

        # Avoid promoting decorative boxes with no text nearby.
        if not _text_intersects_bbox(text_blocks, (x0, y0, x1, y1), tolerance=8.0):
            continue

        margin = 4.0
        region = (
            max(0.0, x0 - margin),
            max(0.0, y0 - margin),
            min(float(page_rect.width), x1 + margin),
            min(float(page_rect.height), y1 + margin),
        )
        regions.append(region)

    return regions


def _merge_overlapping_regions(
    regions: List[Tuple[float, float, float, float]],
    tolerance: float = 8.0,
) -> List[Tuple[float, float, float, float]]:
    if not regions:
        return []
    merged: List[Tuple[float, float, float, float]] = []
    for region in sorted(regions, key=lambda r: (r[1], r[0])):
        rx0, ry0, rx1, ry1 = region
        did_merge = False
        for idx, existing in enumerate(merged):
            if _rects_overlap_or_touch(existing, region, tolerance=tolerance):
                ex0, ey0, ex1, ey1 = existing
                merged[idx] = (min(ex0, rx0), min(ey0, ry0), max(ex1, rx1), max(ey1, ry1))
                did_merge = True
                break
        if not did_merge:
            merged.append(region)
    return merged


def extract_table_regions_from_pdf_by_text(pdf_path: Path, page_num: int, table_count: int = 0) -> List[Tuple[float, float, float, float]]:
    """
    PDF 페이지에서 표 영역의 bbox 추출
    텍스트 블록을 분석하여 표처럼 보이는 영역을 찾음
    반환: [(x0, y0, x1, y1), ...] 리스트
    """
    try:
        doc = fitz.open(pdf_path)
        if page_num > len(doc):
            doc.close()
            return []
        
        page = doc.load_page(page_num - 1)
        text_dict = page.get_text("dict")
        page_rect = page.rect
        
        # 텍스트 블록들을 y 좌표로 그룹화 (행 단위)
        blocks = [b for b in text_dict["blocks"] if b.get("type") == 0]
        if not blocks:
            doc.close()
            return []
        
        # 각 블록의 텍스트와 bbox 추출
        text_blocks = []
        for block in blocks:
            text_content = ""
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text_content += span.get("text", "") + " "
            text_content = text_content.strip()
            if text_content:
                bbox = block.get("bbox", [0, 0, 0, 0])
                text_blocks.append({
                    "text": text_content,
                    "bbox": bbox,
                    "x0": bbox[0],
                    "y0": bbox[1],
                    "x1": bbox[2],
                    "y1": bbox[3],
                })
        
        if not text_blocks:
            doc.close()
            return []
        
        # y 좌표로 정렬하여 행 단위로 그룹화
        text_blocks.sort(key=lambda b: (b["y0"], b["x0"]))
        
        # 행 그룹화 (y 좌표가 비슷한 것들을 같은 행으로)
        # 더 유연한 그룹화: 블록의 높이를 고려하여 동적 임계값 사용
        rows = []
        current_row = []
        current_y = None
        
        for block in text_blocks:
            y_center = (block["y0"] + block["y1"]) / 2
            block_height = block["y1"] - block["y0"]
            # 블록 높이의 1.5배 이내면 같은 행으로 간주 (더 유연함)
            threshold = max(10, block_height * 1.5)
            
            if current_y is None or abs(y_center - current_y) < threshold:
                current_row.append(block)
                if current_y is None:
                    current_y = y_center
                else:
                    # 평균 y 좌표 업데이트
                    current_y = (current_y + y_center) / 2
            else:
                if len(current_row) > 0:
                    rows.append(current_row)
                current_row = [block]
                current_y = y_center
        
        if len(current_row) > 0:
            rows.append(current_row)
        
        print(f"[DEBUG] Page {page_num}: {len(rows)}개 행으로 그룹화")
        
        # 표 영역 찾기: 여러 열(블록)이 있는 행들이 연속으로 나타나는 경우
        table_regions = []
        i = 0
        
        while i < len(rows):
            # 현재 행의 열 개수
            current_cols = len(rows[i])
            
            # 표로 간주할 조건: 2개 이상의 열이 있고, 연속된 행들이 비슷한 열 구조를 가짐
            if current_cols >= 2:
                table_start_idx = i
                table_end_idx = i
                
                # 표의 특징: 여러 열이 있고, x 좌표가 정렬되어 있음
                # 첫 행의 x 좌표들을 기준으로 정렬 확인
                first_row_x_positions = sorted([b["x0"] for b in rows[i]])
                
                # 연속된 행들 확인 (더 유연한 표 연속성 판단)
                for j in range(i + 1, min(i + 50, len(rows))):  # 최대 50행까지 확인 (더 긴 표 지원)
                    next_cols = len(rows[j])
                    
                    # 열 개수가 비슷하거나 (표의 구조 유지)
                    # 또는 1개 열이지만 긴 텍스트인 경우 (표의 셀 병합 등)
                    if next_cols >= 2:
                        # 다음 행의 x 좌표들도 확인
                        next_row_x_positions = sorted([b["x0"] for b in rows[j]])
                        # x 좌표가 비슷하면 같은 표로 간주
                        if len(first_row_x_positions) == len(next_row_x_positions):
                            # x 좌표 차이가 작으면 같은 표
                            max_x_diff = max(abs(first_row_x_positions[k] - next_row_x_positions[k]) 
                                           for k in range(min(len(first_row_x_positions), len(next_row_x_positions))))
                            if max_x_diff < 30:  # 30pt 이내면 같은 표 (더 유연하게)
                                table_end_idx = j
                            else:
                                # x 좌표가 다르지만 여전히 표일 수 있음 (열 구조 변화)
                                # 같은 x 범위 내에 있으면 같은 표로 간주
                                x_range_overlap = not (max(first_row_x_positions) < min(next_row_x_positions) or 
                                                      max(next_row_x_positions) < min(first_row_x_positions))
                                if x_range_overlap:
                                    table_end_idx = j
                                else:
                                    break
                        else:
                            # 열 개수가 다르지만 여전히 표일 수 있음 (셀 병합, rowspan 등)
                            # x 좌표 범위가 겹치면 같은 표로 간주
                            x_range_overlap = not (max(first_row_x_positions) < min(next_row_x_positions) or 
                                                  max(next_row_x_positions) < min(first_row_x_positions))
                            if x_range_overlap:
                                table_end_idx = j
                            else:
                                # x 범위가 다르면 표 종료
                                break
                    elif next_cols == 1:
                        # 1개 열이지만 긴 텍스트인 경우 (표의 셀 병합 등)
                        block_text = rows[j][0]["text"].strip()
                        block_x0 = rows[j][0]["x0"]
                        block_x1 = rows[j][0]["x1"]
                        
                        # x 좌표가 표 범위 내에 있고, 텍스트가 충분히 길면 표의 일부로 간주
                        table_x0 = min(b["x0"] for b in rows[table_start_idx])
                        table_x1 = max(b["x1"] for b in rows[table_start_idx])
                        
                        x_within_table = (block_x0 >= table_x0 - 20 and block_x1 <= table_x1 + 20)
                        
                        if len(block_text) > 5 and x_within_table:  # 더 짧은 텍스트도 허용
                            table_end_idx = j
                        elif len(block_text) == 0 or (len(block_text) < 3 and not x_within_table):
                            # 빈 줄이거나 표 범위 밖의 짧은 텍스트면 표 종료
                            break
                        else:
                            # 짧지만 표 범위 내에 있으면 계속
                            table_end_idx = j
                    else:
                        # 빈 줄이 나오면 표 종료
                        break
                
                # 최소 2행 이상이면 표로 간주 (조건 완화)
                if table_end_idx - table_start_idx >= 1:
                    # look-back: 표 시작 전 1열짜리 스패닝 헤더 행 포함
                    # (예: "제·개정 이력" 같은 전체 폭 제목 행)
                    if table_start_idx > 0:
                        first_multi_x0 = min(b["x0"] for b in rows[table_start_idx])
                        first_multi_x1 = max(b["x1"] for b in rows[table_start_idx])
                        table_width = first_multi_x1 - first_multi_x0

                        lookback_idx = table_start_idx - 1
                        while lookback_idx >= 0 and (table_start_idx - lookback_idx) <= 2:
                            prev_row = rows[lookback_idx]
                            if len(prev_row) != 1:
                                break
                            block = prev_row[0]
                            # x 범위 겹침 확인: 표 폭의 50% 이상 겹쳐야 함
                            x_overlap = max(0, min(block["x1"], first_multi_x1) - max(block["x0"], first_multi_x0))
                            if x_overlap < table_width * 0.5:
                                break
                            # y 간격 확인: 다음 행과 30pt 이내여야 함
                            next_y0 = min(b["y0"] for b in rows[lookback_idx + 1])
                            if next_y0 - block["y1"] >= 30:
                                break
                            table_start_idx = lookback_idx
                            lookback_idx -= 1

                    # 표 영역의 bbox 계산
                    all_blocks = []
                    for row_idx in range(table_start_idx, table_end_idx + 1):
                        all_blocks.extend(rows[row_idx])
                    
                    x0 = min(b["x0"] for b in all_blocks) - 10
                    y0 = min(b["y0"] for b in all_blocks) - 10
                    x1 = max(b["x1"] for b in all_blocks) + 10
                    y1 = max(b["y1"] for b in all_blocks) + 10
                    
                    # 페이지 범위 내로 제한
                    x0 = max(0, min(x0, page_rect.width))
                    y0 = max(0, min(y0, page_rect.height))
                    x1 = max(x0 + 20, min(x1, page_rect.width))  # 최소 너비 보장
                    y1 = max(y0 + 20, min(y1, page_rect.height))  # 최소 높이 보장
                    
                    # 표 영역이 너무 작으면 제외 (노이즈 제거)
                    area = (x1 - x0) * (y1 - y0)
                    if area > 1000:  # 최소 면적 1000pt²
                        table_regions.append((x0, y0, x1, y1))
                        print(f"[DEBUG] 표 영역 {len(table_regions)}: ({x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}), {table_end_idx - table_start_idx + 1}행")
                    
                    i = table_end_idx + 1
                    continue
            
            i += 1

        drawing_table_regions = _detect_grid_table_regions_from_drawings(page, text_blocks)
        if drawing_table_regions:
            before_count = len(table_regions)
            table_regions = _merge_overlapping_regions(table_regions + drawing_table_regions)
            added_count = max(0, len(table_regions) - before_count)
            print(
                f"[DEBUG] Page {page_num}: drawing grid 표 후보 {len(drawing_table_regions)}개 감지 "
                f"(병합 후 +{added_count}개)"
            )

        doc.close()
        
        # 인접한 표 영역 병합: 같은 페이지에서 수직으로 가까운 표들을 하나로 병합
        if len(table_regions) > 1:
            merged_regions = []
            # y 좌표로 정렬
            sorted_regions = sorted(table_regions, key=lambda r: r[1])  # y0 기준 정렬
            
            i = 0
            while i < len(sorted_regions):
                current = sorted_regions[i]
                x0_curr, y0_curr, x1_curr, y1_curr = current
                
                # 현재 표와 병합할 후보들 찾기
                merged = [current]
                j = i + 1
                
                while j < len(sorted_regions):
                    next_region = sorted_regions[j]
                    x0_next, y0_next, x1_next, y1_next = next_region
                    
                    # 병합 조건:
                    # 1. x 좌표가 겹치거나 비슷함 (같은 열 구조)
                    # 2. y 좌표가 가까움 (표 사이 간격이 작음)
                    x_overlap = not (x1_curr < x0_next or x1_next < x0_curr)
                    x_similar = abs((x0_curr + x1_curr) / 2 - (x0_next + x1_next) / 2) < max(x1_curr - x0_curr, x1_next - x0_next) * 0.3
                    y_gap = y0_next - y1_curr  # 두 표 사이의 간격
                    max_height = max(y1_curr - y0_curr, y1_next - y0_next)
                    
                    # 간격이 표 높이의 2배 이내이고, x 좌표가 비슷하면 같은 표로 간주
                    if (x_overlap or x_similar) and y_gap >= 0 and y_gap < max_height * 2.5:
                        merged.append(next_region)
                        # 병합된 영역의 bbox 업데이트
                        x0_curr = min(x0_curr, x0_next)
                        y0_curr = min(y0_curr, y0_next)
                        x1_curr = max(x1_curr, x1_next)
                        y1_curr = max(y1_curr, y1_next)
                        current = (x0_curr, y0_curr, x1_curr, y1_curr)
                        j += 1
                    else:
                        break
                
                # 병합된 영역 추가
                merged_regions.append(current)
                print(f"[DEBUG] 표 영역 병합: {len(merged)}개 영역을 1개로 병합 → ({current[0]:.1f}, {current[1]:.1f}, {current[2]:.1f}, {current[3]:.1f})")
                i = j
            
            table_regions = merged_regions
            print(f"[DEBUG] 표 영역 병합 완료: {len(table_regions)}개 표 영역 (병합 전: {len(sorted_regions)}개)")
        
        # 요청한 개수가 0이면 모든 표 반환, 아니면 요청한 개수만큼만 반환
        if table_count == 0:
            result = table_regions
        else:
            result = table_regions[:table_count]
        print(f"[DEBUG] 총 {len(result)}개 표 영역 추출 (요청: {table_count}개, 최종: {len(table_regions)}개)")
        return result
        
    except Exception as e:
        print(f"[WARNING] PDF 표 영역 추출 실패: {e}")
        import traceback
        traceback.print_exc()
        return []


def convert_hwp_to_pdf_via_libreoffice(hwp_path: Path, output_dir: Path) -> Optional[Path]:
    """
    HWP/HWPX 파일을 LibreOffice로 PDF 변환
    HWPX(ZIP) 형식도 LibreOffice가 지원함
    """
    _lo = _find_libreoffice()
    if not _lo:
        return None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            _lo,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(hwp_path),
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        pdf_path = output_dir / f"{hwp_path.stem}.pdf"
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            # PDF 페이지 수 검증 (비정상적으로 많은 페이지 = OLE 덤프)
            import fitz
            try:
                doc = fitz.open(pdf_path)
                lo_page_count = len(doc)
                doc.close()
                print(f"[DEBUG] LibreOffice HWP → PDF 변환 성공: {pdf_path.name} ({lo_page_count}페이지)")
                
                # 500페이지 초과 시 비정상으로 판단 (OLE/바이너리 덤프 가능성)
                if lo_page_count > 500:
                    print(f"[WARNING] LibreOffice PDF 페이지 수가 비정상적으로 많음({lo_page_count}페이지). 텍스트 기반 PDF로 폴백합니다.")
                    if pdf_path.exists():
                        pdf_path.unlink()
                    return None
                
                return (pdf_path, {"simple_tables": [], "complex_tables": []})
            except Exception as e:
                print(f"[WARNING] LibreOffice PDF 페이지 수 확인 실패: {e}")
                return (pdf_path, {"simple_tables": [], "complex_tables": []})
        return None
    except Exception as e:
        print(f"[WARNING] LibreOffice HWP 변환 실패: {e}")
        return None


def convert_hwp_to_pdf_via_legacy_converter(hwp_path: Path, output_dir: Path) -> Optional[Path]:
    """
    Optional HWP v4-or-older conversion hook.

    Configure HWP_LEGACY_CONVERTER_CMD with placeholders:
    - {input}: source HWP path
    - {output}: expected output PDF path
    - {output_dir}: output directory

    Example:
      HWP_LEGACY_CONVERTER_CMD='legacy-hwp-convert --input {input} --output {output}'
    """
    from core.env import env_str

    template = env_str("HWP_LEGACY_CONVERTER_CMD", "", strip=True)
    if not template:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"{hwp_path.stem}.pdf"
    try:
        cmd_text = template.format(
            input=str(hwp_path),
            output=str(pdf_path),
            output_dir=str(output_dir),
        )
        cmd = shlex.split(cmd_text)
        if not cmd:
            return None
        print(f"[INFO] HWP legacy converter 실행: {cmd[0]}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else result.stdout.strip()
            print(f"[WARNING] HWP legacy converter 실패: {stderr}")
            return None
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return (pdf_path, {"simple_tables": [], "complex_tables": []})
        converted = output_dir / f"{hwp_path.stem}.PDF"
        if converted.exists() and converted.stat().st_size > 0:
            converted.rename(pdf_path)
            return (pdf_path, {"simple_tables": [], "complex_tables": []})
        print("[WARNING] HWP legacy converter가 PDF를 생성하지 않았습니다.")
        return None
    except Exception as e:
        print(f"[WARNING] HWP legacy converter 실행 실패: {e}")
        return None


def convert_hwp_to_pdf_via_html(hwp_path: Path, output_dir: Path) -> Optional[Path]:
    """
    HWP → HTML → PDF 변환 (hwp5html + wkhtmltopdf)
    표와 이미지가 포함된 PDF 생성
    """
    try:
        import subprocess
        import shutil
        
        # 1. HWP → HTML (디렉토리 생성됨)
        html_dir = output_dir / f"{hwp_path.stem}_html"
        print(f"[DEBUG] HWP → HTML 변환 시작: {hwp_path.name}")
        
        result = subprocess.run(
            [_find_tool("hwp5html"), str(hwp_path), "--output", str(html_dir)],
            capture_output=True,
            text=True,
            timeout=600  # 대용량 HWP 파일을 위해 10분으로 증가
        )
        
        # hwp5html은 디렉토리 안에 index.xhtml을 생성
        index_html = html_dir / "index.xhtml"
        if result.returncode != 0 or not index_html.exists():
            stderr = result.stderr.strip() if result.stderr else result.stdout.strip()
            if _is_pyhwp_unsupported_error(stderr):
                print(
                    "[WARNING] hwp5html이 이 HWP 내부 OLE/SummaryInfo 구조를 지원하지 않아 "
                    "LibreOffice 변환으로 폴백합니다."
                )
            else:
                print(f"[WARNING] hwp5html 실패: {stderr}")
            if html_dir.exists():
                shutil.rmtree(html_dir, ignore_errors=True)
            return None
            
        print(f"[DEBUG] HTML 생성 완료: {index_html}")
        
        # 1.5. HTML 표 분석 (간단한 표 vs 복잡한 표)
        table_info = analyze_html_tables(index_html)
        
        # 1.5.1. HTML에 <표> 텍스트 플레이스홀더가 많은데 실제 <table>이 거의 없으면
        #        HTML→PDF 결과가 표를 표현하지 못한 것으로 간주하고 LibreOffice 변환을 우선 시도
        try:
            from bs4 import BeautifulSoup
            html_raw = index_html.read_text(encoding='utf-8')
            soup = BeautifulSoup(html_raw, 'xml')
            table_count = len(soup.find_all('table'))
            text_content = soup.get_text(separator='\n', strip=True)
            placeholder_count = text_content.count("<표>")
            if placeholder_count > 0 and table_count == 0:
                print("[WARNING] HTML에 '<표>' 플레이스홀더가 있으나 <table>이 없습니다. LibreOffice 변환으로 폴백합니다.")
                lo_result = convert_hwp_to_pdf_via_libreoffice(hwp_path, output_dir)
                if lo_result:
                    return lo_result
        except Exception as e:
            print(f"[WARNING] HTML 플레이스홀더 검사 실패: {e}")
        
        # 1.6. HTML에 CSS 추가하여 표가 페이지 경계를 넘지 않도록 설정
        try:
            html_content = index_html.read_text(encoding='utf-8')
            # <head> 태그가 있으면 그 안에, 없으면 <html> 태그 다음에 CSS 추가
            css_style = """
            <style>
                table {
                    page-break-inside: avoid !important;
                    break-inside: avoid !important;
                }
                tr {
                    page-break-inside: avoid !important;
                    break-inside: avoid !important;
                }
            </style>
            """
            
            if '<head>' in html_content:
                html_content = html_content.replace('<head>', f'<head>{css_style}')
            elif '<html' in html_content:
                # <html> 태그 다음에 <head> 추가
                import re
                html_content = re.sub(r'(<html[^>]*>)', r'\1<head>' + css_style + '</head>', html_content, count=1)
            else:
                # HTML 구조가 없으면 맨 앞에 추가
                html_content = css_style + html_content
            
            index_html.write_text(html_content, encoding='utf-8')
            print("[DEBUG] HTML에 표 페이지 분할 방지 CSS 추가 완료")
        except Exception as e:
            print(f"[WARNING] CSS 추가 실패 (계속 진행): {e}")
        
        # 2. HTML → PDF (wkhtmltopdf 우선, 없으면 weasyprint 폴백)
        pdf_path = output_dir / f"{hwp_path.stem}.pdf"
        wkhtmltopdf_success = False

        # 2-1. wkhtmltopdf 시도
        if shutil.which("wkhtmltopdf"):
            print("[DEBUG] HTML → PDF 변환 시작 (wkhtmltopdf)")
            try:
                result = subprocess.run(
                    [
                        "wkhtmltopdf",
                        "--enable-local-file-access",
                        "--encoding", "UTF-8",
                        "--no-stop-slow-scripts",
                        "--enable-javascript",
                        str(index_html),
                        str(pdf_path)
                    ],
                    capture_output=True,
                    text=True,
                    timeout=600
                )
            except subprocess.TimeoutExpired:
                print("[ERROR] wkhtmltopdf 타임아웃 (600초 초과)")
                if pdf_path.exists():
                    pdf_path.unlink()

            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                import fitz
                try:
                    doc = fitz.open(pdf_path)
                    pdf_page_count = len(doc)
                    doc.close()
                    html_size_kb = index_html.stat().st_size / 1024
                    print(f"[DEBUG] PDF 생성 완료: {pdf_path} ({pdf_path.stat().st_size} bytes, {pdf_page_count}페이지)")

                    if html_size_kb > 2000 and pdf_page_count < 10:
                        print(f"[WARNING] HTML({html_size_kb:.0f}KB)에 비해 PDF 페이지({pdf_page_count})가 너무 적음")
                        if result.stderr:
                            print(f"[DEBUG] wkhtmltopdf stderr: {result.stderr[:500]}")
                    elif pdf_page_count < 5 and result.stderr and ("error" in result.stderr.lower() or "fail" in result.stderr.lower()):
                        print("[WARNING] PDF 페이지 수 적고 에러 발생")
                        print(f"[DEBUG] stderr: {result.stderr[:500]}")
                    elif pdf_page_count > 500:
                        print(f"[WARNING] PDF 페이지 수가 비정상적으로 많음({pdf_page_count}페이지)")
                        if pdf_path.exists():
                            pdf_path.unlink()
                    else:
                        wkhtmltopdf_success = True
                except Exception as e:
                    print(f"[WARNING] PDF 페이지 수 확인 실패: {e}")
                    wkhtmltopdf_success = True  # 검증 실패해도 파일 있으면 사용
        else:
            print("[DEBUG] wkhtmltopdf 미설치, 대체 방법 시도")

        # 2-2. wkhtmltopdf 실패 시 xhtml2pdf 폴백
        if not wkhtmltopdf_success:
            try:
                from xhtml2pdf import pisa
                import re as _re_mod

                print("[DEBUG] HTML → PDF 변환 시작 (xhtml2pdf)")
                html_raw = index_html.read_text(encoding="utf-8")
                html_raw = _re_mod.sub(r"<link\b[^>]*rel=['\"]stylesheet['\"][^>]*>", "", html_raw, flags=_re_mod.IGNORECASE)
                html_raw = _re_mod.sub(r"font-family\s*:\s*\"[^\"]+\"\s*,\s*sans-serif\s*;", "font-family: sans-serif;", html_raw, flags=_re_mod.IGNORECASE)
                if pdf_path.exists():
                    pdf_path.unlink()

                with open(pdf_path, "wb") as pdf_handle:
                    pisa_result = pisa.CreatePDF(
                        html_raw,
                        dest=pdf_handle,
                        path=str(index_html),
                        link_callback=_resolve_html_resource,
                    )

                if pdf_path.exists() and pdf_path.stat().st_size > 0 and not getattr(pisa_result, "err", 0):
                    import fitz

                    doc = fitz.open(pdf_path)
                    pdf_page_count = len(doc)
                    doc.close()
                    print(f"[DEBUG] xhtml2pdf PDF 생성 완료: {pdf_path} ({pdf_path.stat().st_size} bytes, {pdf_page_count}페이지)")

                    if pdf_page_count > 500:
                        print(f"[WARNING] xhtml2pdf PDF 비정상 ({pdf_page_count}페이지)")
                        pdf_path.unlink()
                    else:
                        wkhtmltopdf_success = True
                else:
                    print("[WARNING] xhtml2pdf PDF 생성 실패")
                    if pdf_path.exists() and pdf_path.stat().st_size == 0:
                        pdf_path.unlink()
            except ImportError:
                print("[WARNING] xhtml2pdf 미설치")
            except Exception as e:
                print(f"[WARNING] xhtml2pdf 변환 실패: {e}")

        # 2-3. HTML → PDF 실패 시 weasyprint 폴백
        if not wkhtmltopdf_success:
            try:
                from weasyprint import HTML as WeasyprintHTML, CSS as WeasyprintCSS
                print("[DEBUG] HTML → PDF 변환 시작 (weasyprint)")

                # HTML에서 섹션 너비 감지하여 페이지 크기 결정
                import re as _re
                html_raw = index_html.read_text(encoding='utf-8')
                page_width_mm = 210  # 기본값: A4 세로
                page_height_mm = 297
                section_match = _re.search(r'\.Section-\d+\s*\{[^}]*width:\s*([\d.]+)mm', html_raw)
                if section_match:
                    section_width = float(section_match.group(1))
                    if section_width > 220:  # A4 세로(210mm)보다 넓으면 가로 방향
                        page_width_mm = section_width
                        page_height_mm = 210
                        print(f"[DEBUG] 가로 방향 감지: 섹션 너비 {section_width}mm → 페이지 {page_width_mm}x{page_height_mm}mm")

                # 여백 감지
                margin_top = margin_right = margin_bottom = margin_left = "10mm"
                margin_match = _re.search(
                    r'\.HeaderPageFooter\s*\{[^}]*'
                    r'margin-top:\s*([\d.]+mm)[^}]*'
                    r'margin-right:\s*([\d.]+mm)[^}]*'
                    r'margin-bottom:\s*([\d.]+mm)[^}]*'
                    r'margin-left:\s*([\d.]+mm)',
                    html_raw
                )
                if margin_match:
                    margin_top = margin_match.group(1)
                    margin_right = margin_match.group(2)
                    margin_bottom = margin_match.group(3)
                    margin_left = margin_match.group(4)

                page_css = WeasyprintCSS(string=(
                    f'@page {{ size: {page_width_mm}mm {page_height_mm}mm; '
                    f'margin: {margin_top} {margin_right} {margin_bottom} {margin_left}; }}'
                ))

                if pdf_path.exists():
                    pdf_path.unlink()
                WeasyprintHTML(filename=str(index_html)).write_pdf(str(pdf_path), stylesheets=[page_css])

                if pdf_path.exists() and pdf_path.stat().st_size > 0:
                    import fitz
                    doc = fitz.open(pdf_path)
                    pdf_page_count = len(doc)
                    doc.close()
                    print(f"[DEBUG] weasyprint PDF 생성 완료: {pdf_path} ({pdf_path.stat().st_size} bytes, {pdf_page_count}페이지)")

                    if pdf_page_count > 500:
                        print(f"[WARNING] weasyprint PDF 비정상 ({pdf_page_count}페이지)")
                        pdf_path.unlink()
                    else:
                        wkhtmltopdf_success = True  # weasyprint 성공
                else:
                    print("[WARNING] weasyprint PDF 생성 실패")
            except ImportError:
                print("[WARNING] weasyprint 미설치")
            except Exception as e:
                print(f"[WARNING] weasyprint 변환 실패: {e}")

        if wkhtmltopdf_success:
            shutil.rmtree(html_dir, ignore_errors=True)
            return (pdf_path, table_info)
        else:
            print("[WARNING] HTML → PDF 변환 실패 (wkhtmltopdf, weasyprint 모두 실패)")
            shutil.rmtree(html_dir, ignore_errors=True)
            return None

    except Exception as e:
        if _is_pyhwp_unsupported_error(str(e)):
            print(
                "[WARNING] HWP → HTML 변환 중 pyhwp 지원 불가 구조를 감지했습니다. "
                "다음 변환 경로로 폴백합니다."
            )
        else:
            print(f"[ERROR] HWP → PDF 변환 실패: {e}")
            import traceback
            traceback.print_exc()
        return None


def convert_to_pdf(src: Path, dst_dir: Path):
    """
    문서를 PDF로 변환
    HWP의 경우 (pdf_path, table_info) 튜플 반환, 그 외는 pdf_path만 반환
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    
    # HWP 파일은 HTML 경유 PDF 생성 (표/이미지 포함)
    if src.suffix.lower() in {".hwp", ".hwpx"}:
        if _is_legacy_hwp_format(src):
            print(f"[INFO] {_hwp_version_label(src)} 감지. HWP5 전용 pyhwp 경로를 건너뛰고 legacy PDF 변환을 시도합니다.")
            legacy_result = convert_hwp_to_pdf_via_legacy_converter(src, dst_dir)
            if legacy_result:
                return legacy_result
            lo_result = convert_hwp_to_pdf_via_libreoffice(src, dst_dir)
            if lo_result:
                return lo_result
            print("[WARNING] legacy HWP 변환기가 없거나 실패했습니다. 기존 fallback을 계속 시도합니다.")

        # 1) pyhwp(hwp5html) 시도 - OLE2 형식용
        print("[DEBUG] HWP → HTML → PDF 변환 시도")
        result = convert_hwp_to_pdf_via_html(src, dst_dir)
        if result:
            if isinstance(result, tuple):
                pdf_path, table_info = result
                if pdf_path and pdf_path.exists():
                    return (pdf_path, table_info)
            elif hasattr(result, 'exists') and result.exists():
                return (result, {"simple_tables": [], "complex_tables": []})
        
        # 2) HTML 변환 실패 또는 불완전 → LibreOffice 우선 시도 (OLE2/HWPX 모두)
        print("[INFO] HTML→PDF 변환 실패/불완전. LibreOffice로 재시도합니다.")
        lo_result = convert_hwp_to_pdf_via_libreoffice(src, dst_dir)
        if lo_result:
            if isinstance(lo_result, tuple):
                pdf_path, _ = lo_result
                if pdf_path and pdf_path.exists():
                    # LibreOffice PDF 페이지 수 검증
                    import fitz
                    try:
                        doc = fitz.open(pdf_path)
                        lo_page_count = len(doc)
                        doc.close()
                        print(f"[INFO] LibreOffice PDF 생성 성공: {lo_page_count}페이지")
                        return lo_result
                    except Exception:
                        return lo_result
            return lo_result

        if _is_hwpx_format(src):
            print("[INFO] LibreOffice 변환 실패. HWPX 요소 기반 PDF fallback을 시도합니다.")
            try:
                pdf_path = convert_hwpx_to_pdf_via_elements(src, dst_dir)
                return (pdf_path, {"simple_tables": [], "complex_tables": []})
            except ConversionError as e:
                print(f"[WARNING] HWPX 요소 기반 PDF fallback 실패: {e}")
        
        # 3) 텍스트 기반 PDF (pyhwp hwp5txt 또는 HWPX PrvText 추출) - 최종 폴백
        print("[WARNING] LibreOffice 변환도 실패. 텍스트 기반 PDF 변환 시도 (표/이미지 손실 가능)")
        try:
            pdf_path = convert_hwp_to_pdf_via_text(src, dst_dir)
            return (pdf_path, {"simple_tables": [], "complex_tables": []})
        except ConversionError as e:
            # 4) 최종: 오류 PDF 생성
            print("[ERROR] HWP 변환 완전 실패. 빈 PDF 생성")
            pdf_path = dst_dir / f"{src.stem}.pdf"
            from reportlab.pdfgen import canvas
            from reportlab.lib.pagesizes import A4
            c = canvas.Canvas(str(pdf_path), pagesize=A4)
            c.drawString(100, 750, f"HWP 파일 변환 실패: {src.name}")
            c.drawString(100, 730, f"오류: {str(e)}")
            c.save()
            return (pdf_path, {"simple_tables": [], "complex_tables": []})
    
    # 기타 문서는 LibreOffice 사용
    _lo = _find_libreoffice()
    if _lo:
        cmd = [
            _lo,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(dst_dir),
            str(src),
        ]
        _run(cmd)
    elif shutil.which("unoconv"):
        cmd = ["unoconv", "-f", "pdf", "-o", str(dst_dir), str(src)]
        _run(cmd)
    else:
        raise ConversionError("libreoffice 또는 unoconv가 필요합니다.")

    pdf_path = dst_dir / f"{src.stem}.pdf"
    if not pdf_path.exists():
        raise ConversionError(f"PDF 생성 실패: {pdf_path}")
    return pdf_path
