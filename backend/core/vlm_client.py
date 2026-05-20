from typing import Dict, Any, Optional, Tuple
import base64
import tempfile
import os
import re
import io
import sys
import threading
from PIL import Image

try:
    import boto3
    BEDROCK_AVAILABLE = True
except ImportError:
    BEDROCK_AVAILABLE = False

try:
    from anthropic import Anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from core.env import env_bool, env_str


class VLMChartService:
    """Minimal OpenAI-compatible image service used by VLMClient."""

    def __init__(
        self,
        use_openai: bool = True,
        use_paddleocr: bool = False,
        openai_api_key: Optional[str] = None,
        openai_model: str = "gpt-5.2",
        openai_base_url: Optional[str] = None,
        device: str = "cpu",
    ) -> None:
        self.use_openai = use_openai
        self.use_paddleocr = use_paddleocr
        self.openai_model = openai_model
        self.device = device
        self.openai_client = None

        if not self.use_openai:
            return

        api_key = openai_api_key or env_str("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for OpenAI-compatible VLM mode.")
        if OpenAI is None:
            raise ImportError("openai package is not installed. Install dependencies first.")

        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if openai_base_url:
            client_kwargs["base_url"] = openai_base_url
        self.openai_client = OpenAI(**client_kwargs)

    def _encode_image_to_base64(self, image: Image.Image) -> str:
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode("utf-8")

    def _preprocess_image(self, image_path: Any) -> Image.Image:
        image = Image.open(image_path) if isinstance(image_path, (str, os.PathLike)) else image_path
        if image.mode != "RGB":
            image = image.convert("RGB")
        return image


def _safe_console_preview(value: Any, limit: int = 500) -> str:
    text = "" if value is None else str(value)
    if limit > 0:
        text = text[:limit]
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _sanitize_openai_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x00", " ")
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def _build_openai_image_data_url(
    base64_image: str,
    *,
    force_compact: bool = False,
    max_dimension: int = 1600,
    jpeg_quality: int = 85,
    compact_size_threshold_bytes: int = 1_500_000,
) -> tuple[str, dict[str, Any]]:
    raw_bytes = base64.b64decode(base64_image)
    meta = {
        "inputBytes": len(raw_bytes),
        "outputBytes": len(raw_bytes),
        "mime": "image/png",
        "width": None,
        "height": None,
        "compacted": False,
    }

    try:
        image = Image.open(io.BytesIO(raw_bytes))
        image.load()
        width, height = image.size
        meta["width"] = width
        meta["height"] = height

        should_compact = force_compact or len(raw_bytes) > compact_size_threshold_bytes or max(width, height) > max_dimension
        if not should_compact:
            return f"data:image/png;base64,{base64_image}", meta

        if image.mode not in {"RGB", "L"}:
            converted = Image.new("RGB", image.size, (255, 255, 255))
            converted.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
            image = converted
        elif image.mode != "RGB":
            image = image.convert("RGB")

        current_width, current_height = image.size
        if max(current_width, current_height) > max_dimension:
            scale = max_dimension / float(max(current_width, current_height))
            resized = (
                max(1, int(round(current_width * scale))),
                max(1, int(round(current_height * scale))),
            )
            image = image.resize(resized, Image.Resampling.LANCZOS)
            meta["width"], meta["height"] = image.size

        output_buffer = io.BytesIO()
        image.save(output_buffer, format="JPEG", quality=jpeg_quality, optimize=True)
        output_bytes = output_buffer.getvalue()
        encoded = base64.b64encode(output_bytes).decode("utf-8")

        meta["outputBytes"] = len(output_bytes)
        meta["mime"] = "image/jpeg"
        meta["compacted"] = True
        return f"data:image/jpeg;base64,{encoded}", meta
    except Exception as exc:  # noqa: BLE001
        print(f"[WARNING] OpenAI image payload normalization failed; fallback to original PNG: {exc}")
        return f"data:image/png;base64,{base64_image}", meta


def _is_openai_payload_parse_error(exc: Exception) -> bool:
    error_text = f"{type(exc).__name__}: {exc}".lower()
    patterns = (
        "could not parse the json body",
        "expects a json payload",
        "image_parse_error",
        "unsupported image",
    )
    return any(pattern in error_text for pattern in patterns)

TABLE_PROMPT = """당신은 표 이미지를 HTML 표로 변환하는 변환기입니다.

- 이미지에 보이는 내용을 정확히 전사하세요. 추측하거나 변형하지 마세요.
- 이미지가 회전되어 있어도 텍스트 방향을 파악하여 올바른 순서로 추출하세요.

[출력 형식]
1) HTML <table> 출력 (완전한 구조)
2) 구분자 ===TABLE_MARKDOWN=== 후 Markdown 표 출력
3) 표 외 텍스트(주석, 각주 등)가 있으면 ===TEXT_CONTENT=== 후 출력
4) 표가 아니면 이미지의 텍스트만 그대로 출력 (HTML 금지)

[HTML 규칙]
- 헤더 셀은 <th>, 데이터 셀은 <td>
- 병합된 셀은 rowspan/colspan 속성 사용
- 같은 열에 같은 텍스트가 연속 행에 반복되면 rowspan으로 병합
- 빈 셀이 연속되면 colspan으로 병합
- 모든 행에서 colspan 합 + rowspan 점유 = 총 열 수
- 모든 행을 빠짐없이 출력 (표의 시작부터 끝까지)

[셀 내용]
- 모든 셀의 텍스트를 이미지에서 정확히 읽어 전사
- 숫자, 단위, 괄호 레이블을 정확히 복사
- 추측하지 말고 각 셀을 개별적으로 확인

[Markdown 출력]
===TABLE_MARKDOWN=== 구분자 후 Markdown 표 형식으로 출력:

# TableTitle: [표 제목 또는 주요 주제]

| 열1 | 열2 | 열3 |
|-----|-----|-----|
| 값1 | 값2 | 값3 |

응답은 {language}로 작성하세요."""


FLOWCHART_PROMPT = """당신은 흐름도(Flowchart) 이미지를 분석하여 RAG 시스템에서 검색 가능한 구조화된 형식으로 변환하는 전문가입니다.

**절대 필수: 일관성 보장**
- 동일한 이미지를 분석할 때마다 **정확히 동일한 결과**를 출력해야 합니다.
- 텍스트, 구조, 경로를 **정확히 동일하게** 읽고 변환하세요.
- 추측하거나 변형하지 말고, 이미지에 보이는 내용을 **그대로** 정확히 전사하세요.

**절대 필수: 흐름도 분석 원칙**
1. **이미지를 시각적으로 분석**: 텍스트만 나열하지 말고, 화살표, 도형, 연결선, 조건 분기 등을 모두 인식하세요.
2. **모든 경로를 완전히 기술**: 시작점부터 모든 종료점까지의 모든 가능한 경로를 빠짐없이 나열하세요.
3. **구조화된 형식으로 출력**: 아래 지정된 형식을 정확히 따라 출력하세요.

**출력 형식 (반드시 이 형식으로 출력하세요):**

```markdown
# 흐름도 제목: [흐름도의 제목 또는 주제]

## 개요
[흐름도의 전체적인 목적과 기능을 간단히 설명]

## 시작점
- 시작: [시작 단계의 이름과 설명]

## 모든 경로 상세 설명

### 경로 1: [경로 이름 또는 설명]
**조건/상황**: [이 경로가 실행되는 조건]
1. [단계 1 이름]
   - 작업: [이 단계에서 수행되는 작업의 상세 설명]
   - 입력: [필요한 입력 데이터나 조건]
   - 출력: [생성되는 출력이나 상태 변화]
   - 다음 단계: [다음으로 이동하는 단계]

2. [단계 2 이름]
   - 작업: [상세 작업 설명]
   - 조건: [조건이 있다면]
   - 조건 True → [True일 때 다음 단계]
   - 조건 False → [False일 때 다음 단계]

3. [단계 3 이름]
   - 작업: [상세 작업 설명]
   - 다음 단계: [다음 단계]

[계속해서 이 경로의 모든 단계를 나열]

**종료점**: [이 경로의 종료 조건 및 결과]

### 경로 2: [다른 경로 이름]
[경로 1과 동일한 형식으로 기술]

### 경로 3: [반복 경로]
**반복 유형**: [반복의 종류: while, for, do-while 등]
**반복 시작 조건**: [반복이 시작되는 조건]
**반복 내용**:
1. [반복 내부 단계 1]
2. [반복 내부 단계 2]
3. [반복 내부 단계 3]

**반복 종료 조건**: [반복이 종료되는 조건]
**반복 종료 후**: [반복 종료 후 다음 단계]

### 경로 4: [역방향 경로]
**역방향 조건**: [되돌아가는 조건]
1. [현재 단계] → [이전 단계로 되돌아감]
2. [이전 단계에서 재처리]
3. [다시 진행]

### 경로 5: [예외/에러 경로]
**에러 발생 조건**: [에러가 발생하는 조건]
1. [에러 감지 단계]
2. [에러 처리 단계]
3. [에러 복구 또는 종료]

## 조건 분기 상세

### 조건 1: [조건 이름]
- **조건 설명**: [조건의 상세 설명]
- **조건 True 경로**: [True일 때의 전체 경로]
- **조건 False 경로**: [False일 때의 전체 경로]

### 조건 2: [다른 조건]
[동일한 형식으로 기술]

## 모든 종료점
- **종료점 A**: [종료 조건 및 최종 결과]
- **종료점 B**: [종료 조건 및 최종 결과]
- **종료점 C**: [종료 조건 및 최종 결과]

## 부가 정보
- [단계 X] 참고사항: [주석이나 참고 내용]
- [조건 Y] 예외: [예외 상황 설명]
- [경로 Z] 주의사항: [주의해야 할 사항]
```

**중요 지시사항:**
1. **모든 화살표와 연결선을 추적**: 이미지의 모든 화살표를 따라가며 모든 경로를 찾으세요.
2. **조건 분기의 모든 경우의 수**: 각 조건문의 True/False 경로를 모두 기술하세요.
3. **반복 구조 명확히**: 반복의 시작, 조건, 종료를 명확히 구분하세요.
4. **역방향 흐름 포함**: 되돌아가는 화살표가 있으면 반드시 포함하세요.
5. **예외 처리 포함**: 에러 처리, 재시도 로직을 모두 포함하세요.
6. **각 단계의 상세 설명**: 단순히 "입력"이라고 하지 말고 "2개의 숫자를 입력받음"처럼 상세히 기술하세요.
7. **RAG 검색 최적화**: "조건 X일 때 어떻게 되나요?", "에러가 발생하면 어떻게 되나요?" 같은 질문에 답할 수 있도록 모든 정보를 포함하세요.

**절대 하지 말아야 할 것:**
- 단순히 텍스트만 나열하지 마세요 (예: "시작 입력 에러 종료")
- 경로를 생략하지 마세요
- 조건의 일부 경우만 기술하지 마세요
- 반복 구조를 간략화하지 마세요

응답은 {language}로 작성하세요."""

MATH_PROMPT = """당신은 수학/교육 자료 이미지를 분석하여 RAG 시스템에서 검색 가능한 구조화된 형식으로 변환하는 전문가입니다.

**절대 필수: 일관성 보장**
- 동일한 이미지를 분석할 때마다 **정확히 동일한 결과**를 출력해야 합니다.
- 텍스트, 숫자, 각도, 도형 구조를 **정확히 동일하게** 읽고 변환하세요.
- 추측하거나 변형하지 말고, 이미지에 보이는 내용을 **그대로** 정확히 전사하세요.

**⚠️⚠️⚠️ 절대 필수: 수학/교육 자료 분석 원칙 ⚠️⚠️⚠️**

**절대 금지 사항:**
- ❌ "A 37° x° D F 55° B E C" 같은 단순 텍스트 나열은 절대 금지!
- ❌ 텍스트만 읽어서 나열하는 것은 절대 금지!
- ❌ 도형의 구조를 분석하지 않고 텍스트만 나열하는 것은 절대 금지!

**필수 작업:**
1. **시각적 분석 필수**: 이미지를 직접 보고, 도형, 각도, 선, 점, 수식 등의 구조와 관계를 상세히 분석하세요.
2. **도형 구조 분석**: 삼각형, 사각형, 원 등 도형의 형태, 변의 길이, 각도, 점의 위치를 정확히 파악하세요.
3. **각도 및 측정값**: 각도(°), 길이, 거리 등 모든 측정값을 정확히 읽고, **어떤 점들로 이루어진 각도인지 명시**하세요.
   - 예: "∠BAD = 37°" (점 B-A-D로 이루어진 각도)
   - 예: "∠ADF = x°" (점 A-D-F로 이루어진 각도, 미지수)
4. **점과 선의 관계**: 점(A, B, C 등)의 위치, 선분의 연결 관계, 평행, 수직 등의 기하학적 관계를 명확히 기술하세요.
5. **수식 및 방정식**: 수식, 방정식, 부등식 등을 정확히 전사하고, 변수와 상수의 의미를 설명하세요.
6. **문제 문맥**: 문제의 조건, 구하는 것, 풀이 단계 등을 구조화하여 기술하세요.

**출력 형식 (반드시 이 형식으로 출력하세요):**
```markdown
# 수학/교육 자료 제목: [문제 제목 또는 주제]

## 자료 유형
[수학 문제 / 기하학 도형 / 각도 문제 / 수식 / 손글씨 문제 / 기타]

## 문제/내용 개요
[전체적인 문제 내용이나 자료의 목적]

## 도형 구조 (도형이 있는 경우)

### 점의 위치 및 명칭
- **점 A**: [위치 설명, 예: "삼각형의 왼쪽 꼭짓점"]
- **점 B**: [위치 설명]
- **점 C**: [위치 설명]
[모든 점을 나열]

### 선분 및 변
- **선분 AB**: [길이, 위치, 특징]
- **선분 BC**: [길이, 위치, 특징]
- **선분 AC**: [길이, 위치, 특징]
[모든 선분을 나열]

### 각도 정보
- **∠ABC** 또는 **각도 ABC**: [각도 값, 예: "37°"]
- **∠DEF** 또는 **각도 DEF**: [각도 값, 예: "x°" (미지수인 경우 명시)]
- **∠GHI** 또는 **각도 GHI**: [각도 값, 예: "55°"]
[모든 각도를 나열하고, 미지수인 경우 명시]

### 도형의 특징
- **도형 유형**: [삼각형, 사각형, 원 등]
- **변의 길이**: [각 변의 길이 정보]
- **각도 관계**: [평행, 수직, 합동, 닮음 등의 관계]
- **특수 조건**: [직각, 이등변, 정삼각형 등의 특수 조건]

## 수식 및 방정식 (수식이 있는 경우)
- **수식 1**: [수식을 정확히 전사]
  - 변수: [변수의 의미]
  - 상수: [상수의 값]
- **수식 2**: [수식을 정확히 전사]
[모든 수식을 나열]

## 문제 조건
- [조건 1]: [정확한 조건 설명]
- [조건 2]: [정확한 조건 설명]
[모든 조건을 나열]

## 구하는 것
[문제에서 구하라고 하는 것, 예: "x의 값", "각도 ABC의 크기" 등]

## 풀이 단계 (가능한 경우)
1. [단계 1 설명]
2. [단계 2 설명]
[풀이 과정을 구조화하여 기술]

## 텍스트 내용 (손글씨 또는 텍스트가 있는 경우)
[이미지에 보이는 모든 텍스트를 정확히 전사]
- **제목/문제 번호**: [텍스트]
- **문제 본문**: [텍스트]
- **조건/제약**: [텍스트]
- **기타 텍스트**: [모든 텍스트]

## 시각적 요소
- **도형의 배치**: [도형들이 어떻게 배치되어 있는지]
- **색상/강조**: [특정 부분이 강조되어 있는지]
- **화살표/표시**: [화살표나 특별한 표시가 있는지]

## 요약
[수학 문제나 교육 자료의 핵심 내용과 구조]
```

**중요**: 
- 단순히 텍스트만 나열하지 말고, 도형의 구조, 각도, 점과 선의 관계를 상세히 분석하세요.
- 각도, 길이, 거리 등 모든 측정값을 정확히 읽고, 어떤 요소에 해당하는지 명시하세요.
- 미지수(x, y 등)가 있으면 명확히 표시하세요.
- 기하학적 관계(평행, 수직, 합동 등)를 명확히 기술하세요.

**예시:**
이미지에 "A 37° x° D F 55° B E C" 같은 텍스트가 보여도, 절대 그대로 나열하지 마세요!
대신 다음과 같이 구조화하여 출력하세요:

```markdown
# 수학/교육 자료 제목: 각도 문제

## 자료 유형
기하학 각도 문제

## 문제/내용 개요
삼각형 또는 다각형에서 각도를 구하는 문제입니다.

## 도형 구조

### 점의 위치 및 명칭
- **점 A**: [위치 설명]
- **점 B**: [위치 설명]
- **점 C**: [위치 설명]
- **점 D**: [위치 설명]
- **점 E**: [위치 설명]
- **점 F**: [위치 설명]

### 각도 정보
- **∠BAD**: 37°
- **∠ADF**: x° (미지수, 구하는 값)
- **∠BEC**: 55°
```

**⚠️⚠️⚠️ 반드시 위 형식으로 구조화하여 출력하세요! 단순 텍스트 나열은 절대 금지! ⚠️⚠️⚠️**

응답은 {language}로 작성하세요."""

IMAGE_DESCRIPTION_PROMPT = """당신은 이미지를 분석하여 RAG 시스템에서 검색 가능한 구조화된 형식으로 변환하는 전문가입니다.

**절대 필수: 일관성 보장**
- 동일한 이미지를 분석할 때마다 **정확히 동일한 결과**를 출력해야 합니다.
- 텍스트, 숫자, 구조를 **정확히 동일하게** 읽고 변환하세요.
- 추측하거나 변형하지 말고, 이미지에 보이는 내용을 **그대로** 정확히 전사하세요.

**절대 필수: 이미지 분석 및 출력 형식**

⚠️ **OCR이 아닙니다!** 단순히 텍스트만 나열하지 말고, 이미지의 구조, 관계, 의미를 상세히 분석하세요.

**⚠️⚠️⚠️ 매우 중요: 이미지 분석 전 확인 사항 ⚠️⚠️⚠️**

**이미지를 보기 전에 다음을 명심하세요:**
1. 이미지에 도형(삼각형, 사각형, 원 등)이 있는가?
2. 각도 표시(°)가 있는가?
3. 점이 알파벳(A, B, C, D, E, F 등)으로 표시되어 있는가?
4. "A 37° x° D F 55° B E C" 같은 패턴의 텍스트가 보이는가?

**위 중 하나라도 있으면 → 반드시 수학/교육 자료로 판단하고 구조화된 형식으로 출력하세요!**

**⚠️⚠️⚠️ 첫 번째 단계: 이미지 내용 분석 및 유형 판단 (반드시 먼저 수행!) ⚠️⚠️⚠️**

**이미지를 자세히 관찰하고 다음을 확인하세요:**

**1. 수학/교육 자료 판단 (가장 먼저 확인!):**
   - 도형(삼각형, 사각형, 원 등)이 그려져 있는가?
   - 각도 표시(°, degree)가 있는가?
   - 점이 알파벳(A, B, C, D, E, F 등)으로 표시되어 있는가?
   - 선분, 직선, 곡선이 그려져 있는가?
   - "A 37° x° D F 55° B E C" 같은 패턴의 텍스트가 보이는가?
   - 알파벳과 각도(°)가 함께 나타나는가?
   - 정사각형, 삼각형 등 기하학적 도형이 있는가?
   
   **위 중 하나라도 있으면 → 반드시 수학/교육 자료로 판단하고, 아래 "### 3. 수학/교육 자료인 경우" 형식으로 출력하세요!**
   
   **특히 주의:**
   - "A 37° x° D F 55° B E C" 같은 텍스트가 보이면 → 이것은 수학/교육 자료입니다!
   - 각도(°)와 알파벳 점이 함께 있으면 → 수학/교육 자료입니다!
   - 정사각형 ABCD, 삼각형 AEF 같은 도형이 있으면 → 수학/교육 자료입니다!

**2. 차트/그래프 판단:**
   - X축, Y축이 있는가?
   - 데이터 포인트, 막대, 선이 있는가?
   
**3. 다이어그램/구조도 판단:**
   - 박스, 화살표, 연결선이 있는가?
   
**4. 일반 이미지:**
   - 위에 해당하지 않으면 일반 이미지로 판단

**⚠️⚠️⚠️ 이미지 유형 판단 - 절대 필수! ⚠️⚠️⚠️**
**먼저 이미지를 자세히 관찰하고, 아래 특징을 확인하여 이미지 유형을 정확히 판단하세요.**

**1단계: 이미지 관찰 및 유형 판단 (수학/교육 자료 우선 확인!)**

**⚠️⚠️⚠️ 수학/교육 자료 판단을 가장 먼저 수행하세요! ⚠️⚠️⚠️**

이미지를 보고 다음을 확인하세요:
- **도형**이 있는가? (삼각형, 사각형, 원, 다각형 등)
- **각도 표시**(°, degree)가 있는가?
- **점이 알파벳**(A, B, C, D, E, F 등)으로 표시되어 있는가?
- **선분, 직선, 곡선**이 그려져 있는가?
- **수식, 방정식**이 있는가?
- **손글씨 문제**나 수학 문제 형식인가?
- **기하학적 도형**과 각도, 점이 함께 있는가?

**위 특징 중 하나라도 있으면 → 반드시 수학/교육 자료로 판단하고, 아래 "### 3. 수학/교육 자료인 경우" 형식으로 출력하세요!**

**특히 주의:**
- "A 37° x° D F 55° B E C" 같은 텍스트가 보이면 → 이것은 수학/교육 자료입니다!
- 각도(°)와 알파벳 점이 함께 있으면 → 수학/교육 자료입니다!
- 도형과 각도가 함께 있으면 → 수학/교육 자료입니다!

**2단계: 이미지 유형별 출력 형식 선택**

1. **차트/그래프**: 막대 그래프, 선 그래프, 파이 차트 등 데이터를 시각화한 차트
   - 특징: X축, Y축, 데이터 포인트, 범례 등이 있음
   
2. **다이어그램/구조도**: 조직도, 시스템 구조도, 네트워크 다이어그램 등
   - 특징: 박스, 화살표, 연결선으로 구조를 표현
   
3. **수학/교육 자료** ⚠️⚠️⚠️ **매우 중요!** ⚠️⚠️⚠️
   - **판단 기준**: 도형, 각도(°), 점(A/B/C 등), 선분, 수식 중 하나라도 있으면 수학/교육 자료입니다!
   - **절대 금지**: "A 37° x° D F 55° B E C" 같은 단순 텍스트 나열은 절대 금지!
   - **필수 작업**: 
     * 도형의 구조를 분석하세요 (삼각형인지, 사각형인지 등)
     * 모든 점의 위치와 명칭을 나열하세요
     * 모든 각도를 정확히 읽어서 어떤 점들로 이루어진 각도인지 명시하세요
     * 선분과 변의 관계를 설명하세요
     * 미지수(x, y 등)가 있으면 명확히 표시하세요
   
4. **흐름도/플로우차트**: 프로세스 흐름을 나타내는 다이어그램 (별도 프롬프트 사용)
   
5. **일반 이미지**: 사진, 스크린샷, 문서 등

---

## 이미지 유형별 출력 형식

### 1. 차트/그래프인 경우 (막대 그래프, 선 그래프, 파이 차트, 산점도 등)

**⚠️ 차트 분석 정확성 - 절대 필수!**
- **시각적 분석 필수**: 차트 이미지를 직접 보고, 각 데이터 포인트의 위치를 정확히 파악하세요.
- **X축-Y축 정확한 매칭**: 각 X축 레이블(연도, 카테고리 등)에 대응하는 Y축 값을 정확히 읽으세요. 추측하지 마세요.
- **단위 반영 필수**: 차트 제목, 축 레이블, 범례 등에 표기된 단위(예: "단위: 천명", "%", "억원" 등)를 반드시 확인하고 반영하세요.
- **Y축 눈금선 확인**: Y축의 눈금선을 확인하여 각 데이터 포인트의 정확한 값을 읽으세요.
- **모든 데이터 포함**: 차트의 모든 데이터 포인트를 빠짐없이 포함하세요.

**출력 형식:**
```markdown
# 차트 제목: [차트의 제목]

## 차트 유형
[막대 그래프 / 선 그래프 / 파이 차트 / 산점도 / 복합 차트 등]

## 축 정보
- **X축(가로축)**: [축 이름], 단위: [단위], 범위: [최소값 ~ 최대값]
- **Y축(세로축)**: [축 이름], 단위: [단위], 범위: [최소값 ~ 최대값]
- **인덱스/레이블**: [모든 카테고리, 항목, 시간대 등을 순서대로 나열]

## 데이터 표 (가로/세로 축 기반 구조화) - **절대 필수!**
차트의 가로축(X축)과 세로축(Y축) 정보를 바탕으로 **표 형태로 정리**하세요.

**⚠️ 데이터 정확성 - 절대 필수!**
1. **시각적 매칭 필수**: 차트 이미지를 직접 보고, 각 X축 레이블(연도, 카테고리 등)에 대응하는 막대/점/선의 Y축 값을 **정확히** 읽으세요.
2. **X축-Y축 매칭 검증**: 
   - 막대 그래프: 각 막대의 위치를 X축 레이블과 정확히 매칭하세요.
   - 선 그래프: 각 데이터 점의 위치를 X축 레이블과 정확히 매칭하세요.
   - 파이 차트: 각 조각의 레이블과 값을 정확히 매칭하세요.
3. **단위 반영 필수**: 
   - 차트 제목, 축 레이블, 범례 등에 표기된 단위를 **반드시 확인**하세요.
   - 예: "단위: 천명", "단위: 억원", "%" 등이 표기되어 있으면 반드시 반영하세요.
   - 단위가 별도로 표기되어 있으면 표 헤더에 명시하거나 각 값에 포함하세요.
4. **데이터 값 검증**: 
   - Y축 눈금선을 확인하여 각 데이터 포인트의 정확한 값을 읽으세요.
   - 추측하거나 근사치를 사용하지 마세요. 정확한 값을 읽으세요.
   - 모든 데이터 포인트를 빠짐없이 포함하세요.

**표 형식:**
- **단일 시리즈 차트** (막대 그래프, 선 그래프 등):
  | [X축 레이블/카테고리] | [Y축 값] [단위] |
  |---------------------|----------------|
  | [인덱스 1] | [정확한 값 1] |
  | [인덱스 2] | [정확한 값 2] |
  | ... | ... |

- **다중 시리즈 차트** (여러 데이터 세트):
  | [X축 레이블/카테고리] | [시리즈 1 이름] [단위] | [시리즈 2 이름] [단위] | ... |
  |---------------------|---------------------|---------------------|-----|
  | [인덱스 1] | [정확한 값 1-1] | [정확한 값 1-2] | ... |
  | [인덱스 2] | [정확한 값 2-1] | [정확한 값 2-2] | ... |
  | ... | ... | ... | ... |

**작업 순서 (반드시 이 순서로 수행):**
1. X축 레이블을 왼쪽부터 오른쪽으로 순서대로 모두 나열
2. 각 X축 레이블에 대응하는 막대/점/선을 시각적으로 확인
3. 해당 막대/점/선의 Y축 값을 Y축 눈금선을 기준으로 정확히 읽기
4. 단위 확인 (제목, 축 레이블, 범례 등)
5. 표에 X축 레이블과 Y축 값을 정확히 매칭하여 기록

## 데이터 상세 (인덱스별) - 표와 함께 제공
- **[인덱스/레이블 1]**: [정확한 데이터 값] [단위]
  - 추가 정보: [색상, 패턴, 주석 등]
- **[인덱스/레이블 2]**: [정확한 데이터 값] [단위]
  - 추가 정보: [색상, 패턴, 주석 등]
[모든 데이터 포인트를 빠짐없이 나열]

## 시리즈 정보 (여러 데이터 세트가 있는 경우)
- **시리즈 1 ([이름])**: [설명]
  - [인덱스 1]: [값]
  - [인덱스 2]: [값]
  - ...
- **시리즈 2 ([이름])**: [설명]
  - [인덱스 1]: [값]
  - [인덱스 2]: [값]
  - ...

## 트렌드 및 패턴
- [데이터에서 관찰되는 주요 트렌드, 패턴, 특이점 설명]
- [최고값/최저값, 증가/감소 추세, 비교 등]

## 범례 및 주석
- [범례 항목들과 그 의미]
- [차트 내 모든 주석, 참고사항, 출처 등]

## 요약
[차트가 전달하는 핵심 메시지와 주요 인사이트]
```

**중요**: 차트의 경우 **모든 인덱스(레이블, 카테고리, 시간대 등)와 각 인덱스별 정확한 데이터 값**을 반드시 포함하세요.
- **X축-Y축 정확한 매칭 필수**: 각 X축 레이블에 대응하는 Y축 값을 시각적으로 정확히 읽어서 매칭하세요. 추측하거나 근사치를 사용하지 마세요.
- **단위 반영 필수**: 차트 제목, 축 레이블, 범례 등에 표기된 단위(예: "단위: 천명", "%", "억원" 등)를 반드시 확인하고 표에 반영하세요.
- **Y축 눈금선 확인**: Y축의 눈금선을 확인하여 각 데이터 포인트의 정확한 값을 읽으세요.

---

### 2. 다이어그램/구조도인 경우 (조직도, 시스템 구조도, 네트워크 다이어그램 등)

**출력 형식:**
```markdown
# 다이어그램 제목: [다이어그램의 제목 또는 주제]

## 개요
[다이어그램의 전체적인 목적과 내용]

## 구조 상세

### [구성 요소 1 이름]
- **유형**: [박스, 원, 아이콘 등]
- **위치**: [상단, 중앙, 하단 등]
- **내용**: [구성 요소 내부의 텍스트나 설명]
- **연결**: [다른 어떤 구성 요소와 연결되는지]
  - → [구성 요소 2]: [연결 관계 설명]
  - → [구성 요소 3]: [연결 관계 설명]

### [구성 요소 2 이름]
- **유형**: [유형]
- **위치**: [위치]
- **내용**: [내용]
- **연결**: [연결 관계]

[모든 구성 요소를 상세히 나열]

## 관계 및 흐름
- [구성 요소 A] → [구성 요소 B]: [관계 설명]
- [구성 요소 B] → [구성 요소 C]: [관계 설명]
[모든 연결 관계를 명시]

## 계층 구조 (조직도인 경우)
```
최상위
  ├─ 레벨 1-1
  │   ├─ 레벨 2-1
  │   └─ 레벨 2-2
  └─ 레벨 1-2
      └─ 레벨 2-3
```

## 부가 설명
[다이어그램에 포함된 모든 주석, 참고사항, 범례]

## 요약
[다이어그램의 핵심 내용과 구조]
```

---

### 3. 수학/교육 자료인 경우 (수학 문제, 도형, 각도, 기하학, 수식, 손글씨 등)

**⚠️⚠️⚠️ 수학/교육 자료 분석 정확성 - 절대 필수! ⚠️⚠️⚠️**

**절대 금지 사항:**
- ❌ "A 37° x° D F 55° B E C" 같은 단순 텍스트 나열은 절대 금지!
- ❌ 텍스트만 읽어서 나열하는 것은 절대 금지!
- ❌ 도형의 구조를 분석하지 않고 텍스트만 나열하는 것은 절대 금지!

**필수 작업:**
1. **시각적 분석 필수**: 이미지를 직접 보고, 도형, 각도, 선, 점, 수식 등의 구조와 관계를 상세히 분석하세요.
2. **도형 구조 분석**: 삼각형, 사각형, 원 등 도형의 형태, 변의 길이, 각도, 점의 위치를 정확히 파악하세요.
3. **각도 및 측정값**: 각도(°), 길이, 거리 등 모든 측정값을 정확히 읽고, **어떤 점들로 이루어진 각도인지 명시**하세요.
   - 예: "∠BAD = 37°" (점 B-A-D로 이루어진 각도)
   - 예: "∠ADF = x°" (점 A-D-F로 이루어진 각도, 미지수)
4. **점과 선의 관계**: 점(A, B, C 등)의 위치, 선분의 연결 관계, 평행, 수직 등의 기하학적 관계를 명확히 기술하세요.
5. **수식 및 방정식**: 수식, 방정식, 부등식 등을 정확히 전사하고, 변수와 상수의 의미를 설명하세요.
6. **문제 문맥**: 문제의 조건, 구하는 것, 풀이 단계 등을 구조화하여 기술하세요.

**판단 기준:**
이미지에 다음 중 하나라도 있으면 수학/교육 자료입니다:
- 도형(삼각형, 사각형, 원 등)
- 각도 표시(°, degree)
- 점이 알파벳으로 표시됨(A, B, C 등)
- 선분, 직선, 곡선
- 수식, 방정식
- 손글씨 문제나 수학 문제 형식

**출력 형식:**
```markdown
# 수학/교육 자료 제목: [문제 제목 또는 주제]

## 자료 유형
[수학 문제 / 기하학 도형 / 각도 문제 / 수식 / 손글씨 문제 / 기타]

## 문제/내용 개요
[전체적인 문제 내용이나 자료의 목적]

## 도형 구조 (도형이 있는 경우)

### 점의 위치 및 명칭
- **점 A**: [위치 설명, 예: "삼각형의 왼쪽 꼭짓점"]
- **점 B**: [위치 설명]
- **점 C**: [위치 설명]
[모든 점을 나열]

### 선분 및 변
- **선분 AB**: [길이, 위치, 특징]
- **선분 BC**: [길이, 위치, 특징]
- **선분 AC**: [길이, 위치, 특징]
[모든 선분을 나열]

### 각도 정보
- **∠ABC** 또는 **각도 ABC**: [각도 값, 예: "37°"]
- **∠DEF** 또는 **각도 DEF**: [각도 값, 예: "x°" (미지수인 경우 명시)]
- **∠GHI** 또는 **각도 GHI**: [각도 값, 예: "55°"]
[모든 각도를 나열하고, 미지수인 경우 명시]

### 도형의 특징
- **도형 유형**: [삼각형, 사각형, 원 등]
- **변의 길이**: [각 변의 길이 정보]
- **각도 관계**: [평행, 수직, 합동, 닮음 등의 관계]
- **특수 조건**: [직각, 이등변, 정삼각형 등의 특수 조건]

## 수식 및 방정식 (수식이 있는 경우)
- **수식 1**: [수식을 정확히 전사]
  - 변수: [변수의 의미]
  - 상수: [상수의 값]
- **수식 2**: [수식을 정확히 전사]
[모든 수식을 나열]

## 문제 조건
- [조건 1]: [정확한 조건 설명]
- [조건 2]: [정확한 조건 설명]
[모든 조건을 나열]

## 구하는 것
[문제에서 구하라고 하는 것, 예: "x의 값", "각도 ABC의 크기" 등]

## 풀이 단계 (가능한 경우)
1. [단계 1 설명]
2. [단계 2 설명]
[풀이 과정을 구조화하여 기술]

## 텍스트 내용 (손글씨 또는 텍스트가 있는 경우)
[이미지에 보이는 모든 텍스트를 정확히 전사]
- **제목/문제 번호**: [텍스트]
- **문제 본문**: [텍스트]
- **조건/제약**: [텍스트]
- **기타 텍스트**: [모든 텍스트]

## 시각적 요소
- **도형의 배치**: [도형들이 어떻게 배치되어 있는지]
- **색상/강조**: [특정 부분이 강조되어 있는지]
- **화살표/표시**: [화살표나 특별한 표시가 있는지]

## 요약
[수학 문제나 교육 자료의 핵심 내용과 구조]
```

**중요**: 
- 단순히 텍스트만 나열하지 말고, 도형의 구조, 각도, 점과 선의 관계를 상세히 분석하세요.
- 각도, 길이, 거리 등 모든 측정값을 정확히 읽고, 어떤 요소에 해당하는지 명시하세요.
- 미지수(x, y 등)가 있으면 명확히 표시하세요.
- 기하학적 관계(평행, 수직, 합동 등)를 명확히 기술하세요.

**⚠️⚠️⚠️ 구체적인 예시 (반드시 참고하세요!):**
만약 이미지에 정사각형 ABCD와 삼각형 AEF가 있고, 각도가 표시되어 있다면:

**❌ 잘못된 출력 (절대 금지!):**
```
A 37° x° D F 55° B E C
```

**올바른 출력 (반드시 이렇게!):**
```markdown
# 수학/교육 자료 제목: 각도 문제

## 자료 유형
기하학 각도 문제

## 문제/내용 개요
정사각형 ABCD 내부에 삼각형 AEF가 그려진 기하학 문제입니다.

## 도형 구조

### 점의 위치 및 명칭
- **점 A**: 정사각형의 왼쪽 상단 꼭짓점이자 삼각형의 한 꼭짓점
- **점 B**: 정사각형의 왼쪽 하단 꼭짓점
- **점 C**: 정사각형의 오른쪽 하단 꼭짓점
- **점 D**: 정사각형의 오른쪽 상단 꼭짓점
- **점 E**: 정사각형의 변 BC 위에 있는 점이자 삼각형의 한 꼭짓점
- **점 F**: 정사각형의 변 CD 위에 있는 점이자 삼각형의 한 꼭짓점

### 선분 및 변
- **선분 AB**: 정사각형의 왼쪽 변
- **선분 BC**: 정사각형의 하단 변 (점 E가 이 변 위에 있음)
- **선분 CD**: 정사각형의 오른쪽 변 (점 F가 이 변 위에 있음)
- **선분 DA**: 정사각형의 상단 변
- **선분 AE**: 삼각형 AEF의 한 변
- **선분 AF**: 삼각형 AEF의 한 변
- **선분 EF**: 삼각형 AEF의 한 변

### 각도 정보
- **∠BAE**: 37° (점 B-A-E로 이루어진 각도)
- **∠DAF**: x° (점 D-A-F로 이루어진 각도, 미지수)
- **∠AEC**: 55° (점 A-E-C로 이루어진 각도)

### 도형의 특징
- **도형 유형**: 정사각형 ABCD와 내부 삼각형 AEF
- **변의 길이**: 정사각형의 모든 변은 같음
- **각도 관계**: 정사각형의 모든 내각은 90°
- **특수 조건**: 삼각형 AEF는 정사각형 내부에 그려짐
```

**⚠️⚠️⚠️ 반드시 위와 같은 구조화된 형식으로 출력하세요! 단순 텍스트 나열은 절대 금지! ⚠️⚠️⚠️**

---

### 4. 일반 이미지인 경우 (사진, 스크린샷, 문서 이미지 등)

**출력 형식:**
```markdown
# 이미지 제목: [이미지의 제목 또는 주제]

## 이미지 유형
[사진 / 스크린샷 / 문서 / 인포그래픽 / 기타]

## 주요 내용

### 텍스트 내용
[이미지에 보이는 모든 텍스트를 순서대로 정확히 전사]
- **제목**: [제목 텍스트]
- **본문**: [본문 텍스트]
- **라벨/캡션**: [모든 라벨과 캡션]
- **기타 텍스트**: [그 외 모든 텍스트]

### 시각적 요소
- **주요 객체**: [이미지에서 중심이 되는 객체나 요소]
- **색상**: [주요 색상과 그 위치]
- **레이아웃**: [요소들의 배치와 구조]
- **아이콘/심볼**: [모든 아이콘과 심볼의 의미]

### 상세 설명
[이미지의 각 부분을 왼쪽에서 오른쪽, 위에서 아래 순서로 상세히 설명]
1. [영역 1 설명]
2. [영역 2 설명]
3. [영역 3 설명]
[계속...]

## 숫자 및 수치 정보
[이미지에 포함된 모든 숫자, 금액, 퍼센트, 날짜 등을 정확히 기록]
- [항목 1]: [정확한 수치]
- [항목 2]: [정확한 수치]

## 맥락 및 해석
[이미지가 전달하는 메시지, 목적, 의미]

## 부가 정보
[출처, 날짜, 저작권, 워터마크 등 모든 메타 정보]
```

---

### 4. **조직도/계층 구조인 경우 (매우 중요!)**:
     * 모든 조직 단위, 부서, 팀, 역할을 정확히 나열
     * 상하 관계를 명확히 표시 (예: "농지관리처장 → 농지조사부장 → 전담반")
     * 각 조직 단위의 담당 업무나 역할을 상세히 기술
     * 화살표(→), 선, 박스 등의 시각적 요소가 나타내는 관계를 명확히 설명
     * 계층 레벨을 들여쓰기나 번호로 구분하여 표현
   - **흐름도/플로우차트인 경우 (절대 필수! - RAG 질문 답변을 위해 매우 중요!)**:
     * **모든 가능한 경로(Path)를 완전히 나열하세요.**
     * **순방향 경로**: 시작점부터 각 종료점까지의 모든 정상 흐름 경로를 순서대로 기술
     * **역방향 경로**: 되돌아가는 화살표나 역방향 흐름이 있으면 반드시 포함
     * **반복(Loop) 경로**: 순환 구조가 있으면 시작점, 반복 조건, 종료 조건을 명확히 기술
     * **조건 분기**: 모든 조건문(if, switch 등)과 각 분기의 조건, True/False 경로를 모두 기술
     * **각 경로의 상세 설명**: 각 단계에서 수행되는 작업, 입력/출력, 상태 변화를 상세히 기술
     * **부가 설명**: 각 단계의 주석, 참고사항, 예외 처리, 에러 경로 등을 모두 포함
     * **경로 번호 매기기**: 각 경로를 명확히 구분할 수 있도록 번호나 이름을 부여
     * **조건별 시나리오**: "조건 A가 True일 때 → 경로 1", "조건 A가 False일 때 → 경로 2" 형식으로 기술
     * **예외 처리**: 에러 발생 시 경로, 재시도 경로, 종료 경로 등을 모두 포함
     * **RAG 질문 대응**: "조건 X일 때 어떻게 되나요?" 같은 질문에 답할 수 있도록 모든 조건과 경로를 명시
4. **학생 친화적**: 전문 용어를 사용할 때는 간단한 설명을 함께 제공하세요.
5. **수치 데이터 보존 필수**: 
   - 이미지의 모든 숫자, 수치, 퍼센트, 금액, 날짜 등을 정확히 그대로 기술
   - 수치를 반올림하거나 생략하지 않음
   - 소수점, 단위 등을 원본과 동일하게 보존

**⚠️⚠️⚠️ 최종 확인 사항 - 절대 필수! ⚠️⚠️⚠️**

**작업 순서 (수학/교육 자료 우선 확인!):**
1. **이미지를 자세히 관찰하세요**
2. **수학/교육 자료 판단을 가장 먼저 수행하세요:**
   - 도형, 각도(°), 점(A/B/C 등), 선분, 수식 중 하나라도 있으면 → **반드시 수학/교육 자료로 판단**
   - "A 37° x° D F 55° B E C" 같은 텍스트가 보이면 → **수학/교육 자료입니다!**
   - 각도(°)와 알파벳 점이 함께 있으면 → **수학/교육 자료입니다!**
3. **수학/교육 자료인 경우:**
   - ❌ 절대 "A 37° x° D F 55° B E C" 같은 단순 텍스트 나열 금지!
   - 반드시 "### 3. 수학/교육 자료인 경우" 형식으로 출력
   - 도형 구조, 점의 위치, 각도 정보를 구조화하여 출력
   - 각 각도가 어떤 점들로 이루어진지 명시 (예: ∠BAD = 37°)
4. 다른 유형인 경우 해당 유형의 출력 형식 사용

**수학/교육 자료 판단 체크리스트 (하나라도 체크되면 수학/교육 자료!):**
- [ ] 도형이 있는가? (삼각형, 사각형, 원 등)
- [ ] 각도 표시(°)가 있는가?
- [ ] 점이 알파벳으로 표시되어 있는가? (A, B, C, D, E, F 등)
- [ ] 선분, 직선, 곡선이 그려져 있는가?
- [ ] 수식, 방정식이 있는가?
- [ ] "A 37° x° D F 55° B E C" 같은 패턴의 텍스트가 있는가?

**⚠️⚠️⚠️ 위 체크리스트 중 하나라도 체크되면 → 반드시 수학/교육 자료로 판단하고 구조화된 형식으로 출력하세요! ⚠️⚠️⚠️**

**특히 주의할 패턴:**
- 알파벳 + 각도(°) + 알파벳 조합 → 수학/교육 자료
- 예: "A 37°", "x°", "55° B" → 수학/교육 자료
- 예: "A 37° x° D F 55° B E C" → 수학/교육 자료 (단순 텍스트 나열 금지!)

## 출력 예시

### 차트 예시:
```markdown
# 차트 제목: 2020-2024년 분기별 매출 추이

## 차트 유형
선 그래프

## 축 정보
- **X축(가로축)**: 분기, 단위: 분기(Q1~Q4), 범위: 2020 Q1 ~ 2024 Q4
- **Y축(세로축)**: 매출, 단위: 억원 (차트 제목 또는 축 레이블에 명시), 범위: 0 ~ 500
- **인덱스/레이블**: 2020-Q1, 2020-Q2, 2020-Q3, 2020-Q4, 2021-Q1, 2021-Q2, ...

## 데이터 표 (가로/세로 축 기반 구조화)
**⚠️ 각 분기(X축)에 대응하는 매출 값(Y축)을 차트에서 정확히 읽어서 매칭하세요.**

| 분기 | 매출 (억원) |
|------|------------|
| 2020-Q1 | 250 |
| 2020-Q2 | 280 |
| 2020-Q3 | 310 |
| 2020-Q4 | 350 |
| 2021-Q1 | 380 |
| 2021-Q2 | 320 |
| ... | ... |

**참고**: 위 표는 예시입니다. 실제 차트를 분석할 때는:
1. X축 레이블을 왼쪽부터 오른쪽으로 순서대로 확인
2. 각 X축 레이블에 대응하는 막대/점/선의 위치를 시각적으로 확인
3. 해당 막대/점/선의 Y축 값을 Y축 눈금선을 기준으로 정확히 읽기
4. 단위 확인 (제목, 축 레이블 등에 표기된 단위 반영)
5. 표에 정확히 매칭하여 기록

## 데이터 상세 (인덱스별)
- **2020-Q1**: 250억원
- **2020-Q2**: 280억원
- **2020-Q3**: 310억원
- **2020-Q4**: 350억원
[모든 분기 데이터 계속...]

## 트렌드 및 패턴
- 전반적으로 증가 추세
- 2021년 Q2에 일시적 하락
- 2024년 Q4에 최고값(480억원) 기록

## 범례 및 주석
- 출처: 재무팀 2024년 보고서
```

### 수학/교육 자료 예시:
```markdown
# 수학/교육 자료 제목: 각도 문제

## 자료 유형
기하학 각도 문제

## 문제/내용 개요
삼각형 또는 다각형에서 각도를 구하는 문제입니다.

## 도형 구조

### 점의 위치 및 명칭
- **점 A**: 삼각형의 왼쪽 상단 꼭짓점
- **점 B**: 삼각형의 오른쪽 상단 꼭짓점
- **점 C**: 삼각형의 하단 꼭짓점
- **점 D**: 선분 위의 점 (위치 명시)
- **점 E**: 선분 위의 점 (위치 명시)
- **점 F**: 선분 위의 점 (위치 명시)

### 선분 및 변
- **선분 AB**: [길이 정보, 위치]
- **선분 BC**: [길이 정보, 위치]
- **선분 AC**: [길이 정보, 위치]
- **선분 AD**: [길이 정보, 위치]
- **선분 DF**: [길이 정보, 위치]
- **선분 BE**: [길이 정보, 위치]
- **선분 EC**: [길이 정보, 위치]

### 각도 정보
- **∠BAD** 또는 **각도 BAD**: 37°
- **∠ADF** 또는 **각도 ADF**: x° (미지수, 구하는 값)
- **∠BEC** 또는 **각도 BEC**: 55°
- **∠ABC**: [각도 값]
- **∠BCA**: [각도 값]
- **∠CAB**: [각도 값]

### 도형의 특징
- **도형 유형**: 삼각형 (또는 다각형)
- **변의 길이**: [각 변의 길이 정보]
- **각도 관계**: [평행, 수직 등의 관계]
- **특수 조건**: [직각, 이등변 등의 조건]

## 문제 조건
- 각도 BAD = 37°
- 각도 BEC = 55°
- 점 D, E, F의 위치 관계

## 구하는 것
각도 ADF (x°)의 값

## 풀이 단계 (가능한 경우)
1. 삼각형의 내각의 합을 이용
2. 평행선과 각도 관계를 이용
3. x의 값을 계산

## 텍스트 내용
- **문제 본문**: "각도 x를 구하시오" 또는 유사한 문제 문구
- **조건/제약**: 각도 정보 (37°, x°, 55°)

## 시각적 요소
- **도형의 배치**: 삼각형이 어떻게 그려져 있는지
- **각도 표시**: 각도가 어떻게 표시되어 있는지 (호, 숫자 등)
- **점의 위치**: 각 점이 도형의 어느 위치에 있는지

## 요약
삼각형에서 주어진 각도 정보를 바탕으로 미지의 각도 x를 구하는 기하학 문제입니다.
```

### 다이어그램 예시:
```
[흐름도 제목/주제]

[시작점]
- 시작: [시작 단계 설명]

[경로 1: 정상 흐름]
1. [단계 1 이름]
   - 작업: [상세 작업 내용]
   - 입력: [입력 데이터/조건]
   - 출력: [출력 결과]
   - 다음: [다음 단계로 이동 조건]

2. [단계 2 이름]
   - 작업: [상세 작업 내용]
   - 조건: [조건 설명]
   - 조건 True → [다음 단계]
   - 조건 False → [다른 경로로 분기]

[경로 2: 조건 분기 - 조건 A가 True인 경우]
1. [단계 1] → [단계 2] → [단계 3] → [종료점 A]
   - 각 단계의 상세 설명

[경로 3: 조건 분기 - 조건 A가 False인 경우]
1. [단계 1] → [단계 4] → [단계 5] → [종료점 B]
   - 각 단계의 상세 설명

[경로 4: 반복 구조]
- 반복 시작: [반복 시작 조건]
- 반복 내용: [반복되는 단계들]
- 반복 조건: [반복 계속 조건]
- 반복 종료: [반복 종료 조건 및 다음 단계]

[경로 5: 역방향 흐름]
- [단계 X] → [이전 단계로 되돌아감] → [단계 Y]
- 역방향 조건: [되돌아가는 조건]

[경로 6: 예외 처리]
- 에러 발생 시: [에러 처리 단계] → [에러 복구 또는 종료]
- 재시도: [재시도 조건 및 경로]

[부가 설명]
- [단계 N] 참고사항: [주석이나 참고 내용]
- [조건 M] 예외: [예외 상황 설명]

[모든 종료점]
- 종료점 A: [종료 조건 및 결과]
- 종료점 B: [종료 조건 및 결과]
- 종료점 C: [종료 조건 및 결과]
```

**중요**: 흐름도 분석 시 반드시 다음을 포함하세요:
1. **모든 가능한 경로를 빠짐없이 나열** (순방향, 역방향, 반복, 예외 경로 포함)
2. **각 조건 분기의 모든 경우의 수를 명시** (True/False, 각 케이스별 경로)
3. **각 단계의 상세한 작업 내용과 입력/출력**
4. **반복 구조의 시작, 조건, 종료를 명확히 기술**
5. **역방향 흐름이 있으면 반드시 포함**
6. **예외 처리, 에러 경로, 재시도 로직을 모두 포함**
7. **부가 설명, 주석, 참고사항을 모두 포함**

이렇게 하면 RAG 시스템에서 "조건 X일 때 어떻게 되나요?", "에러가 발생하면 어떻게 되나요?", "반복은 언제 종료되나요?" 같은 질문에 정확히 답할 수 있습니다.

응답은 {language}로 작성하세요."""


class VLMClient:
    def __init__(self, openai_model: str = "openrouter/openai/gpt-5.2", device: str = "gpu", use_claude: bool = False, claude_model: str = "claude-sonnet-4-5-20250929-v1:0", max_concurrent_api: int = 8, gpu_max_concurrent: int = 1):
        # 이미지 해시 기반 캐시 (같은 이미지에 대해 동일한 결과 반환, 병렬 처리 시 스레드 안전)
        self._image_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()
        # 동시 API 호출 제한 (파일 병렬 + 이미지 병렬 합산 시 rate limit 방지)
        self._api_semaphore = threading.Semaphore(max_concurrent_api)
        # 로컬 GPU 추론 동시성 설정 (Qwen 로컬 모델용)
        self._gpu_max_concurrent = gpu_max_concurrent

        # 모델 플래그 초기화
        self.use_qwen = False
        self.qwen_client = None
        self.use_openrouter = False
        self.use_claude = False
        self.claude_model = None

        # OpenRouter 지원 (Qwen3-VL 등 외부 클라우드 API)
        if openai_model.lower().startswith("openrouter/"):
            print(f"[INFO] OpenRouter 모드 활성화: {openai_model}")
            self.use_openrouter = True
            openrouter_api_key = env_str("OPENROUTER_API_KEY")
            if not openrouter_api_key:
                raise ValueError(
                    "OPENROUTER_API_KEY 환경변수가 필요합니다. "
                    ".env 파일 또는 환경변수에 OPENROUTER_API_KEY를 설정하세요."
                )

            # UI 모델명 → OpenRouter API 모델명 매핑
            openrouter_model_map = {
                "openrouter/gpt-5.2": "openai/gpt-5.2",
                "openrouter/openai/gpt-5.2": "openai/gpt-5.2",
                "openrouter/gpt-5-mini": "openai/gpt-5-mini",
                "openrouter/openai/gpt-5-mini": "openai/gpt-5-mini",
                "openrouter/qwen3-vl-8b": "qwen/qwen3-vl-8b-instruct",
                "openrouter/qwen3-vl-32b": "qwen/qwen3-vl-32b-instruct",
            }
            actual_model = openrouter_model_map.get(openai_model.lower())
            if actual_model is None:
                actual_model = openai_model[len("openrouter/"):]
            print(f"[INFO] OpenRouter 모델 매핑: {openai_model} → {actual_model}")
            # OpenRouter Qwen은 시각적 자기검증 패스가 느리고 과수정을 유발할 수 있어 기본 OFF.
            # 필요 시 OPENROUTER_VISUAL_VERIFY=1 로 강제 활성화 가능.
            self.openrouter_visual_verify = env_bool("OPENROUTER_VISUAL_VERIFY", False)
            if not self.openrouter_visual_verify and "qwen/" in actual_model.lower():
                print("[INFO] OpenRouter Qwen 시각적 자기검증 패스: OFF (기본값)")

            self.service = VLMChartService(
                use_openai=True,
                use_paddleocr=False,
                openai_api_key=openrouter_api_key,
                openai_model=actual_model,
                openai_base_url="https://openrouter.ai/api/v1",
                device=device,
            )
            # 조기 리턴 (로컬 모델 초기화 불필요)
            return

        # Qwen2.5-VL 지원 (폐쇄망 로컬 모델 — H100 GPU)
        if openai_model.lower().startswith("qwen2.5-vl"):
            print("[INFO] Qwen2.5-VL 로컬 모드 활성화")
            self.use_qwen = True
            qwen_model_paths = {
                "qwen2.5-vl-7b": env_str(
                    "QWEN_VL_7B_MODEL_PATH",
                    "../models/Qwen2.5-VL-7B-Instruct",
                ),
                "qwen2.5-vl-32b": env_str(
                    "QWEN_VL_32B_MODEL_PATH",
                    "../models/Qwen2.5-VL-32B-Instruct",
                ),
            }
            self.qwen_model_path = qwen_model_paths.get(
                openai_model.lower(),
                env_str(
                    "QWEN_VL_MODEL_PATH",
                    "../models/Qwen2.5-VL-7B-Instruct",
                ),
            )
            self.qwen_device = device
            # 지연 로딩: 실제 사용 시점에 초기화
            self.qwen_client = None
            print(f"[INFO] Qwen2.5-VL 지연 로딩 모드 (모델 경로: {self.qwen_model_path})")
            # 조기 리턴 (OpenAI/Claude 초기화 불필요)
            return

        """
        Args:
            openai_model: OpenAI 모델명 (기본값: "gpt-5-mini" - 성능 검증용)
                          - gpt-5-mini: 최신 경량 멀티모달 모델 (성능 검증)
                          - gpt-4o: 최신 멀티모달 모델 (이미지 분석 최적화)
                          - gpt-4o-mini: 경량 버전 (비용 절감용)
                          - gpt-4-turbo: 이전 버전
            device: 디바이스 (gpu/cpu)
            use_claude: Claude 모델 사용 여부
            claude_model: Claude 모델명 (AWS Bedrock 모델 ID 형식)
        """
        """
        Args:
            openai_model: OpenAI 모델명
            device: 디바이스 (gpu/cpu)
            use_claude: Claude 모델 사용 여부
            claude_model: Claude 모델명 (AWS Bedrock 모델 ID 형식)
        """
        self.use_claude = use_claude
        self.claude_model = claude_model
        self.service = VLMChartService(
            use_openai=not use_claude,
            use_paddleocr=False,
            openai_api_key=None,
            openai_model=openai_model,
            device=device,
        )
        
        # Claude 클라이언트 초기화 (필요시)
        if use_claude:
            if ANTHROPIC_AVAILABLE:
                api_key = env_str("ANTHROPIC_API_KEY")
                if api_key:
                    self.claude_client = Anthropic(api_key=api_key)
                else:
                    print("[WARNING] ANTHROPIC_API_KEY가 설정되지 않았습니다. OpenAI를 사용합니다.")
                    self.use_claude = False
            elif BEDROCK_AVAILABLE:
                # AWS Bedrock 사용
                try:
                    self.bedrock_client = boto3.client('bedrock-runtime', region_name='us-east-1')
                    print("[INFO] AWS Bedrock 클라이언트 초기화 완료")
                except Exception as e:
                    print(f"[WARNING] AWS Bedrock 초기화 실패: {e}. OpenAI를 사용합니다.")
                    self.use_claude = False
            else:
                print("[WARNING] Claude를 사용하려면 anthropic 또는 boto3 패키지가 필요합니다. OpenAI를 사용합니다.")
                self.use_claude = False

    def _clean_openrouter_response(self, text: str) -> str:
        """OpenRouter/Qwen 모델 응답의 마크다운 포맷과 분석 텍스트를 정리합니다.

        Qwen 모델의 대표적 출력 문제:
        1) 마크다운 헤더(### ...)와 코드 블록(```)으로 래핑
        2) 같은 내용을 여러 섹션에 중복 출력
        3) 자기검증 시 분석/추론 텍스트를 HTML 앞뒤에 추가

        이를 정리하여 순수 HTML + ===TABLE_MARKDOWN=== + ===TEXT_CONTENT=== 형식만 추출합니다.
        GPT 모델에는 적용되지 않습니다 (use_openrouter 체크).
        """
        if not text:
            return text

        # "### 최종 출력" 섹션이 있으면 그 부분만 사용 (중복 출력 제거)
        if "### 최종 출력" in text:
            text = text.split("### 최종 출력", 1)[-1].strip()

        # 마크다운 코드 블록 마커 제거 (```html, ```markdown, ```none, ```)
        text = re.sub(r'^```(?:html|markdown|none)\s*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'^```\s*$', '', text, flags=re.MULTILINE)

        # 마크다운 헤더 제거 (### HTML 표 출력, ### Markdown 선형화 등)
        text = re.sub(r'^###\s+.*$', '', text, flags=re.MULTILINE)

        # 구조적 콘텐츠 추출: <table>...</table>이 있으면 분석 텍스트 제거
        # Qwen 자기검증 시 "1. TOTAL_COLS...", "이제 HTML과 이미지가..." 같은 분석 텍스트 제거
        table_match = re.search(r'(<table[^>]*>.*?</table>)', text, re.DOTALL | re.IGNORECASE)
        if table_match:
            table_html = table_match.group(1)

            # ===TABLE_MARKDOWN=== 섹션 추출
            markdown_section = ""
            md_match = re.search(r'(===TABLE_MARKDOWN===.*?)(?:===TEXT_CONTENT===|$)', text, re.DOTALL)
            if md_match:
                markdown_section = md_match.group(1).strip()

            # ===TEXT_CONTENT=== 섹션 추출 (코드 블록 마커 제거 포함)
            text_section = ""
            tc_match = re.search(r'(===TEXT_CONTENT===.*?)$', text, re.DOTALL)
            if tc_match:
                text_section = tc_match.group(1).strip()
                # TEXT_CONTENT 내부의 코드 블록 마커 제거 (```none 등)
                text_section = re.sub(r'```(?:none|text|plain)\s*', '', text_section)
                text_section = re.sub(r'```\s*$', '', text_section, flags=re.MULTILINE)
                text_section = text_section.strip()

            # 순수 콘텐츠만으로 재구성 (분석 텍스트 제거됨)
            parts = [table_html]
            if markdown_section:
                parts.append(markdown_section)
            if text_section:
                parts.append(text_section)
            text = "\n\n".join(parts)

        # 연속된 빈 줄 정리 (3개 이상 → 2개)
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text.strip()

    def _fix_header_rowspan_overflow(self, html_content: str) -> str:
        """헤더 행의 rowspan이 데이터 행으로 넘치는 경우 수정합니다.

        Qwen 모델이 3행 헤더에서 rowspan=4로 잘못 설정하는 패턴을 교정합니다.
        예: 구분(rowspan=4) → 구분(rowspan=3) (헤더 3행만 커버하도록)

        GPT 모델에는 적용되지 않습니다 (use_openrouter 분기에서만 호출).
        """
        if not BS4_AVAILABLE:
            return html_content

        table_match = re.search(r'(<table[^>]*>.*?</table>)', html_content, re.DOTALL | re.IGNORECASE)
        if not table_match:
            return html_content

        try:
            soup = BeautifulSoup(table_match.group(1), 'html.parser')
            table = soup.find('table')
            if not table:
                return html_content

            rows = table.find_all('tr')
            if len(rows) < 2:
                return html_content

            # 연속된 헤더 행 수 계산 (상단부터 <th>가 포함된 행)
            header_row_count = 0
            for row in rows:
                if row.find('th'):
                    header_row_count += 1
                else:
                    break

            if header_row_count == 0:
                return html_content

            # 헤더 행 내 rowspan이 header_row_count를 초과하면 축소
            fixed = False
            for row_idx in range(header_row_count):
                cells = rows[row_idx].find_all(['td', 'th'])
                for cell in cells:
                    rs = int(cell.get('rowspan', 1))
                    if rs > header_row_count:
                        cell_text = cell.get_text(strip=True)[:20]
                        print(f"[DEBUG] 헤더 rowspan 오버플로우 수정: row {row_idx} "
                              f"'{cell_text}' rowspan={rs} → {header_row_count}")
                        cell['rowspan'] = str(header_row_count)
                        fixed = True

            if not fixed:
                return html_content

            new_table = str(table)
            html_content = html_content[:table_match.start(1)] + new_table + html_content[table_match.end(1):]
            return html_content

        except Exception as e:
            print(f"[DEBUG] 헤더 rowspan 오버플로우 수정 중 오류: {e}")
            return html_content

    def _fix_openrouter_table_structure(self, html_content: str) -> str:
        """OpenRouter/Qwen 출력의 구조적 문제를 프로그래밍적으로 수정합니다.

        Qwen 모델의 반복적 구조 오류를 정확히 수정합니다:
        1. 헤더 rowspan 오버플로우 교정 (verify pass가 재도입할 수 있으므로 재적용)
        2. 열 수 부족 행: 연속 빈 셀 병합 또는 마지막 셀 colspan 증가로 보정
        3. 데이터 행 마지막 열 텍스트+빈 셀 → rowspan 병합

        GPT 모델에는 적용되지 않습니다 (use_openrouter 분기에서만 호출).
        """
        if not BS4_AVAILABLE:
            return html_content

        # Step 0: 헤더 rowspan 오버플로우 재적용 (verify pass가 재도입할 수 있음)
        html_content = self._fix_header_rowspan_overflow(html_content)

        # <table>...</table> 부분 추출 (TABLE_MARKDOWN, TEXT_CONTENT은 보존)
        table_match = re.search(r'(<table[^>]*>.*?</table>)', html_content, re.DOTALL | re.IGNORECASE)
        if not table_match:
            return html_content

        try:
            soup = BeautifulSoup(table_match.group(1), 'html.parser')
            table = soup.find('table')
            if not table:
                return html_content

            rows = table.find_all('tr')
            if len(rows) < 2:
                return html_content

            modified = False

            # === 열 수 부족 행의 colspan 자동 보정 ===
            # 총 열 수(rowspan 점유 포함)를 기준으로, 부족한 행의 colspan을 보정
            num_cols = 0
            num_rows = len(rows)
            # rowspan 점유 맵 구축
            rowspan_occ = [0] * num_rows
            for row_idx, row in enumerate(rows):
                cells = row.find_all(['td', 'th'])
                for cell in cells:
                    cs = int(cell.get('colspan', 1))
                    rs = int(cell.get('rowspan', 1))
                    if rs > 1:
                        for r in range(row_idx + 1, min(row_idx + rs, num_rows)):
                            rowspan_occ[r] += cs
                row_new_cols = sum(int(c.get('colspan', 1)) for c in cells)
                effective = row_new_cols + rowspan_occ[row_idx]
                num_cols = max(num_cols, effective)

            if num_cols > 0:
                for row_idx, row in enumerate(rows):
                    cells = row.find_all(['td', 'th'])
                    if not cells:
                        continue
                    row_new_cols = sum(int(c.get('colspan', 1)) for c in cells)
                    effective = row_new_cols + rowspan_occ[row_idx]
                    deficit = num_cols - effective
                    if deficit <= 0:
                        continue

                    # 빈 셀 또는 첫 번째 셀의 colspan을 증가시켜 deficit 해소
                    # 우선순위: 빈 셀 > 첫 번째 셀(헤더 행) > 마지막 셀
                    target_cell = None

                    # 전략 1: 빈 셀이 있으면 첫 번째 빈 셀의 colspan 증가
                    for cell in cells:
                        if not cell.get_text(strip=True):
                            target_cell = cell
                            break

                    # 전략 2: 빈 셀이 없으면, 헤더 행이면 마지막 셀
                    if target_cell is None:
                        has_th = any(c.name == 'th' for c in cells)
                        if has_th:
                            target_cell = cells[-1]

                    # 전략 3: deficit=1, 빈 셀/헤더 없음, rowspan 그룹 맥락 → 첫 텍스트 셀 확장
                    # (과학/특허 표에서 하위 카테고리가 2열을 차지하는 패턴)
                    if target_cell is None and deficit == 1:
                        has_rowspan_context = rowspan_occ[row_idx] > 0 or any(
                            int(c.get('rowspan', 1)) > 1 for c in cells
                        )
                        if has_rowspan_context:
                            # rowspan 셀이 아닌 첫 번째 텍스트 셀 찾기
                            for cell in cells:
                                if int(cell.get('rowspan', 1)) > 1:
                                    continue
                                first_text = cell.get_text(strip=True)
                                if first_text and not any(ch.isdigit() for ch in first_text):
                                    target_cell = cell
                                    break

                    if target_cell is not None:
                        old_cs = int(target_cell.get('colspan', 1))
                        new_cs = old_cs + deficit
                        target_cell['colspan'] = str(new_cs)
                        cell_text = target_cell.get_text(strip=True) or '(빈 셀)'
                        print(f"[DEBUG] colspan 보정: row {row_idx+1} '{cell_text}' colspan={old_cs} → {new_cs} (deficit={deficit})")
                        modified = True

            # === 데이터 행 마지막 열: 텍스트+빈 셀 연속 → rowspan 병합 ===
            # Qwen이 rowspan 대신 별도 빈 셀로 출력하는 패턴 교정
            # (예: "무상 지원" + 아래 빈 셀 → rowspan=2)
            data_row_indices = []
            for row_idx, row in enumerate(rows):
                if not row.find('th'):
                    data_row_indices.append(row_idx)

            for i in range(len(data_row_indices) - 1):
                idx1 = data_row_indices[i]
                idx2 = data_row_indices[i + 1]
                if idx2 != idx1 + 1:  # 연속된 행만 대상
                    continue
                cells1 = rows[idx1].find_all(['td', 'th'])
                cells2 = rows[idx2].find_all(['td', 'th'])
                if not cells1 or not cells2:
                    continue
                last1 = cells1[-1]
                last2 = cells2[-1]
                # 이미 rowspan이 있거나 colspan>1이면 건너뛰기
                if int(last1.get('rowspan', 1)) > 1:
                    continue
                if int(last1.get('colspan', 1)) != 1 or int(last2.get('colspan', 1)) != 1:
                    continue
                text1 = last1.get_text(strip=True)
                text2 = last2.get_text(strip=True)
                # 첫 번째: 텍스트 있고, 두 번째: 빈 셀, 텍스트가 순수 숫자가 아닌 경우만
                if (text1 and not text2 and
                        not text1.replace('.', '').replace(',', '').replace('-', '').isdigit()):
                    last1['rowspan'] = '2'
                    last2.decompose()
                    print(f"[DEBUG] rowspan 병합: row {idx1+1} '{text1[:20]}' → rowspan=2 (아래 빈 셀 제거)")
                    modified = True

            if modified:
                new_table = str(table)
                html_content = html_content[:table_match.start(1)] + new_table + html_content[table_match.end(1):]
                print("[DEBUG] OpenRouter 표 구조 프로그래밍적 수정 완료")

            return html_content

        except Exception as e:
            print(f"[DEBUG] OpenRouter 표 구조 수정 중 오류: {e}")
            import traceback
            traceback.print_exc()
            return html_content

    def _is_flowchart_image(self, image_bytes: bytes) -> bool:
        """
        이미지가 흐름도인지 간단히 판단합니다.
        실제로는 VLM이 판단하지만, 파일명 기반으로도 추정 가능합니다.
        """
        # 이미지 내용 기반 판단은 VLM이 수행하므로 여기서는 False 반환
        # 실제 판단은 describe_image에서 수행
        return False
    
    def describe_image(
        self,
        image_bytes: bytes,
        language: str = "한국어",
        is_table: bool = None,
        is_flowchart: bool = None,
        is_math: bool = None,
    ) -> Dict[str, Any]:
        """
        이미지를 분석하여 텍스트로 변환
        
        Args:
            image_bytes: 이미지 바이트 데이터
            language: 출력 언어
            is_table: 표 여부 (None이면 자동 판단)
            is_flowchart: 흐름도 여부 (None이면 자동 판단)
            is_math: 수학/교육 자료 여부 (None이면 자동 판단)
        """
        # Qwen2.5-VL 로컬 사용 시
        if self.use_qwen:
            # 지연 로딩: 첫 사용 시점에 초기화
            if self.qwen_client is None:
                try:
                    print("[INFO] Qwen2.5-VL 클라이언트 초기화 중...")
                    from core.qwen_vl_client import get_qwen_vl_client, set_gpu_max_concurrent
                    # GPU 동시 추론 수 설정 (H100 등 대용량 GPU에서 병렬 추론 활성화)
                    if self._gpu_max_concurrent > 1:
                        set_gpu_max_concurrent(self._gpu_max_concurrent)
                    # 싱글톤 클라이언트 사용 (병렬 처리 시 모델 중복 로딩 방지)
                    self.qwen_client = get_qwen_vl_client(
                        model_path=self.qwen_model_path,
                        device=self.qwen_device
                    )
                    print(f"[INFO] Qwen2.5-VL 초기화 완료 (GPU 동시 추론: {self._gpu_max_concurrent})")
                except Exception as e:
                    print(f"[ERROR] Qwen2.5-VL 초기화 실패: {e}")
                    import traceback
                    traceback.print_exc()
                    return {
                        "text": "",
                        "model": "qwen2.5-vl",
                        "error": f"Qwen2.5-VL 초기화 실패: {str(e)}",
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                    }

            if self.qwen_client:
                return self.qwen_client.describe_image(
                    image_bytes,
                    language=language,
                    is_table=is_table,
                    is_flowchart=is_flowchart,
                    is_math=is_math
                )
            else:
                return {
                    "text": "",
                    "model": "qwen2.5-vl",
                    "error": "Qwen2.5-VL 클라이언트 초기화 실패",
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                }

        # 이미지 해시 생성 (캐싱용)
        import hashlib
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        cache_key = f"{image_hash}_{language}_{is_table}_{is_flowchart}_{is_math}"
        
        # 캐시 확인 (병렬 처리 시 스레드 안전)
        with self._cache_lock:
            if cache_key in self._image_cache:
                print(f"[DEBUG] 캐시된 결과 사용 (이미지 해시: {image_hash[:16]}...)")
                return self._image_cache[cache_key]
        
        # VLMChartService 내부 util을 재사용해 base64 생성
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(image_bytes)
                tmp.flush()
                tmp_path = tmp.name
            pil_image = self.service._preprocess_image(tmp_path)  # type: ignore
            base64_image = self.service._encode_image_to_base64(pil_image)  # type: ignore
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # 모든 플래그가 None이면 일반 이미지 설명 프롬프트 사용
        if is_table is None and is_flowchart is None and is_math is None:
            prompt = IMAGE_DESCRIPTION_PROMPT.format(language=language)
            model_name = self.service.openai_model.lower()
            is_openrouter_qwen = getattr(self, "use_openrouter", False) and "qwen/" in model_name
            max_tokens = 12000 if is_openrouter_qwen else 14000
            if self.use_claude:
                result = self._describe_image_with_claude(base64_image, prompt, max_tokens)
            else:
                result = self._describe_image_with_openai(base64_image, prompt, max_tokens)
            with self._cache_lock:
                self._image_cache[cache_key] = result
            return result

        elif is_flowchart is True:
            # 흐름도 이미지인 경우 FLOWCHART_PROMPT 사용
            prompt = FLOWCHART_PROMPT.format(language=language)
            max_tokens = 12000  # 흐름도는 모든 경로를 포함해야 하므로 더 많은 토큰 필요
            
            # Claude 사용 여부에 따라 다른 API 호출
            if self.use_claude:
                result = self._describe_image_with_claude(base64_image, prompt, max_tokens)
            else:
                result = self._describe_image_with_openai(base64_image, prompt, max_tokens)
            
            # 캐시에 저장
            with self._cache_lock:
                self._image_cache[cache_key] = result
            return result
        elif is_math is True:
            # 수학/교육 자료인 경우 MATH_PROMPT 사용
            prompt = MATH_PROMPT.format(language=language)
            max_tokens = 10000  # 수학/교육 자료는 더 긴 응답 필요
            
            # Claude 사용 여부에 따라 다른 API 호출
            if self.use_claude:
                result = self._describe_image_with_claude(base64_image, prompt, max_tokens)
            else:
                result = self._describe_image_with_openai(base64_image, prompt, max_tokens)
            
            # 캐시에 저장
            with self._cache_lock:
                self._image_cache[cache_key] = result
            return result
        elif is_table is True:
            # 표 분석: 1차 생성 → 구조 검증 → 실패 시 재요청
            prompt = TABLE_PROMPT.format(language=language)

            # OpenRouter/Qwen 모델: 출력 형식 강화 지시 추가 (GPT에는 적용 안 됨)
            if getattr(self, 'use_openrouter', False):
                prompt = (
                    "[출력 형식 강제 - 최우선 규칙]\n"
                    "- 코드 블록(```)으로 감싸지 마세요. 순수 HTML을 직접 출력하세요.\n"
                    "- 마크다운 헤더(### 등)를 사용하지 마세요.\n"
                    "- \"HTML 표 출력\", \"최종 출력\" 같은 섹션 구분을 사용하지 마세요.\n"
                    "- <table>...</table>을 먼저 출력하고, ===TABLE_MARKDOWN=== 구분자 후 Markdown을 출력하세요.\n\n"
                    "[행 완전성 - 절대 필수]\n"
                    "- 이미지의 표를 위에서 아래로 한 행씩 읽으며, 모든 데이터 행을 빠짐없이 포함하세요.\n"
                    "- 같은 관리주체/구분 아래에 여러 항목이 있으면 모두 포함하고, 관리주체 셀에 rowspan을 적용하세요.\n"
                    "- 절대로 행을 건너뛰거나 생략하지 마세요.\n\n"
                    "[다단계 헤더 구조 - 매우 중요]\n"
                    "- 먼저 데이터 행(가장 아래)의 세로 경계선을 세어 TOTAL_COLS를 결정하세요.\n"
                    "- 헤더가 여러 줄(다단계)이면 각 줄의 수평선 위치를 확인하세요.\n"
                    "- 상위 헤더가 하위 열 N개를 덮으면 colspan=N을 사용하세요.\n"
                    "- 좌측/우측 끝 셀이 헤더 전체를 관통하면 rowspan=헤더행수를 사용하세요.\n"
                    "- 데이터 행의 마지막 열이 여러 행에 걸쳐 같은 값이면 rowspan을 사용하세요 (별도 행 금지).\n"
                    "- 예: 3단계 헤더(대분류>중분류>소분류)에서 좌측 '구분' 셀은 rowspan=3,\n"
                    "  대분류 '산업계'는 colspan=4(하위 열 4개), 중분류 '일반 기업'은 colspan=2(하위 열 2개).\n\n"
                ) + prompt

            # 대형 모델(GPT-5, OpenRouter Qwen 등)은 더 긴 응답이 필요할 수 있으므로 토큰 수 증가
            model_name = self.service.openai_model.lower()
            is_openrouter_qwen = getattr(self, "use_openrouter", False) and "qwen/" in model_name
            if is_openrouter_qwen:
                max_tokens = 14000  # Qwen 튜닝값 유지
            elif "gpt-5" in model_name:
                max_tokens = 16000  # GPT 중간값: CER 유지 + 속도 개선 타깃
            else:
                max_tokens = 16000  # 기타 모델 기본값

            # 1차 생성
            if self.use_claude:
                result = self._describe_image_with_claude(base64_image, prompt, max_tokens)
            else:
                result = self._describe_image_with_openai(base64_image, prompt, max_tokens)

            html_content = result.get("text", "")
            
            # 빈 응답 시 1회 재시도 (토큰 제한 등으로 잘렸을 수 있음)
            if not html_content and not result.get("error"):
                if is_openrouter_qwen:
                    retry_max_tokens = min(max(max_tokens * 2, 18000), 22000)
                else:
                    retry_max_tokens = max(max_tokens * 2, 24000)
                print(f"[WARNING] VLM 1차 응답이 비어있습니다. max_tokens={retry_max_tokens}으로 재시도")
                if self.use_claude:
                    result = self._describe_image_with_claude(base64_image, prompt, retry_max_tokens)
                else:
                    result = self._describe_image_with_openai(base64_image, prompt, retry_max_tokens)
                html_content = result.get("text", "")
            
            # 디버깅: 응답 내용 확인
            if not html_content:
                print(f"[WARNING] VLM 1차 응답이 비어있습니다. 모델: {self.service.openai_model}")
                print(f"[DEBUG] result keys: {result.keys()}")
                print(f"[DEBUG] result: {_safe_console_preview(result, 500)}")
                # 빈 응답이면 그대로 반환 (검증 건너뛰기)
                return result

            # OpenRouter/Qwen 모델 응답 정리 (마크다운 포맷 제거, GPT에는 적용 안 됨)
            if getattr(self, 'use_openrouter', False):
                html_content = self._clean_openrouter_response(html_content)
                result["text"] = html_content
                print("[DEBUG] OpenRouter 응답 정리 완료")

            # OpenRouter/Qwen 전용: 헤더 rowspan 오버플로우 선행 교정 (verify 전 실행)
            # Qwen이 rowspan=4로 설정해야 할 곳을 3으로 설정하는 패턴 교정
            if getattr(self, 'use_openrouter', False) and "<table" in html_content.lower():
                html_content = self._fix_header_rowspan_overflow(html_content)
                result["text"] = html_content

            # OpenRouter/Qwen 전용: 프로그래밍적 구조 수정 (GPT에는 적용 안 됨)
            # 헤더 rowspan 오버플로우 수정 + 중복 텍스트 rowspan 병합
            if getattr(self, 'use_openrouter', False) and "<table" in html_content.lower():
                html_content = self._fix_openrouter_table_structure(html_content)
                result["text"] = html_content

            print(f"[DEBUG] VLM 1차 응답 길이: {len(html_content)} 문자")
            print(f"[DEBUG] VLM 1차 응답 시작 부분: {_safe_console_preview(html_content, 500)}")
            print(f"[DEBUG] VLM 모델: {self.service.openai_model}")
            # HTML 표 태그 존재 여부 확인
            has_table_tag = "<table" in html_content.lower()
            has_markdown_separator = "===TABLE_MARKDOWN===" in html_content
            print(f"[DEBUG] HTML 표 태그 존재: {has_table_tag}, Markdown 구분자 존재: {has_markdown_separator}")

            with self._cache_lock:
                self._image_cache[cache_key] = result
            return result
        else:
            # 기타 경우 (하위 호환성): 일반 이미지 프롬프트 사용
            prompt = IMAGE_DESCRIPTION_PROMPT.format(language=language)
            max_tokens = 4000
            
            if self.use_claude:
                result = self._describe_image_with_claude(base64_image, prompt, max_tokens)
            else:
                result = self._describe_image_with_openai(base64_image, prompt, max_tokens)
            
            # 캐시에 저장
            with self._cache_lock:
                self._image_cache[cache_key] = result
            return result
    
    def _describe_image_with_openai(self, base64_image: str, prompt: str, max_tokens: int) -> Dict[str, Any]:
        """OpenAI API를 사용한 이미지 분석 (OpenAI, Fireworks 등 호환 API 공용)"""
        client = self.service.openai_client

        # gpt-5-mini 등 일부 모델은 특별한 파라미터 처리가 필요함
        # OpenRouter 등 외부 서비스는 표준 OpenAI 파라미터 사용
        model_name = self.service.openai_model.lower()
        is_external_api = getattr(self, 'use_openrouter', False)
        use_max_completion_tokens = not is_external_api and ("gpt-5" in model_name or "o1" in model_name)
        prompt_text = _sanitize_openai_text(prompt)

        def _build_api_params(*, force_compact_image: bool = False) -> tuple[Dict[str, Any], dict[str, Any]]:
            image_data_url, image_meta = _build_openai_image_data_url(
                base64_image,
                force_compact=force_compact_image,
            )

            params = {
                "model": self.service.openai_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_data_url},
                            },
                        ],
                    }
                ],
            }

            if use_max_completion_tokens:
                params["max_completion_tokens"] = max_tokens
            else:
                params["max_tokens"] = max_tokens
                params["temperature"] = 0.0
                params["seed"] = 42

            params["timeout"] = 900.0
            return params, image_meta

        api_params, image_meta = _build_api_params(force_compact_image=False)
        print(
            "[DEBUG] OpenAI image request prepared: "
            f"prompt_chars={len(prompt_text)}, "
            f"image_bytes_in={image_meta.get('inputBytes')}, "
            f"image_bytes_out={image_meta.get('outputBytes')}, "
            f"mime={image_meta.get('mime')}, "
            f"compacted={image_meta.get('compacted')}"
        )

        def _set_token_budget(params: Dict[str, Any], token_budget: int) -> Dict[str, Any]:
            updated = dict(params)
            updated.pop("max_tokens", None)
            updated.pop("max_completion_tokens", None)
            token_budget = max(16, int(token_budget))
            if use_max_completion_tokens:
                updated["max_completion_tokens"] = token_budget
            else:
                updated["max_tokens"] = token_budget
            return updated

        def _is_output_limit_error(exc: Exception) -> bool:
            error_text = f"{type(exc).__name__}: {exc}".lower()
            patterns = (
                "max_tokens or model output limit was reached",
                "model output limit was reached",
                "max output limit",
                "maximum context length",
            )
            return any(pattern in error_text for pattern in patterns)

        def _call_with_retries(params: Dict[str, Any], initial_tokens: int, label: str):
            token_candidates = [initial_tokens]
            current_tokens = initial_tokens
            while current_tokens < 32000:
                next_tokens = min(max(current_tokens * 2, current_tokens + 4000), 32000)
                if next_tokens <= current_tokens:
                    break
                token_candidates.append(next_tokens)
                current_tokens = next_tokens

            last_error = None
            for token_index, token_budget in enumerate(token_candidates):
                request_params = _set_token_budget(params, token_budget)
                max_retries = 2

                for attempt in range(max_retries + 1):
                    try:
                        with self._api_semaphore:
                            return client.chat.completions.create(**request_params), token_budget
                    except Exception as e:
                        last_error = e

                        if _is_output_limit_error(e):
                            has_more_budget = token_index < len(token_candidates) - 1
                            if has_more_budget:
                                next_budget = token_candidates[token_index + 1]
                                print(
                                    f"[WARNING] {label} 중 출력 한도 초과: 현재 토큰={token_budget}, "
                                    f"다음 토큰={next_budget}로 재시도합니다."
                                )
                                break

                        is_retryable = any(
                            x in str(type(e).__name__).lower() or x in str(e).lower()
                            for x in ("timeout", "connection", "rate", "503", "502", "429")
                        )
                        if attempt < max_retries and is_retryable:
                            wait_sec = 30 * (attempt + 1)
                            print(f"[WARNING] API 호출 실패 (재시도 {attempt + 1}/{max_retries}): {e}. {wait_sec}초 후 재시도...")
                            import time as _time
                            _time.sleep(wait_sec)
                            continue
                        raise

            raise last_error

        try:
            response, effective_tokens = _call_with_retries(api_params, max_tokens, "VLM 이미지 분석")
        except Exception as e:
            if _is_openai_payload_parse_error(e):
                print("[WARNING] OpenAI payload parse failed. Retrying once with compact JPEG payload.")
                try:
                    compact_params, compact_meta = _build_api_params(force_compact_image=True)
                    print(
                        "[DEBUG] OpenAI compact retry prepared: "
                        f"prompt_chars={len(prompt_text)}, "
                        f"image_bytes_in={compact_meta.get('inputBytes')}, "
                        f"image_bytes_out={compact_meta.get('outputBytes')}, "
                        f"mime={compact_meta.get('mime')}, "
                        f"compacted={compact_meta.get('compacted')}"
                    )
                    response, effective_tokens = _call_with_retries(
                        compact_params,
                        max_tokens,
                        "VLM 이미지 분석 compact-retry",
                    )
                except Exception as retry_exc:
                    print(f"[ERROR] OpenAI API 호출 실패: {retry_exc}")
                    import traceback
                    traceback.print_exc()
                    return {
                        "text": "",
                        "model": self.service.openai_model,
                        "error": str(retry_exc),
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    }
            else:
                print(f"[ERROR] OpenAI API 호출 실패: {e}")
                import traceback
                traceback.print_exc()
                return {
                    "text": "",
                    "model": self.service.openai_model,
                    "error": str(e),
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }

        # 응답 내용 추출
        if not response.choices:
            print(f"[ERROR] 응답에 choices가 없습니다. 모델: {self.service.openai_model}")
            return {
                "text": "",
                "model": self.service.openai_model,
                "error": "No choices in response",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        
        choice = response.choices[0]
        content = choice.message.content if choice.message else None
        finish_reason = getattr(choice, "finish_reason", None) or "stop"
        print(f"[DEBUG] finish_reason: {finish_reason}, content type: {type(content)}, content length: {len(content) if content else 0}")

        # finish_reason이 "length"인 경우 토큰 제한으로 잘림 → 최대 2회 재시도(2배→4배, 상한 32k)
        if finish_reason == "length":
            for retry_round in range(2):
                mult = 2 ** (retry_round + 1)
                retry_tokens = min(effective_tokens * mult, 32000)
                print(f"[WARNING] 응답이 토큰 제한으로 잘렸습니다. max_tokens={retry_tokens}으로 재시도({retry_round + 1}/2)합니다.")
                retry_params = _set_token_budget(api_params, retry_tokens)
                try:
                    retry_response, effective_tokens = _call_with_retries(retry_params, retry_tokens, "VLM 이미지 분석 재시도")
                except Exception as e:
                    print(f"[ERROR] 토큰 확장 재시도 실패: {e}")
                    break
                if retry_response.choices:
                    retry_choice = retry_response.choices[0]
                    retry_content = retry_choice.message.content if retry_choice.message else None
                    retry_reason = getattr(retry_choice, "finish_reason", None) or "stop"
                    if retry_content and (len(retry_content) > len(content or "") or retry_reason == "stop"):
                        content = retry_content
                        finish_reason = retry_reason
                        response = retry_response
                        choice = retry_choice
                        print(f"[DEBUG] 재시도 결과: finish_reason={finish_reason}, content length={len(content)}")
                        break
                if finish_reason == "stop":
                    break

        # gpt-5-mini는 content가 None일 수 있으므로 확인
        if content is None:
            print(f"[WARNING] VLM 응답 content가 None입니다. 모델: {self.service.openai_model}")
            print(f"[DEBUG] finish_reason: {finish_reason}")
            # finish_reason이 "stop"이 아닌 경우 문제일 수 있음
            if finish_reason != "stop":
                print(f"[ERROR] finish_reason이 'stop'이 아닙니다: {finish_reason}")
            # response 전체 구조 확인
            print(f"[DEBUG] response 구조: choices={len(response.choices)}, choice keys={dir(choice)}")
            if hasattr(choice, 'message'):
                print(f"[DEBUG] message keys: {dir(choice.message)}")
            content = ""
        elif not content:
            # content가 빈 문자열인 경우
            print(f"[WARNING] VLM 응답 content가 빈 문자열입니다. finish_reason: {finish_reason}")
            print(f"[DEBUG] usage: {response.usage if response.usage else 'None'}")
        
        return {
            "text": content or "",  # None이면 빈 문자열로 변환
            "model": self.service.openai_model,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            },
        }
    
    def _request_fix_with_openai(self, messages: list, max_tokens: int) -> Dict[str, Any]:
        """OpenAI API를 사용한 표 구조 수정 요청 (OpenAI, Fireworks 등 호환 API 공용)"""
        client = self.service.openai_client

        # gpt-5-mini 등 일부 모델은 특별한 파라미터 처리가 필요함
        # OpenRouter 등 외부 서비스는 표준 OpenAI 파라미터 사용
        model_name = self.service.openai_model.lower()
        is_external_api = getattr(self, 'use_openrouter', False)
        use_max_completion_tokens = not is_external_api and ("gpt-5" in model_name or "o1" in model_name)
        api_params = {
            "model": self.service.openai_model,
            "messages": messages,
        }
        
        # 모델에 따라 적절한 파라미터 사용
        if use_max_completion_tokens:
            api_params["max_completion_tokens"] = max_tokens
            # gpt-5/o1 계열은 temperature=0 미지원. 파라미터 생략 시 기본값(1) 사용
        else:
            api_params["max_tokens"] = max_tokens
            api_params["temperature"] = 0.0  # 일관성 향상을 위해 0으로 설정
            api_params["seed"] = 42  # 재현성을 위한 시드 설정 (일부 모델만 지원)

        api_params["timeout"] = 900.0

        def _set_token_budget(params: Dict[str, Any], token_budget: int) -> Dict[str, Any]:
            updated = dict(params)
            updated.pop("max_tokens", None)
            updated.pop("max_completion_tokens", None)
            token_budget = max(16, int(token_budget))
            if use_max_completion_tokens:
                updated["max_completion_tokens"] = token_budget
            else:
                updated["max_tokens"] = token_budget
            return updated

        def _is_output_limit_error(exc: Exception) -> bool:
            error_text = f"{type(exc).__name__}: {exc}".lower()
            return any(
                pattern in error_text
                for pattern in (
                    "max_tokens or model output limit was reached",
                    "model output limit was reached",
                    "max output limit",
                    "maximum context length",
                )
            )

        try:
            retry_budgets = [max_tokens]
            current_tokens = max_tokens
            while current_tokens < 32000:
                next_tokens = min(max(current_tokens * 2, current_tokens + 4000), 32000)
                if next_tokens <= current_tokens:
                    break
                retry_budgets.append(next_tokens)
                current_tokens = next_tokens

            response = None
            for budget_index, token_budget in enumerate(retry_budgets):
                request_params = _set_token_budget(api_params, token_budget)
                try:
                    with self._api_semaphore:
                        response = client.chat.completions.create(**request_params)
                    break
                except Exception as e:
                    if _is_output_limit_error(e) and budget_index < len(retry_budgets) - 1:
                        next_budget = retry_budgets[budget_index + 1]
                        print(
                            f"[WARNING] 표 구조 수정 응답이 출력 한도에 걸렸습니다. "
                            f"현재 토큰={token_budget}, 다음 토큰={next_budget}로 재시도합니다."
                        )
                        continue
                    raise
        except Exception as e:
            print(f"[ERROR] OpenAI API 호출 실패 (수정 요청): {e}")
            import traceback
            traceback.print_exc()
            return {
                "text": "",
                "model": self.service.openai_model,
                "error": str(e),
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

        if not response.choices:
            print(f"[ERROR] 응답에 choices가 없습니다 (수정 요청). 모델: {self.service.openai_model}")
            return {
                "text": "",
                "model": self.service.openai_model,
                "error": "No choices in response",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        
        choice = response.choices[0]
        content = choice.message.content if choice.message else None
        finish_reason = choice.finish_reason
        
        print(f"[DEBUG] 수정 요청 finish_reason: {finish_reason}, content type: {type(content)}, content length: {len(content) if content else 0}")
        
        if content is None:
            print("[WARNING] 수정 요청 응답 content가 None입니다.")
            content = ""
        
        return {
            "text": content or "",
            "model": self.service.openai_model,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            },
        }
    
    def _request_fix_with_claude(self, messages: list, max_tokens: int) -> Dict[str, Any]:
        """Claude API를 사용한 표 구조 수정 요청"""
        if hasattr(self, 'claude_client'):
            # Anthropic API 사용
            # messages를 Claude 형식으로 변환
            user_content = []
            for msg in messages:
                if msg["role"] == "user":
                    for content_item in msg["content"]:
                        if content_item["type"] == "text":
                            user_content.append({
                                "type": "text",
                                "text": content_item["text"]
                            })
            
            with self._api_semaphore:
                message = self.claude_client.messages.create(
                    model="claude-3-5-sonnet-20241022",
                    max_tokens=max_tokens,
                    temperature=0.0,
                    messages=[
                        {
                            "role": "user",
                            "content": user_content,
                        }
                    ],
                )
            content = message.content[0].text
            usage = message.usage

            return {
                "text": content,
                "model": "claude-3-5-sonnet",
                "usage": {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "total_tokens": usage.input_tokens + usage.output_tokens,
                },
            }
        elif hasattr(self, 'bedrock_client'):
            # AWS Bedrock 사용
            import json
            model_id = f"anthropic.{self.claude_model}"

            # messages를 Bedrock 형식으로 변환
            bedrock_messages = []
            for msg in messages:
                if msg["role"] == "user":
                    content_list = []
                    for content_item in msg["content"]:
                        if content_item["type"] == "text":
                            content_list.append({
                                "type": "text",
                                "text": content_item["text"]
                            })
                    bedrock_messages.append({
                        "role": "user",
                        "content": content_list,
                    })

            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "temperature": 0.0,
                "messages": bedrock_messages,
            })

            with self._api_semaphore:
                response = self.bedrock_client.invoke_model(
                    modelId=model_id,
                    body=body,
                )

            response_body = json.loads(response.get('body').read())
            content = response_body['content'][0]['text']
            
            return {
                "text": content,
                "model": model_id,
                "usage": {
                    "prompt_tokens": response_body.get('usage', {}).get('input_tokens', 0),
                    "completion_tokens": response_body.get('usage', {}).get('output_tokens', 0),
                    "total_tokens": response_body.get('usage', {}).get('input_tokens', 0) + response_body.get('usage', {}).get('output_tokens', 0),
                },
            }
        else:
            # Claude 사용 불가 시 OpenAI로 폴백
            return self._request_fix_with_openai(messages, max_tokens)
    
    def _describe_image_with_claude(self, base64_image: str, prompt: str, max_tokens: int) -> Dict[str, Any]:
        """Claude API를 사용한 이미지 분석"""

        if hasattr(self, 'claude_client'):
            # Anthropic API 사용 (세마포어로 동시 호출 수 제한)
            with self._api_semaphore:
                message = self.claude_client.messages.create(
                    model="claude-3-5-sonnet-20241022",  # Anthropic API 모델명
                    max_tokens=max_tokens,
                    temperature=0.0,  # 일관성 향상을 위해 0으로 설정
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": base64_image,
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": prompt,
                                },
                            ],
                        }
                    ],
                )
            content = message.content[0].text
            usage = message.usage
            
            return {
                "text": content,
                "model": "claude-3-5-sonnet",
                "usage": {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "total_tokens": usage.input_tokens + usage.output_tokens,
                },
            }
        elif hasattr(self, 'bedrock_client'):
            # AWS Bedrock 사용
            import json
            model_id = f"anthropic.{self.claude_model}"
            
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "temperature": 0.0,  # 일관성 향상을 위해 0으로 설정
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": base64_image,
                                },
                            },
                            {
                                "type": "text",
                                "text": prompt,
                            },
                        ],
                    }
                ],
            })

            with self._api_semaphore:
                response = self.bedrock_client.invoke_model(
                    modelId=model_id,
                    body=body,
                )

            response_body = json.loads(response.get('body').read())
            content = response_body['content'][0]['text']
            
            return {
                "text": content,
                "model": model_id,
                "usage": {
                    "prompt_tokens": response_body.get('usage', {}).get('input_tokens', 0),
                    "completion_tokens": response_body.get('usage', {}).get('output_tokens', 0),
                    "total_tokens": response_body.get('usage', {}).get('input_tokens', 0) + response_body.get('usage', {}).get('output_tokens', 0),
                },
            }
        else:
            # Claude 사용 불가 시 OpenAI로 폴백
            print("[WARNING] Claude 클라이언트를 사용할 수 없어 OpenAI로 폴백합니다.")
            return self._describe_image_with_openai(base64_image, prompt, max_tokens)
