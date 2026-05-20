# Luminir Document Parser - 설치 가이드

다른 서버에 Luminir Document Parser를 설치하는 방법을 단계별로 설명합니다.

## 목차

1. [시스템 요구사항](#시스템-요구사항)
2. [시스템 의존성 설치](#시스템-의존성-설치)
3. [Python 환경 설정](#python-환경-설정)
4. [프로젝트 설치](#프로젝트-설치)
5. [설정](#설정)
6. [실행 및 테스트](#실행-및-테스트)
7. [문제 해결](#문제-해결)

---

## 시스템 요구사항

### 최소 사양
- **OS**: Ubuntu 20.04+ / Debian 11+ / CentOS 8+
- **CPU**: 2 코어 이상
- **RAM**: 4GB 이상 (권장: 8GB+)
- **디스크**: 10GB 이상 여유 공간
- **Python**: 3.12 이상

### 권장 사양 (대용량 배치 처리)
- **CPU**: 8 코어 이상
- **RAM**: 16GB 이상
- **GPU**: NVIDIA GPU 24GB VRAM 권장 (Qwen2.5-VL 7B 사용 시)
- **디스크**: SSD 50GB+

---

## 시스템 의존성 설치

### Ubuntu/Debian

```bash
# 1. 시스템 패키지 업데이트
sudo apt-get update
sudo apt-get upgrade -y

# 2. 필수 도구 설치
sudo apt-get install -y \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
    python3-pip \
    git \
    wget \
    curl

# 3. HWP/PDF 변환 도구
sudo apt-get install -y \
    libreoffice \
    libreoffice-writer \
    wkhtmltopdf \
    poppler-utils

# 4. 기타 라이브러리
sudo apt-get install -y \
    libmagic1 \
    libmagic-dev \
    libjpeg-dev \
    libpng-dev \
    libcairo2-dev \
    libpango1.0-dev \
    libgdk-pixbuf2.0-dev \
    libffi-dev
```

### CentOS/RHEL

```bash
# 1. EPEL 저장소 활성화
sudo yum install -y epel-release

# 2. 필수 도구
sudo yum install -y \
    python3.12 \
    python3.12-devel \
    git \
    wget

# 3. HWP/PDF 변환 도구
sudo yum install -y \
    libreoffice \
    wkhtmltopdf

# poppler-utils (pdf2image 의존성)
sudo yum install -y poppler-utils

# 4. 기타 라이브러리
sudo yum install -y \
    file-devel \
    libjpeg-devel \
    libpng-devel \
    cairo-devel \
    pango-devel \
    gdk-pixbuf2-devel
```

### 설치 확인

```bash
# Python 버전 확인
python3.12 --version
# 출력 예: Python 3.12.0

# LibreOffice 확인
libreoffice --version
# 출력 예: LibreOffice 7.3.7.2

# wkhtmltopdf 확인
wkhtmltopdf --version
# 출력 예: wkhtmltopdf 0.12.6

# pdf2image 의존성 (pdftoppm) 확인
pdftoppm -v
# 출력 예: pdftoppm version 21.09.0
```

---

## Python 환경 설정

### 1. 프로젝트 디렉토리 생성

```bash
# 원하는 위치로 이동
cd /home/yourusername

# Git clone (또는 파일 복사)
git clone <repository-url> luminir-dp
cd luminir-dp
```

### 2. 가상환경 생성

```bash
# Python 3.12 가상환경 생성
python3.12 -m venv .venv

# 가상환경 활성화
source .venv/bin/activate

# pip 업그레이드
pip install --upgrade pip setuptools wheel
```

### 3. Python 패키지 설치

#### 기본 패키지 (GPT 모델 사용)

```bash
pip install -r requirements.txt -c requirements-torch.txt

# 로컬 Qwen GPU worker를 실행할 때만 설치
pip install --index-url https://download.pytorch.org/whl/cu124 -r requirements-torch.txt
pip install -r requirements-qwen.txt -c requirements-torch.txt
```

설치 시간: 약 5-10분

## 프로젝트 설치

### 디렉토리 구조 생성

```bash
cd luminir-dp

# 필수 디렉토리 생성
mkdir -p data/inputs
mkdir -p data/outputs
mkdir -p data/tmp

# 권한 설정
chmod 755 data/
chmod 755 data/inputs data/outputs data/tmp
```

### 환경 변수 설정

`.env` 파일 생성:

```bash
cat > .env << 'EOF'
# OpenAI API 키 (GPT 모델 사용 시 필수)
OPENAI_API_KEY=your_api_key_here

# VLM 모델 선택 (3090 서버 권장)
OPENAI_MODEL=qwen2.5-vl-7b
QWEN_VL_7B_MODEL_PATH=../models/Qwen2.5-VL-7B-Instruct
VLM_DEVICE=gpu
GPU_MAX_CONCURRENT_INFERENCE=1
CUDA_VISIBLE_DEVICES=0

# OpenAI API를 쓸 때만 설정
# OPENAI_MODEL=gpt-5.2
# OPENAI_API_KEY=your_api_key_here

# GPU 디바이스
# VLM_DEVICE=gpu
# VLM_DEVICE=cpu

# 입력/출력 디렉토리 (선택, 기본값 사용 가능)
# INPUT_ROOT=data/inputs
# OUTPUT_ROOT=data/outputs
# TMP_DIR=data/tmp
EOF
```

3090 서버에서 Docker Compose로 실행할 때는 루트 디렉토리에서 아래 명령을 사용합니다.

```bash
docker compose -f docker-compose.yml up -d --build
```

사전 확인:
- `nvidia-smi`
- `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`

**OpenAI API 키 발급**:
1. https://platform.openai.com/ 접속
2. API Keys 메뉴에서 새 키 생성
3. `.env` 파일에 복사

---

## 설정

### pyhwp 설정 (hwp5html 명령어)

```bash
# 가상환경 활성화 상태에서
source .venv/bin/activate

# hwp5html 명령어 확인
which hwp5html
# 출력: /path/to/.venv/bin/hwp5html

# 테스트
hwp5html --help
```

### wkhtmltopdf 설정

**로컬 파일 접근 허용 확인**:
```bash
wkhtmltopdf --help | grep enable-local-file-access
# 출력: --enable-local-file-access 옵션이 보여야 함
```

**문제 발생 시** (일부 배포판):
```bash
# wkhtmltopdf 최신 버전 설치
wget https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6-1/wkhtmltox_0.12.6-1.focal_amd64.deb
sudo dpkg -i wkhtmltox_0.12.6-1.focal_amd64.deb
sudo apt-get install -f  # 의존성 해결
```

---

## 실행 및 테스트

### 1. 기본 테스트

```bash
# 가상환경 활성화
cd luminir-dp
source .venv/bin/activate

# 테스트 파일 준비
cp /path/to/test.hwp data/inputs/

# 단일 파일 변환 테스트
python -c "
from pathlib import Path
from core.pipeline import DocumentPipeline
from core.config import AppConfig

config = AppConfig()
pipeline = DocumentPipeline(config)

hwp_path = Path('data/inputs/test.hwp')
output_path = pipeline.process_file(hwp_path, language='한국어')

print(f'변환 완료: {output_path}')
"
```

### 2. API 실행

```bash
# FastAPI API server
uvicorn api:app --host 0.0.0.0 --port 8000 --reload

# 터미널 출력:
# Swagger docs: http://127.0.0.1:8000/docs
```

브라우저에서 `http://localhost:8000/docs` 접속

### 3. REST API 실행

```bash
# FastAPI 서버 시작
uvicorn api:app --host 0.0.0.0 --port 8000
```

API 문서: `http://localhost:8000/docs`

### 4. 배치 변환 테스트

```bash
# 테스트 파일들 배치
cp *.hwp data/inputs/

# 배치 변환 (직접 실행)
python -m core.batch_worker \
    --max-workers 2 \
    --language 한국어 \
    --model gpt-5.2

# 진행 상황 확인
python -m core.batch_status
```

---

## systemd 서비스 설정 (선택)

### 서비스 파일 확인

`luminir-dp.service` 내용 확인 및 수정:

```bash
# WorkingDirectory 경로 확인
grep WorkingDirectory luminir-dp.service

# ExecStart 경로 확인
grep ExecStart luminir-dp.service

# 필요 시 수정 (절대 경로 사용)
nano luminir-dp.service
```

### 서비스 설치 및 시작

```bash
# 설치 스크립트 실행
./shell/install_service.sh

# 또는 수동 설치
sudo cp luminir-dp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable luminir-dp.service
sudo systemctl start luminir-dp.service

# 상태 확인
sudo systemctl status luminir-dp.service

# 로그 확인
sudo journalctl -u luminir-dp.service -f
```

---

## 문제 해결

### 1. Python 3.12가 없는 경우

#### Ubuntu 20.04
```bash
# deadsnakes PPA 추가
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install python3.12 python3.12-venv python3.12-dev
```

#### 소스 컴파일
```bash
wget https://www.python.org/ftp/python/3.12.0/Python-3.12.0.tgz
tar -xzf Python-3.12.0.tgz
cd Python-3.12.0
./configure --enable-optimizations
make -j $(nproc)
sudo make altinstall
```

### 2. wkhtmltopdf 설치 실패

**증상**: `apt-get install wkhtmltopdf` 실패 또는 오래된 버전

**해결**:
```bash
# 공식 바이너리 다운로드
wget https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6-1/wkhtmltox_0.12.6-1.focal_amd64.deb

# 설치
sudo dpkg -i wkhtmltox_0.12.6-1.focal_amd64.deb

# 의존성 해결
sudo apt-get install -f

# 확인
wkhtmltopdf --version
```

### 3. LibreOffice 한글 폰트 문제

**증상**: HWP → PDF 변환 시 한글 깨짐

**해결**:
```bash
# 한글 폰트 설치
sudo apt-get install -y \
    fonts-nanum \
    fonts-nanum-coding \
    fonts-nanum-extra

# LibreOffice 폰트 캐시 재생성
fc-cache -fv

# 재시작
sudo systemctl restart luminir-dp.service
```

### 4. GPU 인식 실패 (Qwen2.5-VL 7B)

**증상**: `CUDA not available` 에러

**해결**:
```bash
# NVIDIA 드라이버 설치 확인
nvidia-smi

# CUDA 설치 확인
nvcc --version

# PyTorch CUDA 설치 확인
python -c "import torch; print(torch.cuda.is_available())"

# CUDA 12.4 설치 (필요 시)
# https://developer.nvidia.com/cuda-toolkit-archive
```

### 5. pip 설치 실패

**증상**: `ERROR: Could not build wheels for ...`

**해결**:
```bash
# 개발 도구 설치
sudo apt-get install -y \
    build-essential \
    python3.12-dev \
    libpython3.12-dev

# 컴파일러 캐시 (선택, 빌드 속도 향상)
sudo apt-get install -y ccache

# 재시도
pip install -r requirements.txt -c requirements-torch.txt
pip install --index-url https://download.pytorch.org/whl/cu124 -r requirements-torch.txt
pip install -r requirements-qwen.txt -c requirements-torch.txt
```

### 6. 메모리 부족

**증상**: `MemoryError` 또는 프로세스 종료

**해결**:
```bash
# 스왑 공간 추가 (4GB)
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# 영구 설정
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 배치 처리 시 병렬 수 감소
# API/worker configuration에서 max_workers를 1 또는 2로 설정
```

---

## 폐쇄망 환경 설치

### 1. 인터넷 연결 환경에서 준비

```bash
# wheel 파일 다운로드
# PyTorch (CUDA 12.4)
pip download -r requirements-torch.txt \
    --index-url https://download.pytorch.org/whl/cu124 \
    -d packages/

pip download -r requirements.txt -c requirements-torch.txt --find-links=packages/ -d packages/
pip download -r requirements-qwen.txt -c requirements-torch.txt --find-links=packages/ -d packages/

# 전체 패키지 압축
tar -czf luminir-packages.tar.gz packages/
```

### 2. 폐쇄망 서버로 전송

```bash
# SCP 또는 USB로 전송
scp luminir-packages.tar.gz user@target-server:/tmp/

# 또는 USB 복사
cp luminir-packages.tar.gz /media/usb/
```

### 3. 폐쇄망 서버에서 설치

```bash
# 압축 해제
cd /tmp
tar -xzf luminir-packages.tar.gz

# 가상환경 생성
cd /home/yourusername/luminir-dp
python3.12 -m venv .venv
source .venv/bin/activate

# 로컬 wheel 파일로 설치
pip install --no-index --find-links=/tmp/packages -r requirements-torch.txt
pip install --no-index --find-links=/tmp/packages -r requirements.txt -c requirements-torch.txt
pip install --no-index --find-links=/tmp/packages -r requirements-qwen.txt -c requirements-torch.txt
```

### 4. Docker 온프레미스 이미지 반입

폐쇄망 서버에서는 `docker compose build`를 실행하지 않습니다. 외부망 또는 사내 미러 접근이 가능한 빌드 서버에서 이미지를 만든 뒤 반입합니다.

```bash
# 외부망/빌드 서버
docker compose -f docker-compose.yml build \
  --build-arg PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu124

docker image tag luminir-backend:local luminir-backend:onprem
docker pull redis:7-alpine
docker pull rabbitmq:3-management

docker save -o luminir-onprem-images.tar \
  luminir-backend:onprem \
  redis:7-alpine \
  rabbitmq:3-management
```

```bash
# 폐쇄망 서버
docker load -i luminir-onprem-images.tar
docker compose -f docker-compose.onprem.yml up -d
```

`docker-compose.onprem.yml`은 이미지를 빌드하지 않고 `BACKEND_IMAGE`, `REDIS_IMAGE`, `RABBITMQ_IMAGE`로 지정된 이미지만 사용합니다.

---

## 데이터 디렉토리 준비

### 디렉토리 구조

```
luminir-dp/
├── data/
│   ├── inputs/          # 변환할 문서 업로드
│   ├── outputs/         # 변환 결과 (.txt, .meta.json)
│   └── tmp/            # 임시 파일 (PDF, HTML, 로그)
```

### 권한 설정

```bash
cd luminir-dp

# 디렉토리 소유자 설정
sudo chown -R $USER:$USER data/

# 읽기/쓰기 권한
chmod -R 755 data/
```

### 디스크 공간 관리

```bash
# tmp 디렉토리 정리 (주기적 실행 권장)
rm -rf data/tmp/*_html/     # HTML 중간 파일
rm -f data/tmp/*.pdf        # 임시 PDF (선택)

# 오래된 로그 삭제
find data/tmp/ -name "*.log" -mtime +7 -delete
```

---

## 환경별 설정

### 개발 환경

```bash
# .env 파일 (개발용)
cat > .env << 'EOF'
OPENAI_API_KEY=sk-test-xxxx
OPENAI_MODEL=gpt-5.2
DEBUG=true
EOF

# 직접 실행
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

### 스테이징 환경

```bash
# .env 파일
cat > .env << 'EOF'
OPENAI_API_KEY=sk-staging-xxxx
OPENAI_MODEL=gpt-5.2
EOF

# 스크립트 실행
./shell/setup_env.sh
```

### 프로덕션 환경

```bash
# .env 파일
cat > .env << 'EOF'
OPENAI_API_KEY=sk-prod-xxxx
OPENAI_MODEL=gpt-5.2
EOF

# systemd 서비스로 실행
sudo systemctl start luminir-dp.service
sudo systemctl enable luminir-dp.service

# 로그 모니터링
sudo journalctl -u luminir-dp.service -f
```

---

## 설치 검증

### 체크리스트

```bash
# 1. Python 버전
python --version
# Python 3.12.x

# 2. 가상환경 활성화
echo $VIRTUAL_ENV
# /path/to/luminir-dp/.venv

# 3. 패키지 설치 확인
pip list | grep -E "fastapi|pymupdf|openai"
# 모든 패키지 버전 출력

# 4. 시스템 도구 확인
libreoffice --version && wkhtmltopdf --version && pdftoppm -v
# 모두 버전 출력

# 5. 디렉토리 확인
ls -la data/
# inputs, outputs, tmp 디렉토리 존재

# 6. 환경 변수 확인
python -c "from core.config import load_config; print(load_config())"
# AppConfig(...) 출력
```

### 통합 테스트

```bash
# 테스트 스크립트 실행
source .venv/bin/activate

python -c "
from pathlib import Path
from core.config import AppConfig
from core.pipeline import DocumentPipeline

print('=== Luminir Document Parser 설치 확인 ===')

# Config 로드
config = AppConfig()
print(f' Config 로드 성공')
print(f'   - 입력 디렉토리: {config.input_root}')
print(f'   - 출력 디렉토리: {config.output_root}')
print(f'   - VLM 모델: {config.openai_model}')

# Pipeline 초기화
pipeline = DocumentPipeline(config)
print(f' Pipeline 초기화 성공')

# 시스템 도구 확인
import shutil
tools = {
    'libreoffice': shutil.which('libreoffice'),
    'wkhtmltopdf': shutil.which('wkhtmltopdf'),
    'hwp5html': shutil.which('hwp5html'),
    'pdftoppm': shutil.which('pdftoppm'),
}

print(f' 시스템 도구 확인:')
for name, path in tools.items():
    status = '' if path else '❌'
    print(f'   {status} {name}: {path or \"없음\"}')

print(f'\n🎉 설치 완료!')
print(f'   API 실행: uvicorn api:app --host 0.0.0.0 --port 8000')
print(f'   API 서버: uvicorn api:app --host 0.0.0.0 --port 8000')
"
```

---

## 다음 단계

### 기본 사용
1. `README.md` - 기본 사용법
2. FastAPI API 서버 실행 후 `/docs`에서 파일 업로드 API 확인

### 고급 기능
1. `README_SERVICE.md` - 서비스 관리

### 문제 발생 시
1. `data/tmp/batch_worker.log` - 변환 로그 확인
2. GitHub Issues 등록
3. 관리자에게 문의

---

## 업그레이드

### 기존 설치 업그레이드

```bash
cd luminir-dp

# 코드 업데이트
git pull origin main

# 가상환경 활성화
source .venv/bin/activate

# 패키지 업데이트
pip install --index-url https://download.pytorch.org/whl/cu124 --upgrade -r requirements-torch.txt
pip install --upgrade -r requirements.txt -c requirements-torch.txt
pip install --upgrade -r requirements-qwen.txt -c requirements-torch.txt

# 서비스 재시작 (systemd 사용 시)
sudo systemctl restart luminir-dp.service
```

### 설정 파일 마이그레이션

버전 업그레이드 시 `.env` 파일이나 설정 변경사항이 있으면 `CHANGELOG.md` 참조

---

## 제거

### 완전 제거

```bash
# 서비스 중지 및 제거
sudo systemctl stop luminir-dp.service
sudo systemctl disable luminir-dp.service
sudo rm /etc/systemd/system/luminir-dp.service
sudo systemctl daemon-reload

# 프로젝트 디렉토리 삭제
cd /home/yourusername
rm -rf luminir-dp

# 시스템 패키지는 유지 (다른 프로젝트에서 사용 가능)
# 필요 시 수동 제거:
# sudo apt-get remove libreoffice wkhtmltopdf
```

---

## 참고 자료

- [README.md](../README.md) - 프로젝트 개요
- [README_SERVICE.md](README_SERVICE.md) - 서비스 관리

---

## 지원

문제 발생 시:
1. 로그 확인: `data/tmp/batch_worker.log`, `journalctl -u luminir-dp.service`
2. GitHub Issues 등록 (버전, 에러 메시지, 로그 첨부)
3. 관리자 이메일: support@example.com
