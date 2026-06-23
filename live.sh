#!/usr/bin/env bash
# 상장 스나이퍼 간편 런처 (LIVE: 무장된 $200 자동매수 + 텔레그램 알림 + 공지당 예산분할).
# 사용법:
#   ./live.sh             # 라이브 (실주문)
#   ./live.sh --no-trade  # 감지만, 주문 안 나감 (안전 테스트)
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

echo '▶ 상장 스나이퍼 시작 (LIVE 무장: KRW 신규상장 → 최대 $200 자동매수, 2티커면 $100씩 + 텔레그램 알림)'
echo '  중지: Ctrl+C   |   감지만(주문X): ./live.sh --no-trade'
echo

exec .venv/bin/python main.py --realtime --realtime-backend race \
  --strict-realtime --keep-warm-interval 15 --memory-state "$@"
