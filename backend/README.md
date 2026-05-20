# Luminir Document Parser Backend

한글(HWP/HWPX), PDF, 이미지 문서를 API 기반으로 업로드하고 파싱 작업을 큐로 처리하는 백엔드입니다. 일반 배포는 OpenAI API worker를 사용하고, 온프레미스 배포는 Qwen2.5-VL 7B 로컬 GPU worker를 사용합니다.

## 구성

- FastAPI API 서버
- RabbitMQ 작업 큐
- Redis 상태 캐시
- SQLite 영구 저장소
- OpenAI worker 또는 Qwen GPU worker
- Docker Compose 일반용 / 온프레미스용 분리

## 요구 사항

- Python 3.12+
- Docker / Docker Compose
- Redis 7
- RabbitMQ 3 Management
- 온프레미스 GPU 실행 시 NVIDIA driver, NVIDIA Container Toolkit
- Qwen2.5-VL 7B 실행 권장 GPU: RTX 3090 24GB 이상

## 로컬 Python 실행

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip setuptools wheel
pip install -r requirements.txt -c requirements-torch.txt

# 로컬 Qwen GPU worker를 실행할 때만 설치
pip install --index-url https://download.pytorch.org/whl/cu124 -r requirements-torch.txt
pip install -r requirements-qwen.txt -c requirements-torch.txt
```

`.env` 파일을 준비합니다.

```env
ADMIN_ID=admin
ADMIN_PW=admin1234
ADMIN_UI_SECRET_KEY=change-me-admin-ui-secret
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=openrouter/openai/gpt-5.2
OPENROUTER_API_KEY=your_openrouter_api_key
RAG_CHAT_MODEL=openrouter/openai/gpt-5.2
RAG_PROVIDER=openrouter
RAG_OPENROUTER_MODEL=openai/gpt-5.2
RAG_EMBEDDING_PROVIDER=openrouter
RAG_EMBEDDING_MODEL=openrouter/openai/text-embedding-3-small
RAG_OPENROUTER_EMBEDDING_MODEL=openai/text-embedding-3-small
```

API만 직접 실행하려면:

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

Swagger 문서:

```text
http://localhost:8000/docs
```

## 일반 Docker 실행

일반 배포는 OpenAI API worker를 사용합니다.

```bash
cd backend
docker compose -f docker-compose.yml up -d --build
```

기본 서비스:

- API: `http://localhost:8000`
- Redis: `localhost:6379`
- RabbitMQ: `localhost:5672`
- RabbitMQ 관리 UI: `http://localhost:15672`

## 온프레미스 Docker 실행

온프레미스 compose 파일은 폐쇄망 서버에서 이미지를 빌드하지 않습니다. 외부망 또는 사내 미러 접근이 가능한 빌드 서버에서 이미지를 만든 뒤 반입해야 합니다.

외부망 빌드 서버:

```bash
cd backend
docker compose -f docker-compose.yml build \
  --build-arg PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 \
  --build-arg INSTALL_TORCH=1

docker image tag luminir-backend:local luminir-backend:onprem
docker pull redis:7-alpine
docker pull rabbitmq:3-management

docker save -o luminir-onprem-images.tar \
  luminir-backend:onprem \
  redis:7-alpine \
  rabbitmq:3-management
```

폐쇄망 서버:

```bash
docker load -i luminir-onprem-images.tar
docker compose -f docker-compose.onprem.yml up -d
```

온프레미스 실행 전 모델 디렉터리가 필요합니다.

```text
../models/Qwen2.5-VL-7B-Instruct
```

필요 시 `.env`에서 이미지와 GPU 설정을 바꿉니다.

```env
BACKEND_IMAGE=luminir-backend:onprem
REDIS_IMAGE=redis:7-alpine
RABBITMQ_IMAGE=rabbitmq:3-management
ENABLE_LOCAL_QWEN_MODEL=1
QWEN_MODEL_HOST_DIR=../models
QWEN_VL_7B_MODEL_PATH=/models/Qwen2.5-VL-7B-Instruct
CUDA_VISIBLE_DEVICES=0
QWEN_INFER_WORKER_MAX_CONCURRENCY=1
QWEN_INFER_GPU_SLOTS=1
```

## CUDA / PyTorch 버전

PyTorch 계열은 [requirements-torch.txt](requirements-torch.txt), Qwen 로컬 런타임은 [requirements-qwen.txt](requirements-qwen.txt)에 분리되어 있습니다.

```text
torch==2.6.0
torchvision==0.21.0
```

Docker 빌드 시 `PYTORCH_INDEX_URL`로 CUDA wheel index를 지정합니다. 기본값은 CUDA 12.4입니다.

```env
PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu124
```

대상 서버의 `nvidia-smi`에서 표시되는 CUDA 지원 버전이 낮다면 빌드 서버에서 해당 CUDA wheel index와 호환되는 `torch`, `torchvision` 조합으로 다시 빌드해야 합니다.

## 주요 API

- `GET /api/v1/health`
- `POST /api/v1/parser/jobs`
- `GET /api/v1/parser/jobs/{jobId}`
- `GET /api/v1/parser/jobs/{jobId}/items`
- `POST /api/v1/parser/jobs/{jobId}/cancel`
- `GET /api/v1/parser/queue/stats`
- `GET /api/v1/dashboard/summary`
- `GET /api/v1/documents`

## 큐와 저장소

운영 기본값:

- 작업 큐: RabbitMQ
- RabbitMQ 장애 시 프로세스 내부 memory queue fallback
- 영구 저장소: SQLite
- 상태 캐시: Redis

관련 기본 환경 변수는 compose에서 제공합니다.

```env
QUEUE_BACKEND=rabbitmq
STORE_BACKEND=sqlite
STATUS_CACHE_BACKEND=redis
RABBITMQ_QUEUE=jobs.queue
```

## 테스트와 검증

```bash
cd backend
python -m ruff check .
python -m pytest tests
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.onprem.yml config --quiet
```

## 디렉터리 구조

```text
backend/
├── api/                    # FastAPI app, routers, queue/status APIs
├── core/                   # 설정, 버전, 변환/파싱 공통 로직
├── worker/                 # OpenAI/Qwen worker 실행 경로
├── db/                     # 영구 저장소 관련 코드
├── data/                   # 입력, 출력, 임시 데이터
├── models/                 # 온프레미스 로컬 모델
├── tests/                  # 백엔드 테스트
├── docker-compose.yml      # 일반 배포
├── docker-compose.onprem.yml # 온프레미스 배포
├── requirements.txt        # 일반 Python 의존성
├── requirements-torch.txt  # PyTorch/CUDA 의존성
└── requirements-qwen.txt   # Qwen 로컬 GPU 런타임 의존성
```
