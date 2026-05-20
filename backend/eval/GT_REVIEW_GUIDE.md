# GT 검수 가이드

Luminir 문서 파싱 파이프라인의 정확도를 평가하기 위한 **Ground Truth(GT) 데이터**를 검수하는 방법을 설명합니다.

---

## GT란?

GT(Ground Truth)는 **정답 데이터**입니다. 각 테스트 파일(이미지, PDF, 엑셀 등)에 대해 "이 파일을 파싱하면 이런 결과가 나와야 한다"를 정의한 JSON 파일입니다.

파이프라인의 파싱 결과와 GT를 비교하여 **CER(문자 오류율)**, **표 구조 일치도** 등의 지표를 산출합니다.

### GT JSON 구조

```json
{
  "file": "table_complex01.png",
  "text": "표 안의 전체 텍스트 (CER 계산용)",
  "tables": [
    "<table><tr><th>헤더</th></tr><tr><td>데이터</td></tr></table>"
  ],
  "has_multipage_table": false
}
```

| 필드 | 설명 |
|---|---|
| `file` | 원본 테스트 파일명 |
| `text` | 파싱 결과로 기대되는 전체 텍스트 |
| `tables` | 기대되는 HTML 표 목록 (없으면 `null`) |
| `has_multipage_table` | 다중 페이지에 걸친 표 포함 여부 |

---

## 디렉토리 구조

```
backend/
├── data/testdata/          ← 원본 테스트 파일
│   ├── table/              ← 표 이미지 (PNG)
│   ├── chart/              ← 차트 이미지 (PNG, JPG)
│   ├── flow/               ← 순서도 이미지 (PNG, JPEG)
│   ├── excel/              ← 엑셀 파일 (XLSX, XLS)
│   └── documents/          ← HWP, PDF 문서
│
├── eval/
│   ├── gt/                 ← GT 정답 데이터
│   │   ├── table/          ← 표 GT (JSON)
│   │   ├── chart/          ← 차트 GT (JSON)
│   │   ├── flow/           ← 순서도 GT (JSON)
│   │   ├── excel/          ← 엑셀 GT (JSON)
│   │   └── documents/      ← 문서 GT (JSON)
│   │
│   ├── gt_review.py        ← 검수용 HTML 뷰어 생성 스크립트
│   ├── gt_review.html      ← 생성된 검수 페이지 (브라우저에서 열기)
│   └── gen_excel_gt.py     ← 엑셀 GT 자동 생성 스크립트
```

---

## 검수 방법

### 1. 검수 HTML 생성

```bash
cd backend
python eval/gt_review.py
```

`eval/gt_review.html` 파일이 생성됩니다.

### 2. 브라우저에서 열기

```bash
# macOS
open eval/gt_review.html

# Linux
xdg-open eval/gt_review.html

# Windows
start eval/gt_review.html
```

### 3. 검수 진행

브라우저에서 열면 아래와 같은 화면이 나옵니다:

```
┌─────────────────────────┬──────────────────────────┐
│  📄 원본 이미지/파일     │   GT 정답 데이터        │
│                         │                          │
│  [이미지 표시]           │  표 1                    │
│                         │  ┌──────┬──────┐         │
│                         │  │ 헤더 │ 헤더 │         │
│                         │  ├──────┼──────┤         │
│                         │  │ 데이터│ 데이터│        │
│                         │  └──────┴──────┘         │
│                         │                          │
│                         │  텍스트 (500자)           │
│                         │  "표 안의 텍스트..."      │
│                         │                          │
│                         │  ☐ 정확함  ☐ 수정 필요    │
│                         │  [메모란]                 │
└─────────────────────────┴──────────────────────────┘
```

- **왼쪽**: 원본 테스트 파일 (이미지, 파일 정보)
- **오른쪽**: GT 데이터 (HTML 표 렌더링 + 텍스트 미리보기)
- **하단**: 검수 체크박스 + 메모란

### 4. 검수 기준

원본과 GT를 비교하면서 아래 항목을 확인합니다:

#### 표 (tables)

| 확인 항목 | 설명 |
|---|---|
| 셀 내용 정확성 | 모든 셀의 텍스트가 원본과 일치하는가 |
| 행/열 구조 | 행과 열의 개수가 맞는가 |
| 병합 셀 | rowspan/colspan이 원본 표와 동일한가 |
| 헤더 구분 | `<th>`와 `<td>` 구분이 적절한가 |
| 누락 없음 | 표의 모든 행이 빠짐없이 포함되었는가 |

#### 텍스트 (text)

| 확인 항목 | 설명 |
|---|---|
| 내용 정확성 | 원본의 텍스트가 빠짐없이 포함되었는가 |
| 오탈자 | 잘못 읽힌 글자가 없는가 |
| 순서 | 텍스트의 읽기 순서가 자연스러운가 |

### 5. 수정이 필요한 경우

GT JSON 파일을 직접 편집합니다:

```bash
# 예: table_complex02의 표 수정
vi eval/gt/table/table_complex02.json
```

수정 후 HTML을 다시 생성하여 확인:

```bash
python eval/gt_review.py
open eval/gt_review.html
```

---

## 카테고리별 GT 현황

| 카테고리 | GT 수 | 원본 위치 | 비고 |
|---|---|---|---|
| table | 21개 | `data/testdata/table/` | 표 이미지 (PNG) |
| chart | 9개 | `data/testdata/chart/` | 차트 이미지 (PNG, JPG) |
| flow | 4개 | `data/testdata/flow/` | 순서도 이미지 (PNG, JPEG) |
| excel | 22개 | `data/testdata/excel/` | 엑셀 파일 (XLSX, XLS) |
| documents | 4개 | `data/testdata/documents/` | PDF 문서 |

---

## GT 자동 생성

### 엑셀 GT 생성

기존 파이프라인(`parse_excel`)을 활용하여 GT 초안을 자동 생성합니다:

```bash
python eval/gen_excel_gt.py
```

- `data/testdata/excel/` 의 모든 엑셀 파일을 파싱
- `eval/gt/excel/` 에 JSON으로 저장
- 이미 존재하는 GT는 건너뜀

### 수동 GT 생성

이미지(table, chart, flow) 및 문서(documents)의 GT는 원본을 보고 수동으로 작성합니다. 기존 GT 파일을 참고하여 동일한 JSON 형식으로 작성합니다.

---

## 평가 지표

GT 검수가 완료되면 eval runner로 파이프라인 정확도를 측정할 수 있습니다.

| 지표 | 설명 | 목표 |
|---|---|---|
| CER | 문자 오류율 (0=완벽, 1=전부 틀림) | < 0.05 |
| Table Match | 표 구조 일치도 (0~1) | > 0.90 |
| Speed | 페이지당 처리 속도 (초) | < 5s |
| Cost | API 호출 비용 (USD) | 최소화 |

### CER 해석

| CER | 수준 |
|---|---|
| 0.00 ~ 0.05 | 우수 |
| 0.05 ~ 0.15 | 양호 |
| 0.15 ~ 0.30 | 미흡 |
| 0.30 이상 | 불량 |

---

## 주의사항

- GT는 **LLM(Claude)이 초안을 생성**한 것이므로 오류가 있을 수 있습니다. 반드시 사람이 원본과 대조하여 검수해야 합니다.
- 특히 **해상도가 낮은 이미지**(table_complex04, 07, 16 등)는 오독 가능성이 높습니다.
- 엑셀 GT는 기존 파이프라인으로 자동 생성된 것이라 파이프라인 자체의 오류가 GT에 포함될 수 있습니다.
- 10MB 이상의 대용량 GT 파일은 리뷰 HTML에서 제외됩니다. 해당 파일은 직접 JSON을 열어서 확인하세요.
