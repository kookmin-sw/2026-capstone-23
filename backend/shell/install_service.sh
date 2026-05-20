#!/bin/bash
# Luminir Document VLM 서비스를 systemd에 등록하는 스크립트

SERVICE_NAME="luminir-dp.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_FILE="$PROJECT_ROOT/$SERVICE_NAME"
SYSTEMD_DIR="/etc/systemd/system"

echo "=========================================="
echo "Luminir Document VLM 서비스 설치"
echo "=========================================="

# 서비스 파일 확인
if [ ! -f "$SERVICE_FILE" ]; then
    echo "오류: 서비스 파일을 찾을 수 없습니다: $SERVICE_FILE"
    exit 1
fi

# systemd 디렉토리에 서비스 파일 복사
echo "서비스 파일 복사 중..."
sudo cp "$SERVICE_FILE" "$SYSTEMD_DIR/"

# systemd 데몬 리로드
echo "systemd 데몬 리로드 중..."
sudo systemctl daemon-reload

# 서비스 활성화
echo "서비스 활성화 중..."
sudo systemctl enable $SERVICE_NAME

# 서비스 시작
echo "서비스 시작 중..."
sudo systemctl start $SERVICE_NAME

# 서비스 상태 확인
sleep 2
echo ""
echo "서비스 상태:"
sudo systemctl status $SERVICE_NAME --no-pager -l

echo ""
echo "=========================================="
echo "설치 완료!"
echo "=========================================="
echo "서비스 시작: sudo systemctl start $SERVICE_NAME"
echo "서비스 중지: sudo systemctl stop $SERVICE_NAME"
echo "서비스 재시작: sudo systemctl restart $SERVICE_NAME"
echo "서비스 상태 확인: sudo systemctl status $SERVICE_NAME"
echo "서비스 로그 확인: sudo journalctl -u $SERVICE_NAME -f"
echo "서비스 비활성화: sudo systemctl disable $SERVICE_NAME"

