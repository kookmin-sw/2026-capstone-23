import json
import re
import html as html_module
from pathlib import Path
from typing import Dict, Any, List


_TABLE_TAG_RE = re.compile(r"</?table\b[^>]*>", re.IGNORECASE)


def _extract_first_balanced_table(text: str) -> str:
    """중첩 table이 있어도 바깥 table이 완전히 닫힌 HTML만 잘라낸다."""
    if not text or "<table" not in text.lower():
        return ""

    start_match = re.search(r"<table\b[^>]*>", text, re.IGNORECASE)
    if not start_match:
        return ""

    depth = 0
    start = start_match.start()
    for match in _TABLE_TAG_RE.finditer(text, start):
        tag = match.group(0).lower()
        if tag.startswith("</table"):
            depth -= 1
            if depth == 0:
                return text[start:match.end()].strip()
            if depth < 0:
                return ""
        else:
            depth += 1

    # 닫히지 않은 table을 그대로 내보내면 이후 문서 전체가 앞 셀 안으로 들어간다.
    return ""


def extract_multiple_tables(vlm_text: str) -> List[str]:
    """
    VLM 결과에서 여러 표를 추출
    [TABLE_N]...[/TABLE_N] 형식으로 감싸진 표들을 분리
    """
    tables = []
    pattern = r'\[TABLE_\d+\](.*?)\[/TABLE_\d+\]'
    matches = re.findall(pattern, vlm_text, re.DOTALL)
    
    if matches:
        for match in matches:
            table_html = match.strip()
            if table_html:
                tables.append(table_html)
    
    # [TABLE_N] 형식이 없으면 전체를 하나의 표로 간주
    if not tables and vlm_text.strip():
        tables.append(vlm_text.strip())
    
    return tables


def clean_vlm_image_for_rag(vlm_text: str) -> str:
    """
    VLM 이미지 분석 결과에서 RAG에 불필요한 AI 추론 과정을 제거하고
    실질적인 정보만 추출합니다.

    제거 대상:
    - AI 분류/추론 과정 (이미지 유형 판단, 출력 형식 선택 등)
    - 최종 확인/최종 출력 메타 문구
    - 중복 데이터 (시리즈 정보, 데이터 상세 - 데이터 표와 동일)
    - 무의미한 섹션 (이미지 유형, 부가 정보=없음, 숫자 수치=없음)
    - ```markdown 코드 블록 래퍼, --- 구분선
    """
    if not vlm_text or not vlm_text.strip():
        return vlm_text

    text = vlm_text.strip()

    # ── 1단계: ```markdown ... ``` 코드 블록이 있으면 마지막 블록 내용만 추출 ──
    code_blocks = re.findall(r'```(?:markdown)?\s+(.*?)```', text, re.DOTALL)
    if code_blocks:
        text = code_blocks[-1].strip()

    # ── 1.5단계: 인라인 헤딩 정규화 ──
    # VLM 출력이 한 줄로 저장된 경우 "내용 ## 제목" → "내용\n## 제목"으로 변환
    if text.count('\n') < 3:
        text = re.sub(r'(?<=\S)\s+(#{1,4}\s)', r'\n\1', text)

    # ── 2단계: AI 분류/추론 섹션 제거 ──
    # "### 이미지 분석 및 유형 판단" 전체 블록
    text = re.sub(
        r'#{2,4}\s*이미지 분석 및 유형 판단\s*\n.*?(?=#{2,4}\s[^#]|$)',
        '', text, flags=re.DOTALL
    )
    # "#### N. 이미지 관찰 및 유형 판단" 블록
    text = re.sub(
        r'#{2,4}\s*\d+\.\s*이미지 관찰 및 유형 판단.*?(?=#{2,4}\s*\d+\.|#{2,4}\s*[^#\d]|---|\Z)',
        '', text, flags=re.DOTALL
    )
    # "#### N. 차트 유형 및 특징" 블록 (차트 분석 서두 중복)
    text = re.sub(
        r'#{2,4}\s*\d+\.\s*차트 유형 및 특징.*?(?=#{2,4}\s*\d+\.|#{2,4}\s*[^#\d]|---|\Z)',
        '', text, flags=re.DOTALL
    )
    # "#### N. 출력 형식에 맞춘 분석" 블록
    text = re.sub(
        r'#{2,4}\s*\d+\.\s*출력 형식에 맞춘 분석.*?(?=#{2,4}\s|---|\Z)',
        '', text, flags=re.DOTALL
    )
    # "### 출력 형식 선택" 블록
    text = re.sub(
        r'#{2,4}\s*출력 형식 선택.*?(?=#{2,4}\s|---|\Z)',
        '', text, flags=re.DOTALL
    )
    # "### 출력 결과" 헤더만 제거 (본문은 유지)
    text = re.sub(r'#{2,4}\s*출력 결과\s*\n?', '', text)
    # "### 차트 분석 및 구조화된 출력" 헤더만 제거
    text = re.sub(r'#{2,4}\s*차트 분석 및 구조화된 출력\s*\n?', '', text)
    # "### 분석 결과" 헤더만 제거
    text = re.sub(r'#{2,4}\s*분석 결과\s*\n?', '', text)
    # "### 최종 확인" ~ 끝까지 제거
    text = re.sub(r'#{2,4}\s*최종 확인.*$', '', text, flags=re.DOTALL)
    # "### 최종 출력" ~ 끝까지 제거 (코드 블록은 이미 1단계에서 추출)
    text = re.sub(r'#{2,4}\s*최종 출력.*$', '', text, flags=re.DOTALL)

    # ── 2.5단계: 헤딩 정규화 ──
    # VLM이 "#### ## 제목" 또는 "### # 제목" 형태로 이중 헤딩 출력 → "## 제목"으로 통일
    # (3~4단계 섹션 제거가 깔끔하게 동작하도록 선행 정규화)
    text = re.sub(r'---\s*', '', text)  # 구분선 먼저 제거
    text = re.sub(r'(?:#+[ \t]*){2,}', '## ', text)

    # ── 3단계: 무의미한 메타 섹션 제거 ──
    # 헬퍼: 헤딩부터 다음 헤딩 전까지 섹션 추출 (인라인/멀티라인 모두 지원)
    def _extract_section(txt, heading_keyword):
        """heading_keyword를 포함하는 ## 섹션의 (시작, 끝, 내용)을 반환"""
        regex = r'(#{1,4}\s*' + re.escape(heading_keyword) + r'[^\n]*(?:\n(?!#{1,4}\s)[^\n]*)*)'
        pat = re.search(regex, txt)
        if pat:
            return pat.start(), pat.end(), pat.group(1)
        return None, None, None

    # "## 이미지 유형" 섹션 제거
    s, e, section = _extract_section(text, '이미지 유형')
    if section is not None:
        text = text[:s] + text[e:]

    # "## 숫자 및 수치 정보" - 정보가 없는 경우만 제거
    s, e, section = _extract_section(text, '숫자 및 수치 정보')
    if section is not None and ('없' in section or '포함되어 있지 않' in section):
        text = text[:s] + text[e:]

    # "## 부가 정보" - 모든 항목이 "없음"이면 제거
    s, e, section = _extract_section(text, '부가 정보')
    if section is not None:
        parts = [p.strip() for p in re.split(r'[-•]', section) if p.strip()]
        all_none = all('없음' in p or '없' in p or p.startswith('#') for p in parts)
        if all_none:
            text = text[:s] + text[e:]

    # ── 4단계: 차트/그래프 중복 데이터 제거 ──
    has_data_table = bool(re.search(r'데이터 표', text))
    if has_data_table:
        # "## 데이터 상세 (인덱스별)" 는 "## 데이터 표"와 중복 → 제거
        text = re.sub(
            r'#{1,4}\s*데이터 상세\s*\(인덱스별\).*?(?=#{1,4}\s|\Z)',
            '', text, flags=re.DOTALL
        )
        # "## 시리즈 정보" 도 데이터 표와 중복 → 제거
        text = re.sub(
            r'#{1,4}\s*시리즈 정보.*?(?=#{1,4}\s|\Z)',
            '', text, flags=re.DOTALL
        )

    # ── 5단계: 포맷 정리 ──
    # 3~4단계 제거로 남은 이중 헤딩 재정규화 (안전망)
    text = re.sub(r'(?:#+[ \t]*){2,}', '## ', text)
    # 고립된 "#" 만 있는 줄 제거 (내용 없는 헤더)
    text = re.sub(r'(?:^|\n)\s*#{1,4}\s*(?:\n|$)', '\n', text)
    # 연속 빈 줄 정리
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def remove_meta_explanations(text: str) -> str:
    """
    VLM 결과에서 불필요한 메타 설명 문구를 제거
    예: "단순 텍스트 문서입니다. 따라서 이미지에 있는 텍스트를 그대로 출력합니다."
    주의: 실제 이미지 설명은 유지하고, 단순 텍스트 출력에 대한 메타 설명만 제거
    """
    if not text:
        return text
    
    # 제거할 메타 설명 패턴들 (단순 텍스트 출력에 대한 설명만)
    # 긴 패턴부터 먼저 처리하여 부분 매칭 방지
    meta_patterns = [
        # 복합 패턴 (긴 것부터, 줄바꿈 포함)
        r'이\s*이미지는\s*단순\s*텍스트\s*문서입니다?\s*[\.。]?\s*원문을\s*그대로\s*출력하겠습니다?\s*:?\s*\n?\s*',
        r'단순\s*텍스트\s*문서입니다?\s*[\.。]?\s*원문\s*텍스트를\s*그대로\s*출력합니다?\s*[\.。:]?\s*\n?\s*',
        r'단순\s*텍스트\s*문서입니다?\s*[\.。]?\s*원문을\s*그대로\s*출력하겠습니다?\s*:?\s*\n?\s*',
        r'단순\s*텍스트\s*문서입니다?\s*[\.。]?\s*원문\s*텍스트는\s*다음과\s*같습니다?\s*:?\s*\n?\s*',
        r'단순\s*텍스트\s*문서입니다?\s*[\.。]?\s*다음은\s*이미지의\s*원문\s*텍스트입니다?\s*:?\s*\n?\s*',
        r'단순\s*텍스트\s*문서입니다?\s*[\.。]?\s*\n?\s*',
        r'따라서\s*이미지에\s*있는\s*텍스트를\s*그대로\s*출력합니다?\s*[\.。]?\s*\n?\s*',
        r'이미지에\s*있는\s*텍스트를\s*그대로\s*출력합니다?\s*[\.。]?\s*\n?\s*',
        # "원문 텍스트를..." 패턴을 "텍스트를..." 패턴보다 먼저 처리
        r'원문\s*텍스트를\s*그대로\s*출력합니다?\s*[\.。:]?\s*\n?\s*',
        r'원문을\s*그대로\s*출력하겠습니다?\s*:?\s*\n?\s*',
        r'원문을\s*그대로\s*출력합니다?\s*[\.。:]?\s*\n?\s*',
        r'텍스트를\s*그대로\s*출력합니다?\s*[\.。]?\s*\n?\s*',
        r'문서입니다?\s*[\.。]?\s*따라서\s*',
        r'이\s*이미지는\s*단순\s*텍스트\s*문서입니다?\s*[\.。]?\s*\n?\s*',
        r'단순\s*텍스트\s*입니다?\s*[\.。]?\s*\n?\s*',
        r'단순\s*텍스트\s*문서\s*입니다?\s*[\.。]?\s*\n?\s*',
        # 원문 텍스트 관련 설명 패턴 (줄바꿈 포함)
        r'원문\s*텍스트는\s*다음과\s*같습니다?\s*:?\s*\n?\s*',
        r'다음은\s*이미지의\s*원문\s*텍스트입니다?\s*:?\s*\n?\s*',
        r'다음은\s*원문\s*텍스트입니다?\s*:?\s*\n?\s*',
        r'원문\s*텍스트는\s*다음\s*과\s*같습니다?\s*:?\s*\n?\s*',
        r'원문\s*텍스트\s*:?\s*\n?\s*',
    ]
    
    cleaned = text
    for pattern in meta_patterns:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE | re.MULTILINE)
    
    # 문장 시작 부분의 불필요한 설명 제거 (단순 텍스트 출력에 대한 설명만)
    # "따라서", "그러므로", "즉" 등으로 시작하는 메타 설명 제거
    explanation_starters = [
        r'^따라서\s+(이미지에\s*있는\s*텍스트를\s*그대로\s*출력|텍스트를\s*그대로\s*출력)',
        r'^그러므로\s+(이미지에\s*있는\s*텍스트를\s*그대로\s*출력|텍스트를\s*그대로\s*출력)',
        r'^즉\s*,?\s*(이미지에\s*있는\s*텍스트를\s*그대로\s*출력|텍스트를\s*그대로\s*출력)',
        r'^이\s*문서는\s*단순\s*텍스트',
        r'^원문\s*텍스트는\s*다음과\s*같습니다?\s*:?\s*',
        r'^다음은\s*이미지의\s*원문\s*텍스트입니다?\s*:?\s*',
        r'^다음은\s*원문\s*텍스트입니다?\s*:?\s*',
    ]
    
    for pattern in explanation_starters:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE | re.MULTILINE)
    
    # 연속된 공백 정리
    cleaned = re.sub(r'\s+', ' ', cleaned)
    cleaned = cleaned.strip()
    
    return cleaned


def normalize_plain_text_output(text: str) -> str:
    """
    CER/NID 계산용 일반 텍스트 정규화.
    - HTML 엔티티 해제
    - 태그 제거
    - 공백/구두점/불릿 등 렌더러별 장식 차이 제거
    """
    if not text:
        return ""

    normalized = str(text)
    normalized = html_module.unescape(normalized).replace("\xa0", " ")
    normalized = re.sub(r"<br\s*/?>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?>", " ", normalized)
    normalized = re.sub(r"\b참조\s*(p\s*\d+)\b", r"\1참조", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b(p\s*\d+)\s*참조\b", lambda m: m.group(1).replace(" ", "") + "참조", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"(?<![A-Za-z0-9])o(?![A-Za-z0-9])", " ", normalized)
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", "", normalized)
    return normalized.strip()


def _is_fullpage_fallback_image(image_id: Any) -> bool:
    """PDF 텍스트 부족 보정으로 추가한 전체 페이지 이미지인지 확인."""
    return bool(re.fullmatch(r"page-\d+-fullpage", str(image_id or "")))


def _normalize_for_duplicate_text_check(text: str) -> str:
    if not text:
        return ""

    normalized = html_module.unescape(str(text)).lower()
    normalized = re.sub(r"\[\[/?(?:image|table|table_markdown)[^\]]*\]\]", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<[^>]+>", " ", normalized)
    normalized = remove_meta_explanations(normalized)
    normalized = re.sub(
        r"이\s*이미지는.*?(?:텍스트를\s*그대로\s*출력합니다|텍스트를\s*그대로\s*출력하겠습니다|이미지에\s*포함된\s*텍스트를\s*그대로\s*출력합니다)\s*[\.。:]?",
        " ",
        normalized,
        flags=re.DOTALL,
    )
    return re.sub(r"[^0-9a-z가-힣]+", "", normalized)


def _is_duplicate_of_page_text(image_text: str, page_text: str) -> bool:
    image_norm = _normalize_for_duplicate_text_check(image_text)
    page_norm = _normalize_for_duplicate_text_check(page_text)
    if len(image_norm) < 80 or len(page_norm) < 80:
        return False

    if image_norm in page_norm or page_norm in image_norm:
        return True

    image_sample = image_norm[:5000]
    page_sample = page_norm[:8000]
    similarity = SequenceMatcher(None, image_sample, page_sample, autojunk=False).ratio()
    return similarity >= 0.72
from core.output_cleaning import (
    _is_duplicate_of_page_text,
    _is_fullpage_fallback_image,
    clean_vlm_image_for_rag,
    normalize_plain_text_output as normalize_plain_text_output,
    remove_meta_explanations,
)


_HTML_ROW_RE = re.compile(r"<tr[^>]*>[\s\S]*?</tr>", re.IGNORECASE)
_HTML_CELL_RE = re.compile(r"<(td|th)([^>]*)>[\s\S]*?</\1>", re.IGNORECASE)


def _html_attr_int(attrs: str, name: str, default: int = 1) -> int:
    match = re.search(rf'{name}\s*=\s*["\']?(\d+)', attrs or "", re.IGNORECASE)
    if not match:
        return default
    try:
        return max(1, int(match.group(1)))
    except ValueError:
        return default


def _append_paragraph_to_cell(cell_html: str, text: str) -> str:
    escaped_lines = [
        html_module.escape(re.sub(r"^#{1,6}\s*", "", line.strip()))
        for line in (text or "").splitlines()
        if line.strip() and not re.fullmatch(r"[-*_]{3,}", line.strip())
    ]
    if not escaped_lines:
        return cell_html
    note_html = "<p>[차트 설명]</p>" + "".join(f"<p>{line}</p>" for line in escaped_lines)
    return re.sub(r"</(td|th)>\s*$", note_html + r"</\1>", cell_html, flags=re.IGNORECASE)


def _append_notes_to_table_cells(table_html: str, notes: List[Dict[str, Any]]) -> str:
    """row/col 메타로 지정된 셀에 표 내부 이미지 설명을 append한다."""
    if not table_html or not notes:
        return table_html

    notes_by_pos: Dict[tuple[int, int], List[str]] = {}
    for note in notes:
        row = note.get("row")
        col = note.get("col")
        text = str(note.get("text") or "").strip()
        if row is None or col is None or not text:
            continue
        notes_by_pos.setdefault((int(row), int(col)), []).append(text)
    if not notes_by_pos:
        return table_html

    rows = list(_HTML_ROW_RE.finditer(table_html))
    occupied: set[tuple[int, int]] = set()
    replacements: List[tuple[int, int, str]] = []
    for row_idx, row_match in enumerate(rows):
        row_html = row_match.group(0)
        col_idx = 0
        for cell_match in _HTML_CELL_RE.finditer(row_html):
            while (row_idx, col_idx) in occupied:
                col_idx += 1
            attrs = cell_match.group(2) or ""
            rowspan = _html_attr_int(attrs, "rowspan")
            colspan = _html_attr_int(attrs, "colspan")

            cell_notes: List[str] = []
            for dr in range(rowspan):
                for dc in range(colspan):
                    cell_notes.extend(notes_by_pos.get((row_idx + dr, col_idx + dc), []))
                    if dr or dc:
                        occupied.add((row_idx + dr, col_idx + dc))

            if cell_notes:
                appended = cell_match.group(0)
                for note_text in cell_notes:
                    appended = _append_paragraph_to_cell(appended, note_text)
                replacements.append((
                    row_match.start() + cell_match.start(),
                    row_match.start() + cell_match.end(),
                    appended,
                ))
            col_idx += colspan

    updated = table_html
    for start, end, replacement in reversed(replacements):
        updated = updated[:start] + replacement + updated[end:]
    return updated


def build_document_text(
    source_path: Path,
    page_count: int,
    pages: List[Dict[str, Any]],
    image_results: Dict[str, Dict[str, Any]],
) -> str:
    """
    bbox 기반 reading order로 문서 텍스트 생성
    1. 텍스트/이미지/표를 모두 bbox로 추출 (완료)
    2. Reading order 결정 (완료)
    3. 이미지/표 위치에 placeholder 삽입
    4. VLM 결과로 placeholder 치환
    """
    lines: List[str] = []
    lines.append(f"원본 파일: {source_path}")
    lines.append(f"페이지 수: {page_count}")
    lines.append("-" * 60)

    emitted_native_table_ids = {
        elem.get("image_id")
        for page in pages
        for elem in page.get("elements", [])
        if elem.get("type") == "native_table" and str(elem.get("html", "")).strip()
    }
    embedded_cell_notes: Dict[str, List[Dict[str, Any]]] = {}
    embedded_image_ids_with_text: set[str] = set()
    for page in pages:
        for elem in page.get("elements", []):
            if elem.get("type") != "image" or not elem.get("is_embedded_in_table_cell"):
                continue
            img_id = elem.get("image_id")
            ref = elem.get("embedded_table_cell") or {}
            vlm_text = image_results.get(img_id, {}).get("text", "").strip()
            if not img_id or not vlm_text or not ref.get("table_image_id"):
                continue
            if ref["table_image_id"] not in emitted_native_table_ids:
                continue
            # 셀 내부 이미지는 페이지가 달라도 병합된 표에 붙을 수 있으므로 먼저 전역으로 모은다.
            embedded_image_ids_with_text.add(img_id)
            embedded_cell_notes.setdefault(ref["table_image_id"], []).append({
                "row": ref.get("row"),
                "col": ref.get("col"),
                "text": clean_vlm_image_for_rag(vlm_text),
            })
    
    for page in pages:
        page_no = page["page"]
        lines.append(f"\n## Page {page_no}")
        
        # bbox 기반 요소 처리
        elements = page.get("elements", [])
        page_text_reference = "\n".join(
            str(elem.get("content", "")).strip()
            for elem in elements
            if elem.get("type") == "text" and str(elem.get("content", "")).strip()
        )
        if elements:
            # Reading order 순서대로 요소 처리
            for elem in elements:
                elem_type = elem.get("type")
                
                if elem_type == "text":
                    # 텍스트 블록 삽입
                    content = elem.get("content", "").strip()
                    if content:
                        # 쪽번호 패턴 제거: "- 숫자 -" 형식
                        if not re.match(r'^\s*-\s*\d+\s*-\s*$', content):
                            # 단독 숫자도 쪽번호로 의심하여 제거 (문맥이 없는 경우)
                            if not (re.match(r'^\s*\d+\s*$', content) and len(content.strip()) <= 5):
                                lines.append(content)
                                lines.append("")  # 블록 구분
                
                elif elem_type == "native_table":
                    # PDF 내부 데이터로 직접 추출한 표 (VLM 없음)
                    table_html = elem.get("html", "").strip()
                    cell_notes = embedded_cell_notes.get(elem.get("image_id"), [])
                    if cell_notes:
                        table_html = _append_notes_to_table_cells(table_html, cell_notes)
                    if table_html:
                        lines.append("[[TABLE]]")
                        lines.append(table_html)
                        lines.append("[[/TABLE]]")
                        lines.append("")

                elif elem_type == "table":
                    # HWPX XML 표: HTML + Markdown 출력 (HWP/PDF와 동일 형식)
                    table_html = elem.get("content", "").strip()
                    table_markdown = elem.get("markdown", "").strip()
                    if table_html:
                        lines.append("[[TABLE]]")
                        lines.append(table_html)
                        lines.append("[[/TABLE]]")
                    if table_markdown:
                        if table_html:
                            lines.append("")
                        lines.append("[[TABLE_MARKDOWN]]")
                        lines.append(table_markdown)
                        lines.append("[[/TABLE_MARKDOWN]]")
                    lines.append("")
                
                elif elem_type == "image":
                    # 이미지/표 placeholder → VLM 치환
                    img_id = elem.get("image_id")
                    if img_id in embedded_image_ids_with_text:
                        continue
                    is_fullpage_fallback = _is_fullpage_fallback_image(img_id)
                    vlm_result = image_results.get(img_id, {}).get("text", "").strip()
                    
                    if vlm_result:
                        # VLM 결과 분석: 표인지, 이미지인지, 단순 텍스트인지 판단
                        has_table_html = "<table" in vlm_result.lower()
                        
                        # 테이블 Markdown 처리
                        table_markdown = ""
                        text_content = ""  # 표 외 텍스트 초기화
                        cleaned_result = ""
                        
                        # 먼저 Markdown 구분자 확인 (표 이미지인 경우 HTML과 Markdown 모두 있어야 함)
                        if "===TABLE_MARKDOWN===" in vlm_result:
                            # 구분자가 있는 경우 (HTML + Markdown)
                            parts = vlm_result.split("===TABLE_MARKDOWN===")
                            if len(parts) > 1:
                                # HTML 부분과 Markdown 부분 분리
                                html_part = parts[0].strip()
                                markdown_part = parts[1].strip()
                                
                                # ===TEXT_CONTENT=== 구분자 확인 (표 외 텍스트)
                                if "===TEXT_CONTENT===" in markdown_part:
                                    text_parts = markdown_part.split("===TEXT_CONTENT===")
                                    table_markdown = text_parts[0].strip()
                                    text_content = text_parts[1].strip() if len(text_parts) > 1 else ""
                                else:
                                    table_markdown = markdown_part
                                    text_content = ""
                                
                                # HTML 부분 추출 (코드 블록 또는 직접 HTML)
                                if "```html" in html_part:
                                    start = html_part.find("```html") + 7
                                    end = html_part.find("```", start)
                                    if end > start:
                                        cleaned_result = html_part[start:end].strip()
                                elif "```" in html_part:
                                    start = html_part.find("```") + 3
                                    end = html_part.find("```", start)
                                    if end > start:
                                        cleaned_result = html_part[start:end].strip()
                                else:
                                    # HTML 태그만 추출
                                    balanced_table = _extract_first_balanced_table(html_part)
                                    if balanced_table:
                                        cleaned_result = balanced_table
                                    else:
                                        # HTML 태그가 없으면 전체를 HTML로 간주 (VLM이 코드 블록 없이 출력한 경우)
                                        cleaned_result = html_part.strip()

                                balanced_table = _extract_first_balanced_table(cleaned_result)
                                if balanced_table:
                                    cleaned_result = balanced_table
                                elif "<table" in cleaned_result.lower():
                                    cleaned_result = ""
                                
                                # HTML이 추출되었는지 확인
                                has_table_html = bool(cleaned_result and "<table" in cleaned_result.lower())
                        elif "# TableTitle:" in vlm_result:
                            # Markdown만 있는 경우 (# TableTitle로 시작)
                            table_markdown = vlm_result.strip()
                            # HTML이 VLM 응답에 숨어있을 수 있으므로 다시 확인
                            # 전체 응답에서 HTML 표 태그 검색
                            balanced_table = _extract_first_balanced_table(vlm_result)
                            if balanced_table:
                                cleaned_result = balanced_table
                                has_table_html = True
                            else:
                                has_table_html = False
                        elif has_table_html:
                            # HTML 표 구조 확인: 실제 표 구조인지 판단
                            # 마크다운 코드 블록 제거 후 표 구조 확인
                            cleaned_result = vlm_result
                            if "```html" in cleaned_result:
                                start = cleaned_result.find("```html") + 7
                                end = cleaned_result.find("```", start)
                                if end > start:
                                    cleaned_result = cleaned_result[start:end].strip()
                            elif "```" in cleaned_result:
                                start = cleaned_result.find("```") + 3
                                end = cleaned_result.find("```", start)
                                if end > start:
                                    cleaned_result = cleaned_result[start:end].strip()
                            else:
                                # HTML 태그만 추출
                                balanced_table = _extract_first_balanced_table(vlm_result)
                                if balanced_table:
                                    cleaned_result = balanced_table

                            balanced_table = _extract_first_balanced_table(cleaned_result)
                            if balanced_table:
                                cleaned_result = balanced_table
                            elif "<table" in cleaned_result.lower():
                                cleaned_result = ""
                                has_table_html = False
                        
                        # 표 구조 확인: HTML이 있으면 검증
                        is_real_table = False
                        if cleaned_result and has_table_html:
                            tr_count = len(re.findall(r'<tr[^>]*>', cleaned_result, re.IGNORECASE))
                            td_th_count = len(re.findall(r'<(td|th)[^>]*>', cleaned_result, re.IGNORECASE))
                            
                            # 표 구조가 명확한 경우 (2행 이상 또는 셀이 여러 개)
                            is_real_table = tr_count >= 2 or td_th_count >= 3
                        
                        # 표 외 텍스트 추출 (===TEXT_CONTENT=== 구분자 확인)
                        if "===TEXT_CONTENT===" in vlm_result and not text_content:
                            # ===TEXT_CONTENT=== 구분자로 분리
                            text_parts = vlm_result.split("===TEXT_CONTENT===")
                            if len(text_parts) > 1:
                                text_content = text_parts[-1].strip()
                        
                        # 표 이미지 처리: HTML과 Markdown 모두 출력해야 함
                        # Markdown이 있으면 표로 간주 (VLM이 표로 인식한 것)
                        is_table_image = bool(table_markdown or (has_table_html and is_real_table))
                        
                        if is_table_image:
                            # HTML이 있으면 먼저 출력
                            if has_table_html and is_real_table:
                                table_html = cleaned_result
                                lines.append("[[TABLE]]")
                                lines.append(table_html)
                                lines.append("[[/TABLE]]")
                            
                            # Markdown 출력 (RAG용)
                            if table_markdown:
                                if has_table_html and is_real_table:
                                    lines.append("")  # HTML과 Markdown 사이 구분
                                lines.append("[[TABLE_MARKDOWN]]")
                                lines.append(table_markdown)
                                lines.append("[[/TABLE_MARKDOWN]]")
                            
                            # 표 외 텍스트 추가
                            if text_content:
                                lines.append("")
                                lines.append(text_content)
                        elif has_table_html and is_real_table:
                            # HTML만 있고 Markdown은 없는 경우
                            table_html = cleaned_result
                            lines.append("[[TABLE]]")
                            lines.append(table_html)
                            lines.append("[[/TABLE]]")
                            
                            # 표 외 텍스트 추가
                            if text_content:
                                lines.append("")
                                lines.append(text_content)
                        else:
                            if is_fullpage_fallback:
                                continue

                            if _is_duplicate_of_page_text(vlm_result, page_text_reference):
                                continue

                            # 단순 텍스트인지 이미지 설명인지 판단
                            # 단순 텍스트: HTML 태그 없고, 설명 문구 없고, 원문 그대로인 경우
                            has_html_tags = "<" in vlm_result and ">" in vlm_result
                            has_table_tags = "<table" in vlm_result.lower() or "<tr" in vlm_result.lower() or "<td" in vlm_result.lower()
                            has_explanation_keywords = any(keyword in vlm_result.lower() for keyword in [
                                "이 이미지는", "이 그림은", "이 차트는", "이 표는",
                                "이 문서는", "위 이미지", "위 그림", "위 차트",
                                "설명", "요약", "분석", "해석", "보여주는", "나타내는",
                                "다음과 같", "다음과 같이", "다음 내용"
                            ])
                            
                            # 단순 텍스트 판단: HTML 태그 없고, 설명 문구 없고, 표 구조 없음
                            is_simple_text = (
                                not has_html_tags and  # HTML 태그 없음
                                not has_table_tags and  # 표 태그 없음
                                not has_explanation_keywords and  # 설명 문구 없음
                                len(vlm_result.strip()) > 10  # 최소 길이 체크
                            )
                            
                            if is_simple_text:
                                # 단순 텍스트는 원문 그대로 출력 (태그 없이)
                                # 불필요한 메타 설명 문구 제거
                                cleaned_text = remove_meta_explanations(vlm_result)
                                if cleaned_text:
                                    lines.append(cleaned_text)
                            else:
                                # 이미지/차트 설명 → RAG용 정리 후 출력
                                cleaned_vlm = clean_vlm_image_for_rag(vlm_result)
                                if cleaned_vlm:
                                    lines.append("[[IMAGE]]")
                                    lines.append(cleaned_vlm)
                                    lines.append("[[/IMAGE]]")
                    else:
                        if is_fullpage_fallback:
                            continue

                        # VLM 결과 없으면 fallback 메시지로 [[IMAGE]] 블록 출력 (앱에서 placeholder 오류 방지)
                        fallback_msg = "이미지 분석 결과를 가져오지 못했습니다. (토큰 제한 또는 API 오류 가능성)"
                        lines.append("[[IMAGE]]")
                        lines.append(fallback_msg)
                        lines.append("[[/IMAGE]]")
                    
                    lines.append("")  # 블록 구분
        
        else:
            # 기존 방식 (호환성) - <표>, <그림> 태그를 VLM 결과로 치환
            text = page.get("text", "").strip()
            page_images = page.get("images", [])
            
            # <표>와 <그림> 태그를 순차적으로 VLM 결과로 치환
            if (("<그림>" in text) or ("<표>" in text)) and page_images:
                page_text_reference = text
                text_lines = text.split('\n')
                result_lines = []
                img_idx = 0
                
                for line in text_lines:
                    # <그림> 또는 <표> 태그가 있으면 VLM 결과로 치환
                    if ("<그림>" in line or "<표>" in line) and img_idx < len(page_images):
                        img = page_images[img_idx]
                        img_id = img.get("image_id", "")
                        vlm_result = image_results.get(img_id, {}).get("text", "").strip()
                        
                        if vlm_result:
                            # <표> 태그 처리
                            if "<표>" in line:
                                # VLM 결과에서 HTML 표 추출 시도
                                has_table_html = "<table" in vlm_result.lower()
                                
                                if has_table_html:
                                    # HTML 표 구조 확인: 실제 표 구조인지 판단
                                    # 마크다운 코드 블록 제거 후 표 구조 확인
                                    cleaned_result = vlm_result
                                    if "```html" in cleaned_result:
                                        start = cleaned_result.find("```html") + 7
                                        end = cleaned_result.find("```", start)
                                        if end > start:
                                            cleaned_result = cleaned_result[start:end].strip()
                                    elif "```" in cleaned_result:
                                        start = cleaned_result.find("```") + 3
                                        end = cleaned_result.find("```", start)
                                        if end > start:
                                            cleaned_result = cleaned_result[start:end].strip()

                                    balanced_table = _extract_first_balanced_table(cleaned_result)
                                    if balanced_table:
                                        cleaned_result = balanced_table
                                    elif "<table" in cleaned_result.lower():
                                        cleaned_result = ""
                                        has_table_html = False
                                    
                                    # 표 구조 확인: <tr> 태그가 2개 이상이거나, <td>/<th> 태그가 여러 개 있으면 표로 간주
                                    tr_count = len(re.findall(r'<tr[^>]*>', cleaned_result, re.IGNORECASE))
                                    td_th_count = len(re.findall(r'<(td|th)[^>]*>', cleaned_result, re.IGNORECASE))
                                    
                                    # 표 구조가 명확한 경우 (2행 이상 또는 셀이 여러 개)
                                    is_real_table = tr_count >= 2 or td_th_count >= 3
                                    
                                    if is_real_table:
                                        # 실제 표 구조: HTML 표로 삽입
                                        table_html = cleaned_result
                                        result_lines.append("[[TABLE]]")
                                        result_lines.append(table_html)
                                        result_lines.append("[[/TABLE]]")
                                    else:
                                        # 표 구조가 불명확한 경우: 단순 텍스트로 간주할 수 있지만,
                                        # 설명 문구가 없고 텍스트가 자연스러우면 단순 텍스트로 출력
                                        text_only = re.sub(r'<[^>]+>', '', cleaned_result)
                                        text_only = re.sub(r'\s+', ' ', text_only).strip()
                                        
                                        has_explanation = any(keyword in vlm_result.lower() for keyword in [
                                            "이 이미지는", "이 그림은", "이 차트는", "이 표는",
                                            "이 문서는", "위 이미지", "위 그림", "위 차트",
                                            "설명", "요약", "분석", "해석", "보여주는", "나타내는",
                                            "다음과 같", "다음과 같이", "다음 내용"
                                        ])
                                        
                                        if not has_explanation and len(text_only) > 20:
                                            # 단순 텍스트로 판단: HTML 표를 제거하고 순수 텍스트만 출력
                                            # 불필요한 메타 설명 문구 제거
                                            cleaned_text = remove_meta_explanations(text_only)
                                            if cleaned_text:
                                                result_lines.append(cleaned_text)
                                        else:
                                            # 설명이 있거나 텍스트가 짧으면 표로 처리
                                            table_html = cleaned_result
                                            result_lines.append("[[TABLE]]")
                                            result_lines.append(table_html)
                                            result_lines.append("[[/TABLE]]")
                                else:
                                    # 단순 텍스트인지 판단
                                    has_html_tags = "<" in vlm_result and ">" in vlm_result
                                    has_table_tags = "<table" in vlm_result.lower() or "<tr" in vlm_result.lower() or "<td" in vlm_result.lower()
                                    has_explanation_keywords = any(keyword in vlm_result.lower() for keyword in [
                                        "이 이미지는", "이 그림은", "이 차트는", "이 표는",
                                        "이 문서는", "위 이미지", "위 그림", "위 차트",
                                        "설명", "요약", "분석", "해석", "보여주는", "나타내는",
                                        "다음과 같", "다음과 같이", "다음 내용"
                                    ])
                                    
                                    is_simple_text = (
                                        not has_html_tags and
                                        not has_table_tags and
                                        not has_explanation_keywords and
                                        len(vlm_result.strip()) > 10
                                    )
                                    
                                    if is_simple_text:
                                        # 단순 텍스트는 원문 그대로 출력
                                        # 불필요한 메타 설명 문구 제거
                                        cleaned_text = remove_meta_explanations(vlm_result)
                                        if cleaned_text:
                                            result_lines.append(cleaned_text)
                                    elif len(vlm_result.strip()) > 20:
                                        # VLM이 표를 인식하지 못했지만 텍스트가 있으면 사용
                                        result_lines.append("[[TABLE]]")
                                        result_lines.append(vlm_result)
                                        result_lines.append("[[/TABLE]]")
                                    else:
                                        # 원본 유지
                                        result_lines.append(line)
                            # <그림> 태그 처리
                            elif "<그림>" in line:
                                if _is_duplicate_of_page_text(vlm_result, page_text_reference):
                                    img_idx += 1
                                    continue

                                # 단순 텍스트인지 이미지 설명인지 판단
                                has_html_tags = "<" in vlm_result and ">" in vlm_result
                                has_table_tags = "<table" in vlm_result.lower() or "<tr" in vlm_result.lower() or "<td" in vlm_result.lower()
                                has_explanation_keywords = any(keyword in vlm_result.lower() for keyword in [
                                    "이 이미지는", "이 그림은", "이 차트는", "이 표는",
                                    "이 문서는", "위 이미지", "위 그림", "위 차트",
                                    "설명", "요약", "분석", "해석", "보여주는", "나타내는",
                                    "다음과 같", "다음과 같이", "다음 내용"
                                ])
                                
                                is_simple_text = (
                                    not has_html_tags and
                                    not has_table_tags and
                                    not has_explanation_keywords and
                                    len(vlm_result.strip()) > 10
                                )
                                
                                if is_simple_text:
                                    # 단순 텍스트는 원문 그대로 출력
                                    # 불필요한 메타 설명 문구 제거
                                    cleaned_text = remove_meta_explanations(vlm_result)
                                    if cleaned_text:
                                        result_lines.append(cleaned_text)
                                else:
                                    # 이미지 설명 → RAG용 정리 후 출력
                                    cleaned_vlm = clean_vlm_image_for_rag(vlm_result)
                                    if cleaned_vlm:
                                        result_lines.append("[[IMAGE]]")
                                        result_lines.append(cleaned_vlm)
                                        result_lines.append("[[/IMAGE]]")
                        else:
                            # VLM 결과 없으면 원본 태그 유지
                            result_lines.append(line)
                        
                        img_idx += 1
                    else:
                        # 태그 없으면 원본 라인 추가
                        result_lines.append(line)
                
                text = '\n'.join(result_lines)
            
            lines.append(text if text else "(텍스트 없음)")
            lines.append("")  # 페이지 구분

    return "\n".join(lines)


def write_outputs(output_txt: Path, content: str, meta: Dict[str, Any]) -> None:
    output_txt.parent.mkdir(parents=True, exist_ok=True)
    with open(output_txt, "w", encoding="utf-8") as f:
        f.write(content)

    meta_path = output_txt.with_suffix(".meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
