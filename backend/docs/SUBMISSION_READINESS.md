# 나라장터 제출 준비 체크리스트

이 문서는 모델 파일 위치 문제를 제외하고, 현재 백엔드가 제출/설치/운영 검증을 통과하기 위해 확인해야 하는 항목을 정리한다.

## 대상 구성

```text
client
  -> backend(FastAPI)
      -> RabbitMQ(job queue)
      -> Redis(status/cache)
      -> SQLite(data volume)
      -> worker-openai 또는 worker-qwen-*
```

- 일반 배포: `docker-compose.yml`
- 온프레미스 GPU 배포: `docker-compose.onprem.yml`
- 서비스명: `backend`, `redis`, `rabbitmq`, `worker-*`, `recovery`
- 영속 볼륨: `backend_data`, `redis_data`, `rabbitmq_data`

## 제출 전 자동 점검

개발/검증 도구 설치:

```powershell
cd backend
python -m pip install -r requirements-dev.txt
```

점검 실행:

```powershell
.\scripts\run_submission_checks.ps1
```

생성 산출물:

- `reports/ruff.txt`
- `reports/pytest.txt`
- `reports/bandit.json`
- `reports/pip-audit.json`
- `reports/sbom-runtime.json`
- `reports/sbom-runtime.txt`
- `reports/sbom-torch.json`
- `reports/sbom-torch.txt`
- `reports/sbom-qwen.json`
- `reports/sbom-qwen.txt`
- `reports/compose.txt`
- `reports/compose-onprem.txt`

`reports/`는 로컬 제출 검증 산출물이므로 git 추적 대상에서 제외한다.

## 운영 안정성 기준

- 컨테이너는 root가 아닌 `app` 사용자로 실행한다.
- 일반 이미지(`INSTALL_TORCH=0`)는 torch/Qwen 런타임을 설치하지 않는다.
- 온프레미스 GPU 이미지(`INSTALL_TORCH=1`)만 `requirements-torch.txt`와 `requirements-qwen.txt`를 설치한다.
- `backend` 컨테이너는 `/v1/health` Docker healthcheck를 가진다.
- Compose 서비스는 `/v1/health/ready` 기준으로 Redis/RabbitMQ 연결 가능 여부까지 확인한다.
- API readiness는 queue가 준비되지 않으면 HTTP 503을 반환한다.
- Redis/RabbitMQ는 각 1개 인스턴스로 단순화되어 중복 기동을 피한다.
- 데이터, Redis, RabbitMQ 상태는 named volume으로 보존한다.

## DB 스키마 변경 기준

- 컬럼 정의 원본은 `db/models/*`의 SQLAlchemy 모델이다.
- 기존 SQLite DB 보정 로직은 `db/migrations.py`에서 관리한다.
- `db/session.py`는 DB 엔진 생성, 모델 로딩, `create_all`, 마이그레이션 적용, 시드 데이터 입력만 담당한다.
- 새 컬럼을 추가할 때는 모델 변경과 함께 `db/migrations.py`에 idempotent migration을 추가한다.

## 보안 확인

- `.env`의 `ADMIN_ID`, `ADMIN_PW`, `ADMIN_UI_SECRET_KEY`, API key는 제출/설치 환경별로 교체한다.
- `.env`, `data/`, `models/`, `reports/`는 git에 포함하지 않는다.
- `bandit.json`과 `pip-audit.json` 결과는 제출 전 검토하고, 예외가 있으면 근거를 문서화한다.
- SBOM은 `reports/sbom-runtime.json`, `reports/sbom-torch.json`, `reports/sbom-qwen.json`로 생성한다.

## 배포 실행 확인

일반 배포:

```powershell
cd backend
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml up -d --build
```

온프레미스 GPU 배포:

```powershell
cd backend
docker compose -f docker-compose.onprem.yml config --quiet
docker compose -f docker-compose.onprem.yml up -d
```

상태 확인:

```powershell
docker compose ps
curl http://localhost:8000/v1/health
curl http://localhost:8000/v1/health/ready
```

## 아직 제출 전에 사람이 확인해야 하는 항목

- 실제 제출 환경의 포트 충돌 여부
- GPU 서버의 NVIDIA driver / NVIDIA Container Toolkit 설치 여부
- 온프레미스 이미지 반입 방식과 이미지 digest 기록
- 관리자 계정 초기 비밀번호 전달 방식
- 취약점 스캔 결과의 조치 또는 예외 승인 내역
- 운영 로그 보관 정책과 장애 복구 절차
