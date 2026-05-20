#!/bin/bash
# 서비스 상태 및 로그 확인 스크립트

SERVICE_NAME="luminir-dp.service"

echo "=========================================="
echo "서비스 상태 확인"
echo "=========================================="

echo ""
echo "1. 서비스 상태:"
sudo systemctl status $SERVICE_NAME --no-pager -l

echo ""
echo "2. 최근 로그 (마지막 30줄):"
sudo journalctl -u $SERVICE_NAME -n 30 --no-pager

echo ""
echo "3. 오류 로그만 확인:"
sudo journalctl -u $SERVICE_NAME --priority=err -n 20 --no-pager

echo ""
echo "=========================================="
echo "실시간 로그 확인: sudo journalctl -u $SERVICE_NAME -f"
echo "=========================================="

