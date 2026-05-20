"""
Qwen2.5-VL 클라이언트
폐쇄망 환경에서 로컬 실행되는 Qwen2.5-VL 모델을 사용하여 이미지 분석 수행

특징:
- Qwen2.5-VL 계열 모델에 최적화된 간결한 프롬프트 사용 (GPT 대비 67~91% 축소)
- GPT와 동일한 이미지 전처리 파이프라인 적용 (샤프닝+대비강화, 업스케일링 제외)
- GPT와 동일한 메시지 순서 (텍스트→이미지) 및 재시도 로직
- 한글 OCR 학습 데이터 포함 (한국어 문서 처리에 최적화)
- 표/차트/흐름도 인식 성능 우수 (DocVQA 95.7%, ChartQA 87.3%)
- BF16 정밀도로 GPU 실행 (모델 크기에 따라 GPU 요구사항 상이)
- max_tokens 축소로 VRAM 효율 향상 (TABLE: 8K, IMAGE/MATH/FLOW: 4K)
"""
import copy
import hashlib
import re
import shutil
import threading
import time
import types
from collections import OrderedDict
from typing import Dict, Any, Optional, Tuple
from pathlib import Path

from core.env import env_bool, env_float, env_int, env_str

# 전역 Lock (Qwen 모델 로딩 직렬화)
_model_load_lock = threading.Lock()

# 전역 Semaphore (Qwen GPU 추론 동시성 제어)
# 기본 1 = 직렬 (안전), H100 등 대용량 GPU에서는 환경변수로 증가
_gpu_max_concurrent = env_int("GPU_MAX_CONCURRENT_INFERENCE", 1, minimum=1)
_inference_semaphore = threading.Semaphore(_gpu_max_concurrent)

# Qwen 생성 토큰 상한 (로컬 GPU 응답 지연 방지용, 환경변수로 조정 가능)
_QWEN_TABLE_MAX_TOKENS = env_int("QWEN_TABLE_MAX_TOKENS", 768, minimum=1)
_QWEN_FLOWCHART_MAX_TOKENS = env_int("QWEN_FLOWCHART_MAX_TOKENS", 512, minimum=1)
_QWEN_MATH_MAX_TOKENS = env_int("QWEN_MATH_MAX_TOKENS", 512, minimum=1)
_QWEN_IMAGE_MAX_TOKENS = env_int("QWEN_IMAGE_MAX_TOKENS", 512, minimum=1)
_QWEN_RETRY_MAX_TOKENS = env_int("QWEN_RETRY_MAX_TOKENS", 1024, minimum=1)
_QWEN_GENERATE_MAX_TIME = env_float("QWEN_GENERATE_MAX_TIME", 120, minimum=1.0)
_QWEN_VISION_MIN_PIXELS = env_int("QWEN_VISION_MIN_PIXELS", 256 * 28 * 28, minimum=28 * 28)
_QWEN_VISION_MAX_PIXELS = max(
    _QWEN_VISION_MIN_PIXELS,
    env_int("QWEN_VISION_MAX_PIXELS", 1280 * 28 * 28, minimum=28 * 28),
)
_QWEN_RESULT_CACHE_MAX_ENTRIES = env_int("QWEN_RESULT_CACHE_MAX_ENTRIES", 128, minimum=0)
_QWEN_RESULT_CACHE_VERSION = "qwen-vl-describe-v1"

# 전역 싱글톤 인스턴스 (병렬 처리 시 모델 중복 로딩 방지)
_global_qwen_client = None
_global_client_lock = threading.Lock()


def _resolve_runtime_device(device: str) -> str:
    requested_device = (device or "cpu").strip().lower()

    if requested_device not in {"gpu", "cuda"}:
        return "cpu"

    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass

    return "cpu"


def _patch_qwen_grid_split_workaround(model) -> None:
    """Work around CUDA driver errors when transformers computes grid split sizes on GPU."""

    def _patched_get_image_features(self, pixel_values, image_grid_thw=None):
        pixel_values = pixel_values.type(self.visual.dtype)
        image_embeds = self.visual(pixel_values, grid_thw=image_grid_thw)
        safe_grid = image_grid_thw.detach().to("cpu")
        split_sizes = (safe_grid.prod(-1) // self.visual.spatial_merge_size**2).tolist()
        image_embeds = torch.split(image_embeds, split_sizes)
        return image_embeds

    def _patched_get_video_features(self, pixel_values_videos, video_grid_thw=None):
        pixel_values_videos = pixel_values_videos.type(self.visual.dtype)
        video_embeds = self.visual(pixel_values_videos, grid_thw=video_grid_thw)
        safe_grid = video_grid_thw.detach().to("cpu")
        split_sizes = (safe_grid.prod(-1) // self.visual.spatial_merge_size**2).tolist()
        video_embeds = torch.split(video_embeds, split_sizes)
        return video_embeds

    import torch

    base_model = getattr(model, "model", None)
    if base_model is None:
        return

    base_model.get_image_features = types.MethodType(_patched_get_image_features, base_model)
    base_model.get_video_features = types.MethodType(_patched_get_video_features, base_model)
    print("[INFO] Qwen grid split CUDA 우회 패치 적용 (split_sizes는 CPU에서 계산)")

# ══════════════════════════════════════════════════════════════
# Qwen2.5-VL 전용 프롬프트 (GPT 프롬프트를 로컬 모델에 맞게 간소화)
# ══════════════════════════════════════════════════════════════

# ── 표 변환 프롬프트 ──
QWEN_TABLE_PROMPT = """당신은 표 이미지를 HTML 표와 Markdown으로 변환하는 변환기입니다.

[출력 형식]
1) 먼저 HTML <table> 출력
2) 구분자 ===TABLE_MARKDOWN=== 후 Markdown 출력
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
- 숫자, 단위(%, 년, 천원 등), 괄호 레이블을 정확히 복사
- 추측하지 말고 각 셀을 개별적으로 확인
- 이미지가 회전되어 있으면 텍스트 방향을 파악하여 올바른 순서로 추출

[Markdown 선형화]
===TABLE_MARKDOWN=== 후 아래 형식으로 출력:

# TableTitle: [표 제목 또는 주요 주제]

| 열1 | 열2 | 열3 | ... |
|-----|-----|-----|-----|
| 값1 | 값2 | 값3 | ... |

- 병합된 셀은 해당 범위의 첫 번째 위치에만 값을 기록
- 모든 행과 열의 데이터를 빠짐없이 포함
- 빈 셀은 공백으로 표시

응답은 {language}로 작성하세요."""

# ── 흐름도 프롬프트 ──
QWEN_FLOWCHART_PROMPT = """당신은 흐름도 이미지를 구조화된 Markdown으로 변환하는 전문가입니다.

[출력 형식]
```markdown
# 흐름도 제목: [제목]

## 개요
[흐름도의 목적을 간단히 설명]

## 시작점
- 시작: [시작 단계 설명]

## 경로 상세

### 경로 1: [경로 이름]
**조건**: [이 경로의 조건]
1. [단계 이름] - [작업 설명] → 다음: [다음 단계]
2. [단계 이름] - [작업 설명] → 다음: [다음 단계]

### 경로 2: [경로 이름]
[동일한 형식으로 기술]

## 조건 분기
- **[조건 이름]**: [조건 설명]
  - True → [경로]
  - False → [경로]

## 종료점
- 종료점 A: [종료 조건 및 결과]
- 종료점 B: [종료 조건 및 결과]
```

[분석 원칙]
- 모든 화살표와 연결선을 추적하여 모든 경로를 찾으세요
- 조건 분기의 True/False 경로를 모두 기술하세요
- 반복(Loop) 구조가 있으면 시작/종료 조건을 명시하세요
- 역방향 화살표가 있으면 반드시 포함하세요
- 각 단계의 작업 내용을 구체적으로 기술하세요

응답은 {language}로 작성하세요."""

# ── 수학/교육 자료 프롬프트 ──
QWEN_MATH_PROMPT = """당신은 수학/교육 자료 이미지를 구조화된 Markdown으로 변환하는 전문가입니다.

[출력 형식]
```markdown
# 수학/교육 자료 제목: [제목]

## 자료 유형
[수학 문제 / 기하학 도형 / 각도 문제 / 수식 / 손글씨 문제]

## 문제/내용 개요
[전체적인 내용 설명]

## 도형 구조 (도형이 있는 경우)

### 점의 위치 및 명칭
- **점 A**: [위치 설명]
- **점 B**: [위치 설명]

### 선분 및 변
- **선분 AB**: [길이, 특징]

### 각도 정보
- **∠ABC**: [각도 값, 예: 37°]
- **∠DEF**: [각도 값, 예: x° (미지수)]

### 도형의 특징
- **도형 유형**: [삼각형, 사각형 등]
- **특수 조건**: [직각, 이등변 등]

## 수식 및 방정식 (수식이 있는 경우)
- [수식을 정확히 전사]

## 문제 조건
- [조건 나열]

## 구하는 것
[문제에서 구하라고 하는 것]

## 텍스트 내용
[이미지에 보이는 모든 텍스트를 정확히 전사]
```

[분석 원칙]
- 도형의 구조, 점의 위치, 선분 관계를 시각적으로 분석하세요
- 각도는 어떤 점들로 이루어진지 명시하세요 (예: ∠BAD = 37°)
- 미지수(x, y 등)를 명확히 표시하세요
- 텍스트만 단순 나열하지 말고 구조화하여 출력하세요

응답은 {language}로 작성하세요."""

# ── 일반 이미지 설명 프롬프트 (차트/다이어그램/일반) ──
QWEN_IMAGE_DESCRIPTION_PROMPT = """당신은 이미지를 분석하여 구조화된 Markdown으로 변환하는 전문가입니다.

이미지 유형을 판단하고 해당 형식으로 출력하세요:

### 1. 차트/그래프인 경우
```markdown
# 차트 제목: [제목]

## 차트 유형
[막대 그래프 / 선 그래프 / 파이 차트 / 산점도 등]

## 축 정보
- **X축**: [축 이름], 단위: [단위]
- **Y축**: [축 이름], 단위: [단위]

## 데이터 표
| [X축 레이블] | [Y축 값] |
|-------------|---------|
| [값] | [값] |

## 트렌드 및 패턴
[주요 트렌드, 최고/최저값, 증감 추세]

## 범례 및 주석
[범례 항목, 출처, 주석]
```

- X축 레이블마다 Y축 값을 눈금선 기준으로 정확히 읽으세요
- 단위(천명, 억원, % 등)를 반드시 반영하세요
- 모든 데이터 포인트를 빠짐없이 포함하세요

### 2. 다이어그램/구조도인 경우
```markdown
# 다이어그램 제목: [제목]

## 개요
[목적과 내용]

## 구조 상세
### [구성 요소 이름]
- **내용**: [텍스트]
- **연결**: → [다른 구성 요소]: [관계 설명]

## 계층 구조 (조직도인 경우)
최상위
  ├─ 레벨 1
  └─ 레벨 2

## 요약
[핵심 내용]
```

### 3. 일반 이미지인 경우
```markdown
# 이미지 제목: [제목]

## 이미지 유형
[사진 / 스크린샷 / 문서 / 인포그래픽]

## 주요 내용
### 텍스트 내용
[이미지의 모든 텍스트를 순서대로 정확히 전사]

### 시각적 요소
[주요 객체, 레이아웃, 아이콘 설명]

### 상세 설명
[왼쪽→오른쪽, 위→아래 순서로 상세 설명]

## 숫자 및 수치 정보
[모든 숫자, 금액, 퍼센트, 날짜]

## 요약
[핵심 메시지]
```

[공통 원칙]
- 이미지에 보이는 내용을 정확히 전사하세요
- 모든 숫자/수치/단위를 원본과 동일하게 보존하세요
- 추측하지 말고 보이는 것만 기술하세요

응답은 {language}로 작성하세요."""


class QwenVLClient:
    """Qwen2.5-VL 로컬 클라이언트 (7B 최적화 프롬프트, GPT 동일 전처리/검증 로직)"""

    def __init__(
        self,
        model_path: str = "../models/Qwen2.5-VL-32B-Instruct",
        device: str = "gpu",
    ):
        self.model_path = str(model_path)
        requested_device = (device or "cpu").strip().lower()
        self.device = _resolve_runtime_device(device)
        if requested_device in {"gpu", "cuda"} and self.device != "cuda":
            print("[WARNING] VLM_DEVICE=gpu 이지만 CUDA/NVIDIA 드라이버를 사용할 수 없어 CPU로 폴백합니다.")
        self.model = None
        self.processor = None
        self.model_revision = env_str("QWEN_MODEL_REVISION", "main", strip=True) or "main"
        self._result_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._result_cache_lock = threading.Lock()

        print(f"[INFO] Qwen2.5-VL 클라이언트 초기화 (모델: {model_path}, 디바이스: {self.device})")

    def _load_model(self):
        """모델 로드 (지연 로딩, Lock으로 중복 로딩 방지)"""
        if self.model is not None:
            return

        with _model_load_lock:
            if self.model is not None:
                return

            print(f"[INFO] Qwen2.5-VL 모델 로딩 중... (경로: {self.model_path})")

            try:
                from transformers import Qwen2_5_VLForConditionalGeneration, Qwen2_5_VLProcessor
                import torch

                model_dir = Path(self.model_path)
                if not model_dir.exists():
                    raise FileNotFoundError(f"Qwen model path does not exist: {model_dir}")
                if not model_dir.is_dir():
                    raise NotADirectoryError(f"Qwen model path is not a directory: {model_dir}")
                model_revision = self.model_revision

                # 일부 체크포인트는 processor_config.json 없이 preprocessor_config.json만 포함한다.
                # 최신 transformers가 절대경로를 Hub repo id처럼 검증하는 경로로 빠지지 않게
                # 로컬 전용 로딩과 함께 processor_config.json을 보완한다.
                processor_config = model_dir / "processor_config.json"
                preprocessor_config = model_dir / "preprocessor_config.json"
                if not processor_config.exists() and preprocessor_config.exists():
                    shutil.copyfile(preprocessor_config, processor_config)
                    print("[INFO] processor_config.json 보완 생성 (from preprocessor_config.json)")

                # Attention 구현 선택: CUDA에서는 flash_attention_2 > sdpa, CPU에서는 eager
                if self.device == "cuda":
                    try:
                        import importlib.util

                        if importlib.util.find_spec("flash_attn") is None:
                            raise ImportError
                        attn_implementation = "flash_attention_2"
                        print("[INFO] FlashAttention2 사용")
                    except ImportError:
                        attn_implementation = "sdpa"
                        print("[INFO] flash_attn 미설치 → SDPA(Scaled Dot-Product Attention) 사용")
                else:
                    attn_implementation = "eager"
                    print("[INFO] CPU 모드 → eager attention 사용")

                # 프로세서 로드 (max_pixels로 비전 토큰 수 제한 → VRAM 절약)
                print(
                    "[INFO] Qwen vision pixel budget: "
                    f"min={_QWEN_VISION_MIN_PIXELS}, max={_QWEN_VISION_MAX_PIXELS}"
                )
                self.processor = Qwen2_5_VLProcessor.from_pretrained(
                    str(model_dir),
                    revision=model_revision,
                    trust_remote_code=True,
                    local_files_only=True,
                    use_fast=False,
                    min_pixels=_QWEN_VISION_MIN_PIXELS,
                    max_pixels=_QWEN_VISION_MAX_PIXELS,
                )

                # 모델 로드 (BF16 강제 적용)
                if self.device == "cuda":
                    prefer_bf16 = env_bool("QWEN_USE_BF16", False)
                    cuda_dtype = torch.bfloat16 if prefer_bf16 else torch.float16
                    dtype_name = "bfloat16" if cuda_dtype == torch.bfloat16 else "float16"
                    print(f"[INFO] Qwen CUDA dtype={dtype_name}")
                    self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                        str(model_dir),
                        revision=model_revision,
                        dtype=cuda_dtype,
                        device_map="cuda:0",
                        attn_implementation=attn_implementation,
                        trust_remote_code=True,
                        local_files_only=True,
                    )
                else:
                    self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                        str(model_dir),
                        revision=model_revision,
                        dtype=torch.float32,
                        device_map="cpu",
                        attn_implementation=attn_implementation,
                        trust_remote_code=True,
                        local_files_only=True,
                    )

                _patch_qwen_grid_split_workaround(self.model)
                self.model.eval()
                print(f"[INFO] Qwen2.5-VL 모델 로드 완료 (attn_implementation={attn_implementation})")

            except Exception as e:
                print(f"[ERROR] Qwen2.5-VL 모델 로드 실패: {e}")
                import traceback
                traceback.print_exc()
                raise

    def ensure_model_loaded(self) -> None:
        """Load the model without running inference."""
        self._load_model()

    @staticmethod
    def _positive_type_hint(value: Optional[bool]) -> Optional[bool]:
        return True if value is True else None

    def _describe_cache_key(
        self,
        image_bytes: bytes,
        *,
        language: str,
        is_table: Optional[bool],
        is_flowchart: Optional[bool],
        is_math: Optional[bool],
    ) -> str:
        image_digest = hashlib.sha256(image_bytes).hexdigest()
        parts = [
            _QWEN_RESULT_CACHE_VERSION,
            image_digest,
            str(self.model_path),
            self.model_revision,
            self.device,
            language,
            f"is_table={is_table is True}",
            f"is_flowchart={is_flowchart is True}",
            f"is_math={is_math is True}",
            f"vision_min={_QWEN_VISION_MIN_PIXELS}",
            f"vision_max={_QWEN_VISION_MAX_PIXELS}",
            f"table_tokens={_QWEN_TABLE_MAX_TOKENS}",
            f"flowchart_tokens={_QWEN_FLOWCHART_MAX_TOKENS}",
            f"math_tokens={_QWEN_MATH_MAX_TOKENS}",
            f"image_tokens={_QWEN_IMAGE_MAX_TOKENS}",
            f"retry_tokens={_QWEN_RETRY_MAX_TOKENS}",
            f"generate_max_time={_QWEN_GENERATE_MAX_TIME}",
        ]
        return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()

    def _get_cached_describe_result(self, cache_key: str) -> Optional[Dict[str, Any]]:
        if _QWEN_RESULT_CACHE_MAX_ENTRIES <= 0:
            return None
        with self._result_cache_lock:
            cached = self._result_cache.get(cache_key)
            if cached is None:
                return None
            self._result_cache.move_to_end(cache_key)
            return copy.deepcopy(cached)

    def _set_cached_describe_result(self, cache_key: str, result: Dict[str, Any]) -> None:
        if _QWEN_RESULT_CACHE_MAX_ENTRIES <= 0 or result.get("error"):
            return
        with self._result_cache_lock:
            self._result_cache[cache_key] = copy.deepcopy(result)
            self._result_cache.move_to_end(cache_key)
            while len(self._result_cache) > _QWEN_RESULT_CACHE_MAX_ENTRIES:
                self._result_cache.popitem(last=False)

    # ──────────────────────────────────────────────
    # 프롬프트 선택 (7B 최적화 프롬프트, 토큰 한도 축소)
    # ──────────────────────────────────────────────
    def _select_prompt(self, is_table: Optional[bool], is_flowchart: Optional[bool],
                       is_math: Optional[bool], language: Optional[str]) -> Tuple[str, int]:
        """Qwen 7B 최적화 프롬프트 선택 (토큰 한도 축소)"""
        lang = language or "한국어"

        if is_table is True:
            return QWEN_TABLE_PROMPT.format(language=lang), _QWEN_TABLE_MAX_TOKENS
        elif is_flowchart is True:
            return QWEN_FLOWCHART_PROMPT.format(language=lang), _QWEN_FLOWCHART_MAX_TOKENS
        elif is_math is True:
            return QWEN_MATH_PROMPT.format(language=lang), _QWEN_MATH_MAX_TOKENS
        else:
            return QWEN_IMAGE_DESCRIPTION_PROMPT.format(language=lang), _QWEN_IMAGE_MAX_TOKENS

    # ──────────────────────────────────────────────
    # 메시지 구성 (GPT와 동일한 순서: 텍스트→이미지)
    # ──────────────────────────────────────────────
    def _build_messages(self, image_bytes: bytes, prompt: str) -> list:
        """Qwen2.5-VL chat template 형식 — 텍스트 먼저, 이미지 후 (GPT와 동일 순서)"""
        import base64

        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                    },
                    {
                        "type": "image",
                        "image": f"data:image/png;base64,{base64_image}",
                    },
                ],
            }
        ]
        return messages

    # ──────────────────────────────────────────────
    # 모델 추론
    # ──────────────────────────────────────────────
    def _generate(self, messages: list, max_new_tokens: int, stage: str = "generate") -> str:
        """모델 추론 실행"""
        import torch
        from qwen_vl_utils import process_vision_info

        started_at = time.time()
        print(f"[DEBUG] Qwen generate 시작: stage={stage}, max_new_tokens={max_new_tokens}")

        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

        image_inputs, video_inputs = process_vision_info(messages)

        inputs = self.processor(
            text=[text],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt",
        )
        print(
            f"[DEBUG] Qwen inputs 준비 완료: stage={stage}, "
            f"input_tokens={inputs.input_ids.shape[-1]}, images={len(image_inputs) if image_inputs else 0}"
        )
        inputs = inputs.to(self.model.device)

        with torch.inference_mode():
            generated_ids = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                max_time=_QWEN_GENERATE_MAX_TIME,
                do_sample=False,
                use_cache=True,
                temperature=None,
                top_p=None,
            )

        generated_ids_trimmed = [
            out_ids[len(in_ids):]
            for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
        ]

        output_text = self.processor.batch_decode(
            generated_ids_trimmed,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]

        elapsed = time.time() - started_at
        print(
            f"[DEBUG] Qwen generate 완료: stage={stage}, elapsed={elapsed:.2f}s, "
            f"output_chars={len(output_text.strip())}"
        )

        return output_text.strip()

    # ──────────────────────────────────────────────
    # 메인 분석 (재시도 로직 포함)
    # ──────────────────────────────────────────────
    def describe_image(
        self,
        image_bytes: bytes,
        is_table: Optional[bool] = None,
        is_math: Optional[bool] = None,
        is_flowchart: Optional[bool] = None,
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """이미지 분석 (프롬프트 선택 → 추론 → 빈 응답 시 재시도)"""
        lang = language or "한국어"
        is_table = self._positive_type_hint(is_table)
        is_flowchart = self._positive_type_hint(is_flowchart)
        is_math = self._positive_type_hint(is_math)

        try:
            cache_key = self._describe_cache_key(
                image_bytes,
                language=lang,
                is_table=is_table,
                is_flowchart=is_flowchart,
                is_math=is_math,
            )
            cached_result = self._get_cached_describe_result(cache_key)
            if cached_result is not None:
                print("[DEBUG] Qwen describe cache hit")
                return cached_result

            self._load_model()

            with _inference_semaphore:

                # ① 프롬프트 선택
                prompt, max_tokens = self._select_prompt(is_table, is_flowchart, is_math, lang)

                # ② 메시지 구성 및 추론
                messages = self._build_messages(image_bytes, prompt)
                text_result = self._generate(messages, max_new_tokens=max_tokens, stage="describe")

                # ③ 빈 응답 시 1회 재시도 (토큰 한도 증가)
                if not text_result or len(text_result.strip()) < 10:
                    retry_max_tokens = min(max_tokens * 2, _QWEN_RETRY_MAX_TOKENS)
                    print(f"[DEBUG] Qwen 빈/짧은 응답 감지, 재시도 (max_tokens: {max_tokens}→{retry_max_tokens})")
                    text_result = self._generate(
                        messages,
                        max_new_tokens=retry_max_tokens,
                        stage="describe_retry",
                    )

                result = {
                    "text": text_result,
                    "model": f"qwen2.5-vl ({Path(self.model_path).name})",
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
                self._set_cached_describe_result(cache_key, result)
                return result

        except Exception as e:
            print(f"[ERROR] Qwen2.5-VL 이미지 분석 실패: {e}")
            import traceback
            traceback.print_exc()
            return {
                "text": "",
                "model": f"qwen2.5-vl ({Path(self.model_path).name})",
                "error": f"Qwen2.5-VL 분석 실패: {str(e)}",
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }


def set_gpu_max_concurrent(n: int):
    """GPU 동시 추론 수 변경 (기존 세마포어를 교체)"""
    global _inference_semaphore
    _inference_semaphore = threading.Semaphore(max(1, n))
    print(f"[INFO] Qwen GPU 동시 추론 수 변경: {max(1, n)}")


def get_qwen_vl_client(
    model_path: str = "../models/Qwen2.5-VL-32B-Instruct",
    device: str = "gpu",
) -> QwenVLClient:
    """싱글톤 Qwen VL 클라이언트 반환 (병렬 처리 시 모델 중복 로딩 방지)"""
    global _global_qwen_client
    resolved_device = _resolve_runtime_device(device)

    with _global_client_lock:
        if (
            _global_qwen_client is None
            or str(_global_qwen_client.model_path) != str(model_path)
            or _global_qwen_client.device != resolved_device
        ):
            _global_qwen_client = QwenVLClient(model_path=model_path, device=device)
        return _global_qwen_client
