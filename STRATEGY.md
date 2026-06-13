# 02. Exchange Listing Sniper

## 목표

업비트와 빗썸 공식 텔레그램 채널에서 상장/마켓 추가 공지를 가장 먼저 감지한다.

## 현재 구현 범위

1. 공개 텔레그램 채널 HTML 폴링
2. 상장 공지 패턴 필터링
3. 티커/마켓 정보 추출
4. Bybit spot/perp 존재 여부 확인
5. 감지 직후 Bybit spot 시장가 자동매수
6. 중복 방지 상태 저장
7. 텔레그램 알림 발송

## 제외 범위

- 포지션 관리
- 후속 청산 로직
- 고급 슬리피지 제어
- 리스크 한도 엔진

## 시그널 예시

- 거래소: 빗썸
- 타입: `market_add`
- 티커: `VVV`
- Bybit spot: `true`
- Bybit perp: `true`
- 자동매수: `executed | failed | skipped`
- 원문: 공식 텔레그램 공지 링크
