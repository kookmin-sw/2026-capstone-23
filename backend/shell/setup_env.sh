#!/bin/bash
# 환경 변수 설정 파일에서 API 키를 읽어 서비스 파일에 추가하는 스크립트

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_FILE="$PROJECT_ROOT/luminir-dp.service"
ENV_FILE="$PROJECT_ROOT/.env"

echo "=========================================="
echo "환경 변수 설정 확인"
echo "=========================================="

# .env 파일이 있으면 읽기
if [ -f "$ENV_FILE" ]; then
    echo ".env 파일 발견, 환경 변수 로드 중..."
    source "$ENV_FILE"
fi

# 환경 변수 확인
if [ -z "$OPENAI_API_KEY" ]; then
    echo "경고: OPENAI_API_KEY 환경 변수가 설정되지 않았습니다."
    echo "현재 셸의 환경 변수를 확인합니다..."
    OPENAI_API_KEY=$(env | grep -i "^OPENAI_API_KEY=" | cut -d'=' -f2-)
fi

if [ -z "$OPENAI_API_KEY" ]; then
    echo "오류: OPENAI_API_KEY를 찾을 수 없습니다."
    echo "환경 변수를 설정하거나 .env 파일을 생성하세요."
    exit 1
fi

echo "OPENAI_API_KEY 발견: ${OPENAI_API_KEY:0:20}..."

# 서비스 파일 업데이트
echo ""
echo "서비스 파일 업데이트 중..."

# 기존 Environment 라인 제거 (OPENAI_API_KEY 관련)
sed -i '/Environment="OPENAI_API_KEY=/d' "$SERVICE_FILE"

# PYTHONUNBUFFERED 다음에 OPENAI_API_KEY 추가
sed -i '/Environment="PYTHONUNBUFFERED=1"/a Environment="OPENAI_API_KEY='"$OPENAI_API_KEY"'"' "$SERVICE_FILE"

echo "서비스 파일 업데이트 완료!"
echo ""
echo "다음 명령으로 서비스를 재시작하세요:"
echo "  ./shell/restart_service.sh"

