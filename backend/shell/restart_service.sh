#!/bin/bash
# Luminir Document VLM 서비스 재시작 스크립트

SERVICE_NAME="luminir-dp.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=========================================="
echo "Luminir Document VLM 서비스 재시작"
echo "=========================================="

# 서비스 파일 업데이트
echo "서비스 파일 업데이트 중..."
sudo cp "$PROJECT_ROOT/$SERVICE_NAME" /etc/systemd/system/
sudo systemctl daemon-reload

# 서비스 중지
echo "서비스 중지 중..."
sudo systemctl stop $SERVICE_NAME

# 잠시 대기
sleep 2

# 서비스 시작
echo "서비스 시작 중..."
sudo systemctl start $SERVICE_NAME

# 서비스 상태 확인
sleep 3
echo ""
echo "서비스 상태:"
sudo systemctl status $SERVICE_NAME --no-pager -l

echo ""
echo "최근 로그 (마지막 20줄):"
sudo journalctl -u $SERVICE_NAME -n 20 --no-pager

echo ""
echo "=========================================="
echo "서비스 로그 확인: sudo journalctl -u $SERVICE_NAME -f"
echo "서비스 중지: sudo systemctl stop $SERVICE_NAME"
echo "서비스 시작: sudo systemctl start $SERVICE_NAME"
echo "=========================================="

