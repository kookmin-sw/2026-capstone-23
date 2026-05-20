from __future__ import annotations

import html as html_module
import re
from difflib import SequenceMatcher
from typing import Any


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
