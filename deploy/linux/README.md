# Linux VPS Deploy

이 폴더는 `02-exchange-listing-sniper`를 Linux VPS에서 상시 실시간 모드로 돌릴 때 쓰는 배포 자산입니다.

## Linux VPS란?

- `VPS`는 `Virtual Private Server`의 약자입니다.
- 쉽게 말해 인터넷 데이터센터에 있는 **항상 켜져 있는 리눅스 서버 한 대를 임대해서 쓰는 것**입니다.
- 내 맥북보다 더 안정적으로 24시간 실행할 수 있고, 재부팅/절전/와이파이 변수 없이 모니터를 유지할 수 있습니다.

## 왜 Linux VPS가 유리한가?

- 24시간 상시 실행이 쉬움
- `systemd`로 자동 재시작 가능
- 백그라운드 서비스 운용이 안정적
- 나중에 Bybit 가까운 리전으로 옮기기 쉬움

## 권장 구조

1. 저장소를 `/opt/chainpulse` 같은 경로에 배치
2. 루트 `.env` 와 `02-exchange-listing-sniper/.env` 설정
3. 실시간 세션 로그인 1회 수행
4. 목적에 맞는 runtime script 선택
   - 감지/알림 중심: `bin/run_low_latency_realtime.sh`
   - race 기반 실전 매수: `bin/run_fast_buy_realtime.sh`
   - TDLib 단독 native-buy: `bin/run_tdlib_native_buy_realtime.sh`
5. `systemd` 서비스로 상시 유지

## 1회 로그인

```bash
cd /opt/chainpulse/detection/02-exchange-listing-sniper
/opt/chainpulse/.venv/bin/python main.py --login-source-telegram
```

세션 파일이 생성된 뒤에는 서비스 실행 시 재로그인 없이 그대로 사용됩니다.

## 수동 실행

```bash
cd /opt/chainpulse/detection/02-exchange-listing-sniper
./bin/run_low_latency_realtime.sh
```

이 스크립트는 다음을 강제합니다.

- `--realtime`
- `--strict-realtime`
- `BYBIT_FAST_EXECUTOR_ENABLED=1`
- `BYBIT_FAST_EXECUTOR_AUTO_BUILD=1`

실전 매수 서비스로 운영하려면 서비스 파일의 `ExecStart`를 `run_fast_buy_realtime.sh` 또는 `run_tdlib_native_buy_realtime.sh`로 바꾼 뒤, 먼저 같은 명령에 `--no-trade`를 붙여 세션/감시 시작을 확인합니다.

## systemd 설치 예시

서비스 파일 복사:

```bash
sudo cp deploy/linux/02-exchange-listing-sniper.service /etc/systemd/system/02-exchange-listing-sniper@${USER}.service
```

서비스 활성화:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now 02-exchange-listing-sniper@${USER}
```

로그 확인:

```bash
journalctl -u 02-exchange-listing-sniper@${USER} -f
```
