# Luminir Document VLM 서비스 관리 가이드

## 서비스 설치

서비스를 systemd에 등록하려면 다음 명령을 실행하세요:

```bash
./shell/install_service.sh
```

또는 수동으로:

```bash
sudo cp luminir-dp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable luminir-dp.service
sudo systemctl start luminir-dp.service
```

## 서비스 관리 명령어

### 서비스 시작
```bash
sudo systemctl start luminir-dp.service
```

### 서비스 중지
```bash
sudo systemctl stop luminir-dp.service
```

### 서비스 재시작 (코드 수정 후)
```bash
./shell/restart_service.sh
```

또는:

```bash
sudo systemctl restart luminir-dp.service
```

### 서비스 상태 확인
```bash
sudo systemctl status luminir-dp.service
```

### 서비스 로그 확인
```bash
# 실시간 로그 확인
sudo journalctl -u luminir-dp.service -f

# 최근 로그 확인
sudo journalctl -u luminir-dp.service -n 100

# 오늘 로그 확인
sudo journalctl -u luminir-dp.service --since today
```

### 서비스 비활성화 (부팅 시 자동 시작 안 함)
```bash
sudo systemctl disable luminir-dp.service
```

## 코드 수정 후 재시작 방법

1. 코드 수정
2. 서비스 재시작:
   ```bash
   ./shell/restart_service.sh
   ```
   또는:
   ```bash
   sudo systemctl restart luminir-dp.service
   ```
3. 로그 확인:
   ```bash
   sudo journalctl -u luminir-dp.service -f
   ```

## 서비스가 자동으로 재시작됩니다

- 서비스가 비정상 종료되면 자동으로 재시작됩니다 (RestartSec=10)
- 서버 재부팅 시 자동으로 시작됩니다 (enable 상태일 때)

## 접속 주소

서비스가 시작되면 다음 주소로 접속할 수 있습니다:
- 외부 접속: `http://58.234.13.68:8000` (또는 사용 가능한 포트)
- 로컬 접속: `http://localhost:8000`
- API 문서: `http://localhost:8000/docs`

