# 성능 평가 시스템 (eval)

문서 추출 엔진 성능을 평가하는 도구입니다.  
`backend` 루트에서 `eval_run.py`를 실행합니다.

## 폴더 구조

```
eval/
├── data/
│   ├── input/           # 평가 입력 데이터셋 (chart/excel/table/flow/documents)
│   └── gt/              # GT JSON 데이터셋 (같은 구조 권장)
├── gt/                  # 기존 GT 저장소
├── metrics/
│   ├── cer.py
│   ├── dpbench.py
│   ├── table_match.py
│   ├── multipage_table.py
│   ├── speed.py
│   └── cost.py
├── parsers/
├── results/             # 실행 결과(.md/.json) 자동 저장
├── runner.py
├── report.py
└── schema.py
```

## 평가 지표

| 지표 | 설명 | GT 필요 |
|---|---|---|
| CER | 문자 오류율 (낮을수록 좋음, 1 초과 가능) | O |
| NID | 정규화 Indel 유사도 (높을수록 좋음, 0~1) | O |
| TEDS | 표 구조+셀 텍스트 유사도 (높을수록 좋음, 0~1) | O |
| TEDS-S | 표 구조 전용 유사도 (높을수록 좋음, 0~1) | O |
| Table Match | HTML 표 구조 일치도 (0~1, 높을수록 좋음) | O |
| 다중페이지 표 | 멀티페이지 표 병합 판단 | O |
| 처리 속도 | 페이지당 처리 시간(초) | X |
| 비용 추정 | API 사용 비용(USD) | X |

## 실행 방법

### 1) 환경 설정

```bash
# backend 루트에서
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) API 키 설정

`.env` 예시:

```
OPENAI_API_KEY=sk-...
```

### 3) 평가 실행

```bash
# GT 포함 평가 (권장)
python eval_run.py --input-dir ./eval/data/input/table --gt-dir ./eval/data/gt/table --engine pipeline

# GT 없이 속도/비용만
python eval_run.py --input-dir ./eval/data/input/table --no-gt --engine pipeline

# 엔진 전체 비교
python eval_run.py --input-dir ./eval/data/input/table --gt-dir ./eval/data/gt/table --engine all
```

주의:
- 옵션은 반드시 `--input-dir`처럼 하이픈 2개(`--`)를 사용해야 합니다.
- `--engine` 생략 시 기본값은 `all`입니다.
- 결과 파일 경로는 자동 생성됩니다.  
  `eval/results/<timestamp>_<input-dir-name>.md`  
  `eval/results/<timestamp>_<input-dir-name>.json`

## CLI 옵션

| 옵션 | 설명 |
|---|---|
| `--input-dir` | 평가할 입력 디렉토리 (필수) |
| `--gt-dir` | GT JSON 루트 디렉토리 (`--no-gt`가 아니면 권장) |
| `--no-gt` | GT 없이 실행 |
| `--engine` | `gpt` / `gpt-raw` / `pipeline` / `qwen` / `openrouter` / `all` |
| `--pipeline-model` | `--engine pipeline`일 때 모델명 |
| `--openrouter-model` | `--engine openrouter/all`일 때 OpenRouter 모델 ID |

## GT 작성 규칙

입력 파일명 stem과 동일한 JSON 파일명을 사용합니다.

예:  
`table_complex01.png` ↔ `table_complex01.json`

```json
{
  "file": "table_complex01.png",
  "text": "GT 텍스트",
  "tables": [
    "<table>...</table>"
  ],
  "has_multipage_table": false
}
```

- `text`: CER 비교용
- `tables`: 구조 비교용 HTML 표 리스트
- `has_multipage_table`: 멀티페이지 표 여부
