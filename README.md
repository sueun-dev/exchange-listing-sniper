# 02. Exchange Listing Sniper

업비트와 빗썸의 공식 텔레그램 채널을 감시해 상장/마켓 추가 공지를 빠르게 감지하는 모듈입니다.

지금은 두 가지 실시간 소스 백엔드를 지원합니다.

- 기본 fallback: 공개 채널 HTML 폴링
- 실시간 백엔드: Telethon 유저 세션 기반 MTProto 수신 (`cryptg` 가속 권장)
- 최속 백엔드: TDLib(C++) 기반 실시간 수신
- 현재 실전 최속 모드: `race` first-wins 수신

현재 범위:

- 업비트 공식 텔레그램 `@upbit_news`
- 빗썸 공식 텔레그램 `@BithumbExchange`
- 상장 공지 감지
- Bybit spot/perp 존재 여부 확인
- 감지 직후 Bybit spot 시장가 자동매수
- 주문 fast path는 C++ 프로세스로 실행 가능
- 공지 분류기는 Python/C++/Rust 비교 후 더 빠른 네이티브 경로 사용 가능
- 텔레그램 알림 전송

분류/중복 기준:

- 업비트는 `[거래] ... KRW 마켓 디지털 자산 추가` 또는 `[거래] ... 신규 거래지원 안내 (KRW 마켓)` 계열만 actionable KRW 신규 상장으로 봅니다.
- 업비트 BTC/USDT 단독 마켓 추가, 입출금, 유통량, 유의종목, 이벤트, 종료, 변경 안내는 매수 대상에서 제외합니다.
- 빗썸은 `[마켓 추가] 코인명(TICKER) 원화 마켓 추가` 와 `[마켓 추가/수수료 이벤트] 코인명(TICKER) 원화 마켓 추가 (거래 수수료 무료)` 계열을 actionable 원화 마켓 추가로 봅니다.
- 빗썸 최초 마켓 추가 공지에는 `거래 오픈 오후 ... 예정` 또는 `거래 개시 ...` 문구가 붙을 수 있으므로 이 문구만으로는 제외하지 않습니다.
- 빗썸의 후속 업데이트인 `시간 변경`, `연기`, `입출금`, `재거래지원`, `유의`, `중단`, `종료` 문구는 제외합니다.
- 같은 채널에서 같은 티커의 마켓 추가 공지가 제목 보강 형태로 다시 올라오면, Python poller 경로와 C++ ultra engine은 티커 단위 중복 guard로 두 번째 매수 시도를 막습니다.
- 실시간 backend에서 높은 message id의 일반 공지가 먼저 도착하고 낮은 message id의 상장 공지가 늦게 도착해도, 같은 실행 중에는 최근 처리 message id window로 중복만 막고 낮은 id의 신규 상장 공지는 처리합니다.
- 재시작 후에는 저장된 `last_seen_message_id`를 replay floor로 사용해 과거 메시지 재처리를 막습니다.
- `H`, `M` 같은 1글자 티커도 유효한 티커로 파싱합니다.
- Python/C++/Rust/TDLib native 경로는 같은 판정 의미를 유지해야 합니다.

사용 예시:

```bash
cd /Users/sueuncho/Documents/01_Trading/02-exchange-listing-sniper
../../.venv/bin/python main.py --exchange bithumb --no-telegram
../../.venv/bin/python main.py --exchange bithumb --no-trade
../../.venv/bin/python main.py --loop
../../.venv/bin/python main.py --realtime
../../.venv/bin/python main.py --realtime --realtime-backend race
../../.venv/bin/python main.py --realtime --realtime-backend telethon
../../.venv/bin/python main.py --realtime --realtime-backend tdlib
../../.venv/bin/python main.py --login-source-telegram
../../.venv/bin/python main.py --login-source-telegram --realtime-backend race
../../.venv/bin/python main.py --test-telegram
```

02 전용 텔레그램 env 키:

- `LISTING_TELEGRAM_BOT_TOKEN`
- `LISTING_TELEGRAM_CHAT_ID`

없으면 루트의 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`를 fallback으로 사용합니다.

실시간 소스 텔레그램 env 키:

- `LISTING_SOURCE_TELEGRAM_API_ID`
- `LISTING_SOURCE_TELEGRAM_API_HASH`
- `LISTING_SOURCE_TELEGRAM_PHONE`
- `LISTING_SOURCE_TELEGRAM_SESSION`

중요:

- 실시간 소스 수신에는 `BotFather`가 아니라 **텔레그램 유저 API (`API_ID`, `API_HASH`)** 가 필요
- 즉 Bot token은 소스 수신용이 아니라 **알림 발송용**
- 실시간 소스 테스트를 하려면 `my.telegram.org` 에서 `API_ID`, `API_HASH`를 만든 뒤 유저 세션 로그인을 해야 함

실시간 모드 사용 순서:

1. `.env`에 위 4개 중 최소 `API_ID`, `API_HASH`, `PHONE` 입력
2. `python main.py --login-source-telegram` 으로 유저 세션 로그인
3. `python main.py --realtime` 또는 `python main.py --loop` 실행

현재 기본 실시간 백엔드는 `race` 입니다. `telethon` 과 `tdlib` 는 단일 비교용/폴백으로 남겨둡니다.

실시간 저지연 경로:

- `--realtime` 또는 실시간 세션이 설정된 `--loop` 는 저지연 모드로 동작
- 핫패스 순서: `message event -> classify -> buy -> build signal`
- `detected_listing_posts.json` 저장과 시그널 JSON 저장은 백그라운드로 지연
- 02 전용 텔레그램 알림도 별도 워커에서 전송
- Bybit 정보는 네트워크 refresh 대신 cache-only snapshot 우선 사용
- 백그라운드 keep-warm 스레드가 Bybit 캐시와 fast executor를 주기적으로 유지

실전 실행 스크립트:

```bash
cd /Users/sueuncho/Documents/01_Trading/02-exchange-listing-sniper
./bin/check_tdlib_native_buy_realtime.sh
./bin/run_low_latency_realtime.sh
./bin/run_source_first_realtime.sh
./bin/run_fast_buy_realtime.sh
./bin/run_race_native_buy_realtime.sh
./bin/run_tdlib_native_buy_realtime.sh
```

이 스크립트는 다음을 강제합니다.

- `--realtime`
- `--strict-realtime`
- `BYBIT_FAST_EXECUTOR_ENABLED=1`
- `BYBIT_FAST_EXECUTOR_AUTO_BUILD=1`

소스 선점 최우선 스크립트 `run_source_first_realtime.sh` 는 다음을 강제합니다.

- `--realtime`
- `--strict-realtime`
- `--memory-state`
- `--source-only`
- `--no-telegram`
- `--state-flush-interval 0`
- raw source 이벤트 파일 저장은 기본적으로 하지 않음

즉 이 경로는 **텔레그램에서 새 글을 가장 빨리 잡는 것만 우선**하고, 분류/매수/알림은 뒤로 미루는 모드입니다.

실전 매수 스크립트 `run_fast_buy_realtime.sh` 는 다음을 강제합니다.

- `--realtime`
- `--realtime-backend race`
- `--strict-realtime`
- `--memory-state`
- `--ultra-buy`
- `--no-telegram`
- `BYBIT_SPOT_BUY_ENABLED=1`
- `BYBIT_WS_ORDER_ENABLED=0`
- `BYBIT_CPP_WS_EXECUTOR_ENABLED=0`
- `BYBIT_FAST_EXECUTOR_ENABLED=1`
- `BYBIT_QUERY_FILL_AFTER_BUY=0`
- `LISTING_CLASSIFIER_BACKEND=cpp` 기본값

즉 이 경로는 **KRW/원화 신규 상장 감지 직후 Bybit spot 시장가 매수 발사**를 최우선으로 두고, `감지 -> 매수 -> 즉시 리턴` 핫패스만 남깁니다. 주문 전송은 최신 실측 기준 winner인 **C++ REST fast executor**만 기본으로 사용해 Python buyer가 fallback loop가 아닌 `cpp-only` 최단 구현을 타게 합니다. C++ trade WebSocket 및 Python trade WebSocket은 명시적으로 켰을 때만 비교용/폴백으로 붙입니다. 주문 이후의 Bybit snapshot 조회, signal build, persistence, 로그는 백그라운드로 미뤄서 반환 경로를 더 얇게 유지합니다.

C++ REST fast executor도 자체 Bybit spot symbol cache를 warmup/keep-warm하고, `BUY` 처리 직전에 캐시에서 `TICKERUSDT`가 없으면 주문을 보내지 않습니다. 즉 race 경로에서도 “없으면 Bybit에 주문 시도하지 않음” 기준을 C++ 프로세스 안에서 빠르게 지킵니다.

복수 티커 공지가 race 경로로 들어오면 TDLib native winner는 relay 내부 native worker pool에서 바로 티커별 주문을 병렬 발사합니다. Telethon/Pyrogram winner도 먼저 C++ ultra engine이 복수 티커를 직접 처리하고, C++ ultra를 쓰지 못하는 fallback에서만 Python이 C++ fast executor `BUYBULK` 한 번으로 넘깁니다. 따라서 정상 ultra 경로에서는 Python에서 티커별 `BUY`를 순차 호출하지 않고, 두 번째 티커가 첫 번째 주문 응답을 기다리는 구조도 아닙니다.

fast executor의 최초 warmup은 Bybit spot symbol cache, bulk 주문용 worker, 병렬 curl client를 준비합니다. 이후 주기 keep-warm은 Python bridge lock을 오래 잡지 않도록 C++ 프로세스 안에서 background refresh만 예약하고 바로 반환합니다. 첫 복수 티커 주문에서 worker thread 생성이나 병렬 client의 첫 DNS/TLS 비용이 주문 발사 직전에 붙는 상황도 줄입니다. 주문용 curl handle은 초기 warmup에서만 데우고, background keep-warm은 symbol cache 전용 handle만 사용해 실제 주문 handle과 동시에 같은 easy handle을 쓰지 않습니다.

C++ fast executor의 명령 파싱은 `BUY`/`BUYBULK` frame을 고정 배열 `string_view`로 읽고, worker slot에 필요한 필드만 복사합니다. 주문 전 탭 split 단계에서 vector allocation이나 모든 필드의 `std::string` 복사를 하지 않습니다. Bybit spot symbol cache도 immutable snapshot을 보관하되 hot path에서는 raw pointer atomic만 읽어 `HAS`/주문 전 심볼 확인에서 `shared_ptr` refcount와 임시 문자열 생성을 피합니다. Bybit 인증 헤더도 고정 5개 구조로 구성하고, content-type/API-key/recv-window처럼 주문마다 변하지 않는 헤더는 새 문자열로 복사하지 않고 기존 문자열 포인터를 씁니다. 주문 body, auth plain text, sign/timestamp header는 thread-local scratch를 재사용하고 warmup/worker 시작 시 한 번 데워 첫 실주문 순간의 버퍼 할당도 줄입니다. HMAC은 API secret 기준 ipad/opad를 시작 시 미리 계산하고, 주문 시에는 signature 중간 문자열을 따로 만들지 않고 header에 바로 hex append합니다. fast executor의 주문 curl handle도 고정 옵션은 초기화 때 세팅하고, 주문마다 `curl_easy_reset`으로 DNS/TCP/TLS 관련 옵션을 다시 쌓지 않습니다. 주문 URL도 curl client 생성 시 한 번 만든 `/v5/order/create` 문자열을 재사용합니다. libcurl header list도 주문마다 `curl_slist_append/free`로 heap 할당하지 않고, 고정 5개 stack node를 요청 중에만 연결해 씁니다.

race ultra-buy에서 쓰는 C++ ultra engine은 keep-warm의 Bybit symbol refresh와 공지 처리 hot path를 분리합니다. refresh가 네트워크를 잡고 있는 순간에도 공지 처리 경로는 전역 mutex를 기다리지 않고, dedup에 필요한 짧은 락만 잡은 뒤 분류/매수로 들어갑니다. C++ ultra warmup은 복수 티커용 worker thread도 미리 띄워 첫 복수 티커 공지 순간에 thread 생성이 붙지 않게 합니다.

`run_race_native_buy_realtime.sh` 는 가장 공격적인 실전 수신 경로입니다. `race` 백엔드로 Telethon, TDLib, Pyrogram 중 먼저 온 이벤트를 Python/C++ fast buyer로 처리하면서, TDLib 쪽은 동시에 C++ relay 내부 native-buy도 켭니다. 즉 Telethon/Pyrogram이 먼저 받으면 기존 race 경로가 먼저 주문하고, TDLib가 먼저 받으면 Python 왕복 없이 C++ relay가 직접 주문합니다. 두 경로가 같은 공지를 동시에 잡아도 `ls-u-...` / `ls-b-...` orderLinkId를 공유하므로 Bybit 쪽 중복 orderLinkId 방어를 같이 씁니다. 대신 이 모드는 중복 주문 시도/거절 로그가 생길 수 있어, 가장 보수적인 단일 주문 경로가 필요하면 `run_tdlib_native_buy_realtime.sh` 를 씁니다.

race 실전 스크립트는 `LISTING_CPP_ULTRA_REQUIRE_WARMUP=1`과 `BYBIT_REQUIRE_FAST_EXECUTOR_WARMUP=1`도 켭니다. 그래서 Telethon/Pyrogram fallback이 먼저 이겼을 때 C++ ultra engine 또는 C++ fast executor가 빈 spot cache/cold order client 상태면 warning만 내고 감시를 시작하지 않고, 시작 전에 실패시킵니다. 또한 `LISTING_RACE_MIN_READY_BACKENDS=2`를 기본으로 둬서 TDLib 하나만 살아 있는 상태를 “race 준비 완료”로 취급하지 않습니다. 이 검사들은 startup gate라 공지 도착 후 주문 hot path에는 들어가지 않습니다.

실전 스크립트는 Bybit 서버 시간과 로컬 시간도 시작 전에 확인합니다. Bybit V5 인증 요청은 `X-BAPI-TIMESTAMP`가 Bybit 서버 시간 기준 허용 범위 안에 있어야 하므로, `bin/bybit_clock_gate.py`가 `/v5/market/time`을 한 번 호출해 clock skew가 너무 크면 감시를 시작하지 않습니다. 기본값은 `BYBIT_CLOCK_GATE_ENABLED=1`, `BYBIT_TIMESTAMP_BIAS_MS=-50`, `BYBIT_MAX_CLOCK_SKEW_MS=1000`, `BYBIT_CLOCK_AHEAD_MARGIN_MS=100`, `BYBIT_CLOCK_MAX_RTT_MS=1000`입니다. C++ REST, TDLib native REST, C++ ultra, Python REST, Python/C++ WS fallback 모두 주문 header timestamp에 같은 bias를 적용합니다. 이 검사는 startup-only라 텔레그램 공지 도착 후 주문 발사 hot path에는 들어가지 않고, 주문 순간에는 이미 파싱된 정수 bias를 millisecond timestamp에 더하는 비용만 있습니다. Bybit API가 막혔거나 Bybit time probe RTT가 비정상적으로 큰 배포 위치라면 이 단계에서 실패하므로, 빠르게 잡았지만 주문 endpoint가 거절되거나 너무 느린 상태를 시작 전에 알 수 있습니다.

실전 최속 스크립트는 `BYBIT_RESOLVE_DUPLICATE_ORDER_LINK_ID=0`도 기본으로 둡니다. C++ fast fallback이 이미 Bybit에서 duplicate orderLinkId 응답을 받은 뒤, 같은 orderLinkId를 다시 GET 조회해서 확인하는 후처리는 주문 응답 이후 작업이라 hot path 반환을 늦춥니다. 이 조회가 필요하면 env를 `1`로 올릴 수 있습니다.

기본 실전 스크립트는 `LISTING_CPP_ULTRA_ORDER_ON_CACHE_MISS=0`, `BYBIT_FAST_ORDER_ON_CACHE_MISS=0`, `LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS=0`으로 동작합니다. 즉 Telethon/Pyrogram winner, C++ ultra fallback, TDLib native-buy 모두 로컬 Bybit spot-symbol cache에서 `TICKERUSDT`가 확인될 때만 주문을 보냅니다. cache miss에서도 Bybit `/v5/order/create` 판단을 우선하고 싶으면 해당 env를 명시적으로 `1`로 올려야 합니다.

복수 티커가 Telethon/Pyrogram winner로 들어와도 C++ ultra engine은 각 티커를 직접 주문 경로까지 보냅니다. 다만 기본값은 strict cache gate라서 spot cache에 없는 티커는 주문하지 않습니다. C++ ultra를 사용할 수 없는 fallback에서 C++ fast executor `BUYBULK`를 타는 경우도 같은 기준을 씁니다.

극한 native TDLib 매수 스크립트 `run_tdlib_native_buy_realtime.sh` 는 다음을 강제합니다.

- `bin/tdlib_native_relay_watch.py` relay-only launcher
- `LISTING_TDLIB_NATIVE_BUY_ENABLED=1`
- `LISTING_TDLIB_NATIVE_KEEPWARM_INTERVAL=5`
- `LISTING_TDLIB_NATIVE_SYMBOL_REFRESH_INTERVAL=KEEP_WARM_INTERVAL`
- `LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED=1`
- `LISTING_TDLIB_NATIVE_BLOCKING_HOT_ORDER_WARMUP=1`
- `LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH=1`
- `LISTING_TDLIB_NATIVE_WORKER_SPIN_WAIT=1`
- `LISTING_TDLIB_NATIVE_WORKER_SPIN_COUNT=2`
- `LISTING_TDLIB_NATIVE_ORDER_START_SPIN_COUNT=64`
- `LISTING_TDLIB_NATIVE_PARALLEL_KEEPWARM_CLIENTS=4`
- `LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS=0`
- `LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH=data/tdlib_bybit_spot_symbols.txt`
- `LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MAX_AGE_SEC=300`
- `LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MIN_COUNT=100`
- `LISTING_TDLIB_NATIVE_TIMING_ENABLED=0`
- `LISTING_BYBIT_ORDER_RESPONSE_TIMEOUT_MS=250`
- `LISTING_TDLIB_WATCH_CHATS=-1002562064658:upbit_news,-1001202540487:BithumbExchange`
- `LISTING_TDLIB_SKIP_CLOCK_CALIBRATION=1`
- `LISTING_TDLIB_RECEIVE_TIMEOUT_SEC=0`
- `LISTING_TDLIB_FLUSH_LISTING_EVENTS=0`
- `LISTING_TDLIB_EMIT_LISTING_EVENTS=0`
- `LISTING_NATIVE_AUTO_BUILD=0`
- `LISTING_CPP_ULTRA_ENGINE_ENABLED=0`
- `LISTING_CLASSIFIER_BACKEND=python`

이 경로는 **TDLib C++ relay 내부에서 상장 판정 직후 Bybit spot 주문까지 직접 실행**합니다. 즉 단일 TDLib 수신 경로에서는 `TDLib C++ -> Python -> C++ ultra engine -> Bybit` 왕복을 빼고 `TDLib C++ -> Bybit`로 줄입니다. 최속 스크립트는 이제 `main.py`/`Poller`를 띄우지 않고, Python은 TDLib 인증, watch chat 설정, `__native_start__` 전송, 프로세스 유지까지만 담당합니다. 감시 시작 뒤에는 Python post 변환, state flush, signal/telegram 후처리, Python Bybit buyer 준비가 운영 경로에 없습니다. 기존 `main.py` 경로로 되돌려 진단하려면 `LISTING_TDLIB_NATIVE_USE_MAIN=1`을 명시합니다. 단, `race` 모드에서 native-buy를 켜면 Telethon/Pyrogram winner와 중복 주문 위험이 생길 수 있어서 이 스크립트는 `tdlib` 단독 백엔드만 사용합니다.

TDLib clock calibration은 latency 표시용 보정일 뿐 매수에는 필요하지 않으므로, ultra/trade-post 경로에서는 기본으로 건너뜁니다. 이 보정이 실패하면 감시 시작 전에 최대 몇 초를 잃을 수 있어서, 실전 핫패스에서는 raw relay timestamp를 쓰고 바로 watch chat 등록으로 넘어갑니다. Native-buy 내부 trade elapsed timestamp도 주문 전 clock call을 줄이기 위해 기본값은 off입니다. `native_trade.trade_elapsed_*` 필드를 정밀 측정해야 할 때만 `LISTING_TDLIB_NATIVE_TIMING_ENABLED=1`로 켭니다. `LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH=1`은 TDLib receive loop가 Bybit 응답 완료까지 기다리지 않고, worker가 `/v5/order/create` 전송 시작을 알리는 순간 `listingMatched`를 반환하게 합니다. 그래서 다음 텔레그램 이벤트 처리가 Bybit 응답 tail에 막히지 않습니다. 이 모드의 즉시 trade payload는 `reason=tdlib_native_rest_dispatched`, `executed=false`, `ret_code=-1`이며, 최종 Bybit 응답 proof가 아니라 “주문 전송 시작 완료” proof입니다. `LISTING_TDLIB_NATIVE_WORKER_SPIN_WAIT=1`은 첫 native-buy worker들을 `atomic_wait` sleep 대신 CPU spin 상태로 둬서 주문 dispatch 때 scheduler wake-up을 기다리지 않게 합니다. 기본 실전 스크립트는 현재 로컬 측정에서 단일/2개 티커 공지가 가장 낮게 나온 `LISTING_TDLIB_NATIVE_WORKER_SPIN_COUNT=2`로 첫 2개 ticker worker만 spin합니다. caller도 worker의 주문 전송 시작 신호를 바로 `atomic_wait`로 재우지 않고 `LISTING_TDLIB_NATIVE_ORDER_START_SPIN_COUNT=64`만큼 짧게 spin해서 wake-up 비용을 줄입니다. CPU를 더 쓰는 옵션이라 최속 실전 모드 전용입니다. `LISTING_BYBIT_ORDER_RESPONSE_TIMEOUT_MS=250`은 worker가 응답을 기다리는 최대 시간을 제한합니다. 주문 전송 시작 전 구간에는 들어가지 않지만, 느린 응답/네트워크 stall이 worker를 오래 붙잡는 시간을 제한합니다. 너무 낮은 값은 cold/reconnect 상황에서 요청 자체를 abort할 수 있으므로, 기본값은 응답 tail을 줄이되 hot keepalive 주문 전송 여지를 남기는 `250ms`로 둡니다. `LISTING_TDLIB_RECEIVE_TIMEOUT_SEC=0`은 TDLib relay receive loop를 non-blocking polling으로 돌려 receive-loop sleep 가능성을 없애는 대신 CPU를 더 씁니다. TDLib 단독 native-buy 스크립트는 `LISTING_TDLIB_FLUSH_LISTING_EVENTS=0`으로 주문 이후 `listingMatched` stdout flush도 핫 루프에서 빼고, `LISTING_TDLIB_EMIT_LISTING_EVENTS=0`으로 post-buy JSON 조립/pipe write 자체를 생략해 바로 다음 TDLib receive로 돌아갑니다. Emit-off 실전 모드에서는 receive thread가 worker의 주문 전송 시작 신호도 기다리지 않는 fire-and-forget dispatch를 사용합니다. worker가 spot cache 확인, 주문 body/signature 생성, `/v5/order/create` 전송을 그대로 수행하므로 없는 심볼을 사지 않는 조건은 유지하면서, TDLib receive loop는 주문 worker에 일을 넣은 직후 다음 update로 복귀합니다. Python 후처리/시그널 저장이 필요하면 emit을 `1`로 켜야 합니다. race 스크립트는 Telethon/Pyrogram fallback dedup을 위해 emit/flush를 기본 `1`로 유지합니다.

TDLib native-buy가 활성인 경우 Python 쪽 Bybit spot buyer와 C++ ultra-engine 객체 생성/warmup도 시작 경로에서 생략합니다. 이 스크립트에서는 Python native classifier/ultra shared library auto-build도 기본 off입니다. 해당 모드의 주문은 TDLib relay 내부 native buyer가 직접 처리하므로, Python fallback 준비가 감시 시작 전에 Bybit 네트워크, executor warmup, 추가 C++ 라이브러리 로딩을 잡고 있는 상황을 피합니다. TDLib relay에는 watch chat 등록, native listing 모드, native-buy 요청을 한 번에 켜는 `__native_start__` 명령을 사용합니다. 따라서 native-buy warmup/status 응답을 기다리는 짧은 시작 구간에 공지가 들어와도 C++ relay가 listing을 잡고, 주문 함수가 native buyer ready를 기다린 뒤 진행합니다.

TDLib native buyer는 Bybit spot symbol cache refresh가 성공할 때 `data/tdlib_bybit_spot_symbols.txt`에 심볼 목록을 저장합니다. 다음 실행에서 이 캐시가 300초 이내이고 최소 100개 이상의 심볼을 담고 있으면 전체 `/v5/market/instruments-info` refresh를 시작 전에 기다리지 않고 바로 캐시를 게시한 뒤 주문용 curl client warmup으로 넘어갑니다. 캐시가 없거나 오래됐거나 너무 작으면 기존처럼 Bybit에서 전체 spot symbol을 새로 받습니다. Keep-warm thread는 시작 직후에도 symbol cache를 먼저 refresh하고 그 다음 order client와 parallel order client를 refresh해서, strict cache 모드에서 stale/partial cache 때문에 실제 존재하는 `TICKERUSDT`를 놓치는 시간을 줄입니다. 실전 스크립트는 주문용 hot curl client keepalive를 기본 5초로 더 자주 돌리고, 전체 spot symbol refresh는 기존 `KEEP_WARM_INTERVAL` 기본 15초로 분리합니다. 이렇게 하면 주문 연결은 더 덜 식게 유지하면서도 전체 심볼 refresh가 매 5초마다 도는 background 잡음은 피합니다. 실전 스크립트는 `LISTING_TDLIB_NATIVE_BLOCKING_HOT_ORDER_WARMUP=1`도 켜서 `ready=true` 전에 hot primary/parallel order client를 한 번 동기 refresh합니다. 그래서 시작 직후 첫 공지가 오더라도 첫 실주문이 cold TLS/DNS를 탈 가능성을 줄입니다. 다만 background keep-warm이 공지 순간과 겹쳐 TDLib receive/order thread scheduling을 방해하지 않도록, 네트워크로 새로 데우는 parallel order client 수는 기본 4개로 제한합니다. worker와 주문 가능 티커 수는 그대로라서 5개 이상 복수 티커도 주문은 나가며, 초과 티커는 이미 priming된 base client를 사용합니다. 이 즉시 refresh는 `LISTING_TDLIB_NATIVE_IMMEDIATE_KEEPWARM_REFRESH=0`으로 끌 수 있지만 실전 최속 모드에서는 켜둡니다. libcurl이 지원하는 빌드에서는 `TCP_FASTOPEN`도 켜서 cold TCP 연결의 첫 요청 지연을 줄입니다.

복수 티커 공지는 C++ relay가 모든 티커를 추출하고, native-buy 활성 상태에서는 각 티커 주문을 병렬로 발사합니다. 즉 `SENT`, `ELSA` 같은 공지에서 두 번째 티커가 첫 번째 주문 응답을 기다린 뒤 나가는 구조가 아닙니다. native-buy worker와 worker별 curl client도 `ready=true`를 내보내기 전에 동기적으로 띄우고 데워 두기 때문에, 시작 직후 첫 복수 티커 공지 순간에도 `std::thread` 생성이나 worker client의 첫 DNS/TLS 비용이 주문 직전에 붙지 않게 합니다. 주문용 curl handle은 준비 단계에서만 데우고, background keep-warm은 symbol cache 전용 handle만 사용해 실제 주문 handle과 동시에 같은 easy handle을 쓰지 않습니다. 주문 curl handle은 매 주문마다 `curl_easy_reset`으로 고정 옵션을 다시 세팅하지 않고, URL/POST mode/body/header처럼 요청마다 필요한 값만 갈아끼웁니다. `/v5/order/create` URL, POST mode, order response callback은 warmup 때 먼저 priming해서 매수 순간 고정 setopt를 줄입니다. Bybit spot symbol cache도 C++ relay 내부 background keep-warm으로 주기 갱신하므로, 오래 켜둔 프로세스가 stale cache 때문에 실제 존재하는 `TICKERUSDT`를 놓칠 가능성을 줄입니다. Native Bybit buyer는 시작 시 `buy_enabled + api key + api secret + quote amount` 설정을 `config_ready` boolean으로 접어두고, 실주문 경로에서는 여러 문자열 상태를 매번 다시 확인하지 않습니다. Native Bybit 인증 헤더도 content-type/API-key/recv-window는 static string pointer로 재사용하고, 매 주문마다 바뀌는 sign/timestamp header만 새로 만듭니다. API key + recv-window 서명 고정 조각과 주문 body의 고정 조각도 시작 시 미리 만들어 주문 순간 문자열 조립을 줄입니다. 주문 body, auth plain text, sign/timestamp header는 thread-local scratch를 재사용하고 warmup 및 worker 시작 시 scratch를 데워 첫 실주문 순간의 heap allocation을 줄입니다. HMAC은 API secret 기준 ipad/opad를 시작 시 미리 계산하고, signature는 중간 문자열을 만들지 않고 `X-BAPI-SIGN` header에 바로 hex append합니다. 주문 URL은 client 생성 시 만든 `/v5/order/create` 문자열을 재사용해 매수 순간 `base_url + path` 조립을 피합니다. libcurl header list도 매 주문마다 `curl_slist_append/free`를 하지 않고 stack node 5개를 연결해 heap 할당을 피합니다. `orderLinkId`는 실제 주문 시도 직전에만 구성하고, buy disabled/spot unavailable 같은 no-order 경로에서는 만들지 않습니다. Native TDLib와 Python/race C++ executor는 같은 공지에 대해 같은 `ls-u-...` / `ls-b-...` orderLinkId prefix를 쓰므로, 실험 중 두 경로가 겹쳐도 Bybit 중복 orderLinkId 방어를 공유합니다. 주문 성공 경로에서는 로그/결과용 `NativeTradeResult` 전체 객체 구성도 주문 body/auth와 libcurl send 준비가 끝난 뒤로 늦춰, 매수 요청 발사 전 구간에 후처리 문자열 초기화가 끼지 않게 합니다.

실전 재시작 시간을 더 줄이기 위해 최속 실행 스크립트는 현재 검증된 공식 TDLib chat id를 기본값으로 넣습니다.

```bash
LISTING_TDLIB_WATCH_CHATS=-1002562064658:upbit_news,-1001202540487:BithumbExchange
```

이 값이 있으면 시작 시 `searchPublicChat` 왕복 없이 바로 C++ relay에 watch 대상 chat id를 전달합니다. 공지 수신 이후의 매수 핫패스는 원래도 username resolve를 타지 않지만, 실전 프로세스 재시작/복구 때 Telegram resolve 대기 때문에 감시 시작이 늦어지는 위험을 줄입니다. 만약 공식 채널이 바뀌는 특수 상황이 생기면 `LISTING_TDLIB_WATCH_CHATS`를 명시적으로 덮어쓰면 됩니다.

env를 직접 넣지 않아도 한 번 resolve에 성공하면 `data/tdlib_watch_chats.json`에 chat id를 저장하고 다음 실행부터 재사용합니다. `data/*`는 gitignore라 이 로컬 런타임 캐시는 커밋 대상이 아닙니다.

C++ relay 안에서는 watch chat 목록을 immutable raw-pointer snapshot으로 들고, lookup은 `unordered_map` 대신 고정 배열 비교로 처리합니다. control thread가 chat id 목록을 갱신해도 receive hot path가 map mutation과 충돌하지 않고, 업비트/빗썸 두 채널 확인에 해시/bucket 비용을 쓰지 않습니다. channel handle -> exchange 매핑도 watch 설정 시점에 enum으로 미리 계산해 두므로, 감시 채널 메시지마다 `upbit_news`/`BithumbExchange` 문자열 비교를 다시 하지 않습니다. listing classifier도 이 enum으로 바로 분기합니다. TDLib JSON도 보통 `{"@type":"updateNewMessage"...}`로 시작하므로 prefix fast path로 먼저 판별하고, top-level type이 다른 TDLib update는 fallback full scan 없이 바로 버립니다. message가 아닌 TDLib update는 watch-chat snapshot을 읽기 전에 버려서, 장시간 감시 중 잡음 이벤트가 listing hot path 자원을 덜 건드립니다. 실제 message update에서는 `message_id`, `chat_id`, `content` 위치를 TDLib message envelope fast path에서 한 번에 뽑고, compact `messageText -> formattedText -> text` 제목은 prefix fast path로 바로 view를 잡습니다. 형태가 다를 때만 기존 pattern scan fallback을 탑니다. Native relay의 Bybit spot symbol cache도 immutable snapshot을 보관하고 hot path에서는 raw pointer atomic만 읽어 주문 전 `TICKERUSDT` 확인을 `string_view`로 바로 수행합니다.

`--memory-state` 는 dedup/state를 메모리 우선으로 처리하고, 디스크 flush는 뒤로 미룹니다. 실시간 핫패스에서는 `StateStore` 락과 파일 동기화를 건드리지 않고, close 또는 주기 flush 때만 상태 파일을 맞춥니다. 실행 중 dedup은 채널별 last-seen 하나가 아니라 exact message_id LRU를 쓰고, flush 때 최근 seen id 목록도 같이 저장합니다. 그래서 같은 실행 중 더 큰 message_id가 먼저 들어왔다는 이유만으로 아직 처리하지 않은 낮은 message_id 공지를 바로 버리지 않습니다. 재시작 후에는 저장된 `last_seen_message_id`를 replay floor로 사용해 과거 글 재처리를 막고, 저장된 최근 seen id 목록은 이미 처리한 최신 글의 중복 방지에 씁니다.

로컬 벤치마크:

```bash
cd /Users/sueuncho/Documents/01_Trading/02-exchange-listing-sniper
../../.venv/bin/python bin/benchmark_latency.py --iterations 2000
../../.venv/bin/python bin/benchmark_cpp_ultra_preflight.py --iterations 100000
./bin/tdlib_json_relay --benchmark-tdlib-message-disabled 20000
./bin/tdlib_json_relay --benchmark-tdlib-message-buy-preflight 20000
./bin/tdlib_json_relay --benchmark-tdlib-message-buy-preflight-upbit 20000
./bin/tdlib_json_relay --benchmark-tdlib-message-buy-preflight-multi 20000
./bin/tdlib_json_relay --benchmark-tdlib-message-emit-preflight 20000
./bin/tdlib_json_relay --benchmark-native-order-prepare 20000
./bin/tdlib_json_relay --benchmark-native-order-curl-prepare 20000
./bin/tdlib_json_relay --benchmark-native-buy-preflight 20000
./bin/tdlib_json_relay --benchmark-native-buy-preflight-multi 20000
./bin/tdlib_json_relay --benchmark-native-async-fire-and-forget 20000
./bin/tdlib_json_relay --benchmark-tdlib-message-fire-and-forget 20000
./bin/tdlib_json_relay --benchmark-tdlib-type-filter 20000
./bin/tdlib_json_relay --benchmark-curl-header-list 20000
```

현재 스크립트 출력 항목:

- `build_post_full`: 실시간 full post 변환 비용
- `build_post_trade`: ultra-buy용 title-only trade post 변환 비용
- `build_post_minimal`: 실시간 minimal post 변환 비용
- `python_classifier`: Python 제목 분류 비용
- `race_gate_unique`: race backend가 처음 보는 `(channel, message_id)`를 채택하는 비용
- `race_gate_duplicate`: race backend 중복 이벤트를 버리는 비용
- `process_post_cpp_ultra_fire`: race winner post가 poller의 no-ack C++ ultra hot path로 들어가 seen mark와 C++ raw 호출, background handoff까지 끝내고 반환하는 비용
- `process_post_tdlib_native_trade_skip`: TDLib native-buy가 이미 주문한 `listingMatched`를 Python poller가 다시 C++ ultra/fast buyer로 보내지 않고 state mark와 background handoff만 하고 반환하는 비용
- `process_post_cpp_ultra_native_disabled`: 실제 `liblisting_ultra_engine` shared library를 호출하되 Bybit buy disabled 상태에서 C++ 분류/티커 추출/결과 반환까지 측정하는 비용
- `cpp_ultra_order_preflight`: race winner가 C++ ultra engine에 들어온 뒤 제목 분류, 티커 추출, `orderLinkId`, 주문 body/auth, libcurl setopt까지 진행하고 실제 `curl_easy_perform` 직전에 멈추는 비용
- `cpp_ultra_multi_order_preflight`: Telethon/Pyrogram winner의 복수 티커 공지를 C++ ultra engine이 Python bulk fallback 없이 worker pool로 처리하고 각 주문을 실제 `curl_easy_perform` 직전에 멈추는 비용

수치는 머신 부하, Python 버전, native library 상태에 따라 달라지므로 README에 고정하지 않는다. 최신 수치는 위 명령으로 다시 뽑는다.

`tdlib_json_relay --benchmark-tdlib-message-disabled` 는 TDLib `updateNewMessage` JSON이 C++ relay에 들어온 뒤 제목 분류, 티커 추출, native-buy disabled gate, `listingMatched` emit까지의 로컬 hot path를 반복 측정한다. 실제 Telegram publish 지연이나 Bybit 네트워크 주문 왕복은 포함하지 않는다.

`tdlib_json_relay --benchmark-tdlib-message-buy-preflight` 는 TDLib `updateNewMessage` JSON 수신, watch chat 확인, 제목 view 추출, 상장 분류, 티커 추출, `TICKERUSDT` spot 확인, 주문 body/auth, libcurl setopt까지 한 번에 잇고 실제 `curl_easy_perform` 직전에서 멈춘다. 즉 Telegram publish 지연과 Bybit 네트워크 왕복을 뺀 `로컬 수신 -> 주문 발사 직전` 통합 hot path를 보는 벤치다.

`tdlib_json_relay --benchmark-tdlib-message-buy-preflight-upbit` 는 같은 통합 hot path를 업비트 `[거래] ... 신규 거래지원 안내 (KRW 마켓)` 제목으로 측정한다. 빗썸과 업비트 제목 규칙이 달라 한쪽만 빨라지는 최적화를 걸러내기 위한 비교 벤치다.

`tdlib_json_relay --benchmark-tdlib-message-buy-preflight-multi` 는 빗썸 복수 티커 공지를 TDLib JSON 수신부터 native worker pool의 복수 주문 preflight까지 한 번에 측정한다. `SENT`, `ELSA` 같은 공지에서 두 번째 티커가 첫 번째 주문을 기다리지 않는지 확인하는 통합 벤치다.

`tdlib_json_relay --benchmark-tdlib-message-emit-preflight` 는 native-buy preflight 이후 `listingMatched` JSON emit까지 포함한다. 주문 발사 전 병목은 아니지만, TDLib receive loop가 다음 이벤트를 빨리 처리할 수 있는지 보는 후처리 비용 벤치다.

`tdlib_json_relay --benchmark-native-order-prepare` 는 Bybit 네트워크를 호출하지 않고, spot symbol cache 확인, `orderLinkId` 생성, 주문 JSON body 생성, HMAC 서명, auth header 생성까지만 반복 측정한다. 즉 실제 주문 발사 직전 CPU 구간을 따로 보는 벤치다.

`tdlib_json_relay --benchmark-native-order-curl-prepare` 는 위 주문 준비에 더해 주문용 curl handle이 `/v5/order/create` URL/POST mode로 priming된 상태에서 stack auth header list, `CURLOPT_POSTFIELDS`, `CURLOPT_POSTFIELDSIZE` 설정까지 하고, 실제 `curl_easy_perform` 직전에 멈춘다. 네트워크 주문 왕복 없이 REST 발사 직전 세팅 비용을 분리해서 보는 벤치다.

`tdlib_json_relay --benchmark-native-buy-preflight` 는 실제 `buy_listing` 경로를 타되 네트워크 주문 대신 `curl_easy_perform` 직전에서 멈춘다. active/config gate, `TICKERUSDT` 조립, spot symbol cache 확인, `orderLinkId`, 주문 body/auth, libcurl setopt까지 포함한 주문 발사 직전 전체 로컬 경로를 보는 벤치다. no-network preflight의 결과 반환 비용은 수치에 포함되지만, 실전 성공 경로에서는 로그/결과 객체의 큰 구성은 주문 send 준비 뒤로 미룬다.

`tdlib_json_relay --benchmark-native-buy-preflight-multi` 는 `SENT`, `ELSA` 같은 복수 티커 공지를 가정하고 native worker pool로 두 주문을 병렬 preflight한다. worker dispatch, 티커별 `TICKERUSDT` 확인, 주문 body/auth, libcurl setopt가 포함되며 실제 네트워크 주문 왕복은 제외된다.

`tdlib_json_relay --benchmark-tdlib-type-filter` 는 message가 아닌 TDLib update를 버릴 때 예전 full-scan 방식과 현재 top-level type fast-reject 방식을 같은 바이너리에서 비교한다. 지속 감시 중 잡음 이벤트가 쌓일 때 event loop가 상장 메시지를 늦게 집지 않도록 보는 벤치다.

`tdlib_json_relay --benchmark-curl-header-list` 는 실제 네트워크 없이 주문 HTTP header list를 예전 heap `curl_slist_append/free` 방식과 현재 stack node 방식으로 각각 반복 측정한다. 주문 발사 직전 libcurl header 연결 비용만 분리해서 보는 벤치다.

네이티브 분류기:

```bash
cd /Users/sueuncho/Documents/01_Trading/02-exchange-listing-sniper
bash ./bin/build_native_classifiers.sh
../../.venv/bin/python ./bin/benchmark_native_classifier.py --iterations 100000 --skip-build
```

- `LISTING_CLASSIFIER_BACKEND=cpp|rust|auto|python` 으로 강제 가능
- 실전 매수 스크립트는 native hot path와 맞추기 위해 `cpp`를 기본값으로 둔다
- `auto` 는 저장된 winner 캐시가 있으면 그 값을 쓰고, 없으면 C++/Rust 중 더 빠른 쪽을 한 번 측정해서 고릅니다
- C++/Rust native backend는 로드 직후 빗썸 수수료 이벤트, 1글자 티커, 거래 오픈 시간 변경 negative canary를 통과해야 선택됩니다. 소스는 최신인데 오래된 `.dylib`가 남아 있으면 해당 backend를 무시하고 Python 또는 다른 native backend로 fallback합니다.
- Python classifier가 정확도 기준입니다. 일반 poller와 `make_listing_title_classifier()` 경로는 native backend가 예외를 내거나 제목을 못 잡으면 같은 제목을 Python classifier로 다시 확인합니다. 즉 native는 가속 경로이지, Python이 잡을 수 있는 상장 공지를 놓치게 하는 단일 판정 지점이 아닙니다.
- TDLib relay 내부 native classifier도 같은 fixture를 기준으로 검증합니다. `./bin/tdlib_json_relay --classify-title bithumb "[마켓 추가] 밈코어(M) 원화 마켓 추가"` 는 relay 내부 판정만 JSON으로 출력하고, `tests/test_tdlib_relay_process_inject.py` 가 이 CLI를 `tests/fixtures/listing_title_cases.json` 전체에 대해 실행합니다.
- 실전 매수 시작 전에는 `bin/verify_listing_classifiers.py --require-tdlib-relay`가 Python 기준 classifier, 기본 `make_listing_title_classifier()` 경로, TDLib relay `--classify-title` 경로를 같은 golden fixture로 비교합니다. `run_tdlib_native_buy_realtime.sh`, `run_race_native_buy_realtime.sh`, `run_fast_buy_realtime.sh`, `run_fast_buy_cpp_realtime.sh`, `check_tdlib_native_buy_realtime.sh`는 relay auto-build 뒤 이 검사를 기본으로 실행하고 실패하면 감시를 시작하지 않습니다. 진단 목적으로만 `LISTING_CLASSIFIER_VERIFY=0`으로 끌 수 있습니다. `run_source_first_realtime.sh`는 분류/매수 없이 raw 소스 수신만 보는 모드라 이 classifier gate를 실행하지 않습니다.
- relay가 emit한 `listingMatched.tickers`는 `src/tdlib_realtime_client.py`의 Python post 변환에서 `native_listing.tickers`로 보존되고, poller가 이를 티커별 주문/후처리로 확장합니다. `tests/test_tdlib_realtime_client.py` 와 `tests/test_poller_startup.py`가 이 경계를 고정합니다.

`benchmark_native_classifier.py` 는 Python/C++/Rust 분류기를 같은 제목 세트로 비교한다. native winner를 `data/native_classifier_benchmark.json`에 저장하려면 `--write-cache`를 추가한다. `data/*`는 gitignore라 로컬 캐시로만 남는다.

실제 Telegram ingest 비교:

```bash
cd /Users/sueuncho/Documents/01_Trading/02-exchange-listing-sniper
../../.venv/bin/python bin/benchmark_live_ingest.py bench --iterations 24 --timeout 20 --pause-sec 0.75
../../.venv/bin/python bin/benchmark_live_ingest.py bench --backend tdlib --native-listing --iterations 1 --timeout 60
../../.venv/bin/python bin/benchmark_live_ingest.py bench --backend tdlib --native-buy-ready --iterations 1 --timeout 10
../../.venv/bin/python bin/benchmark_live_ingest.py bench --backend tdlib --native-preflight-inject --iterations 3 --timeout 10
../../.venv/bin/python bin/benchmark_live_ingest.py bench --backend tdlib --native-file-order-inject --iterations 3 --timeout 10
../../.venv/bin/python bin/tdlib_symbol_cache.py check
../../.venv/bin/python bin/tdlib_symbol_cache.py refresh
../../.venv/bin/python bin/trading_config_gate.py check
../../.venv/bin/python bin/verify_listing_classifiers.py --require-tdlib-relay
../../.venv/bin/python bin/race_fallback_readiness.py check
../../.venv/bin/python bin/fast_readiness_gate.py --live-inject --require-symbol-cache --require-trading-config --race-fallback-warmup
../../.venv/bin/python bin/fast_readiness_gate.py --strict-live-tdlib
../../.venv/bin/python bin/fast_readiness_gate.py --strict-live-race
./bin/check_tdlib_native_buy_realtime.sh
```

이 명령은 로그인된 Telegram user session이 있을 때만 live event를 기다린다. 세션이 없거나 TDLib startup/native-buy readiness가 실패하면 `ok=false`와 이유를 출력하고 끝난다. 기본 명령은 raw ingest 이벤트를 보고, `--native-listing` 명령은 TDLib C++ relay native listing 경로를 켜되 live buy는 강제로 비활성화한 상태에서 실제 상장 공지만 기다린다. `--native-buy-ready` 명령은 TDLib C++ relay native-buy startup/readiness까지 켜지만 Bybit base URL을 localhost로 강제해 실제 Bybit 주문은 절대 보내지 않는다. 따라서 실전 주문 전 `__native_start__` readiness를 live Telegram 세션/공식 채널 watch와 같이 점검할 수 있다. `--native-preflight-inject` 명령은 live TDLib auth와 공식 채널 chat id를 사용한 뒤, synthetic 업비트/빗썸 listing update를 C++ relay에 직접 주입해서 native preflight trade가 생성되는지 확인한다. 이 모드는 단일 업비트/빗썸 공지와 복수 티커 빗썸 공지를 같이 확인하고, `curl_easy_perform` 직전에서 멈추므로 실제 Bybit 주문은 보내지 않지만, 공식 channel id mapping과 C++ native listing/preflight 경로를 한 프로세스에서 검증한다. `--native-file-order-inject` 명령은 포트 listen 없이 `file://` mock Bybit order endpoint를 써서 같은 TDLib native 경로가 `curl_easy_perform`과 주문 응답 파싱까지 지나 `executed=true`를 만드는지 확인한다. `trading_config_gate.py check`는 실제 live buy에 필요한 Bybit API key/secret, `BYBIT_SPOT_BUY_ENABLED`, 양수 USDT 금액을 secret 출력 없이 확인한다. `tdlib_symbol_cache.py check`는 실전 strict spot-cache가 300초 이내인지 확인하고, `tdlib_symbol_cache.py refresh`는 주문 없이 Bybit public spot instruments만 받아 `data/tdlib_bybit_spot_symbols.txt`를 미리 만든다. `run_tdlib_native_buy_realtime.sh`와 `run_race_native_buy_realtime.sh`는 기본적으로 live watch 시작 전에 trading config와 이 캐시를 확인하고, 캐시가 없거나 stale이면 refresh를 먼저 시도한다. refresh 후에도 cache가 준비되지 않으면 감시를 시작하지 않는다. `check_tdlib_native_buy_realtime.sh`는 `run_tdlib_native_buy_realtime.sh`와 같은 최속 TDLib env를 세팅한 뒤 `fast_readiness_gate.py --strict-live-tdlib`를 실행하는 실전 전용 preflight다. watcher를 시작하지 않으므로 gate 시간이 공지 도착 후 hot path에 들어가지 않는다. `race_fallback_readiness.py check`는 Telethon/Pyrogram winner가 먼저 들어오는 경우를 위해 C++ ultra engine과 C++ fast executor warmup도 실전 시작 전 확인한다. 이 검사들은 시작 전 one-time gate라 공지 도착 후 hot path에는 추가 비용을 넣지 않는다. `fast_readiness_gate.py`는 trading config, symbol cache, Bybit clock, native file-order self-test, 단일/복수 티커 native 벤치, 업비트/빗썸 TDLib message 벤치, emit-off fire-and-forget TDLib message 벤치, TDLib non-message fast-reject 벤치, 선택적 live-safe inject, 선택적 race fallback warmup을 한 번에 JSON으로 묶는다. `--strict-live-tdlib`는 live TDLib-native 실전 시작 기준으로 trading config, Bybit spot-symbol cache refresh/check, Bybit clock, live-safe file-order inject를 모두 required로 올린다. `--strict-live-race`는 여기에 C++ ultra/C++ fast race fallback warmup까지 required로 추가한다. Timing을 켠 검증 출력에서는 `receive_to_last_order_send_started_us`가 TDLib relay 수신부터 주문 `curl_easy_perform` 직전까지의 구간이고, `receive_to_last_trade_finished_us`는 mock 주문 응답/파싱까지 포함한 구간이다. `fast_readiness_gate.py --live-inject`는 `--max-live-order-send-us`와 `--max-live-trade-finished-us` 둘 다 확인해서 주문 발사 전 구간과 주문 응답 대기 tail 회귀를 따로 잡는다. 출력의 `published_to_received_ms`, `published_to_callback_ms`, `receive_to_callback_us`는 Telegram publish timestamp와 로컬 receipt/callback 경계를 분리해서 보는 값이다. 단 Telegram message date는 초 단위라 `published_to_*`에는 최대 약 1초 양자화 오차가 있다. 실전 런타임은 Telethon + TDLib + Pyrogram을 동시에 붙여 먼저 도착한 쪽을 채택하는 `race` 모드를 기본값으로 둔다. race dedup은 채널의 마지막 message_id 하나가 아니라 `(channel_handle, message_id)` 단위로 처리하므로, 두 공지가 아주 빠르게 이어질 때 뒤 메시지를 먼저 본 백엔드 때문에 앞 메시지를 버리는 상황을 피한다. poller의 memory-state dedup도 시작 시 저장된 마지막 message_id보다 오래된 과거 글만 버리고, 실행 중에는 exact message_id LRU로 중복만 제거하므로 out-of-order 수신 때문에 아직 처리하지 않은 낮은 message_id 공지를 버리지 않는다.

실전 주문 이후에는 signal JSON 저장 전에 최소 trade proof가 `data/trade_proofs/YYYYMMDD_native_trades.jsonl`에 먼저 append된다. 이 작업은 주문 이후 background finalize에서만 실행되므로 감지→매수 hot path를 막지 않는다. `LISTING_TDLIB_NATIVE_TIMING_ENABLED=1`을 켜면 proof에 `receive_to_trade_finished_*`도 들어가지만, 기본 최속 모드에서는 주문 전 clock call을 줄이기 위해 timing을 끈다. `data/*`는 gitignore 대상이라 실제 주문 proof와 order id가 커밋되지 않는다.

실제 Bybit 주문 transport 비교 (`BTCUSDT`, `10 USDT`, 메인넷 왕복 정리) 기준:

- `python_ws` buy ACK `304.668ms`
- `cpp_ws_trade` buy ACK `296.122ms`
- `cpp_fast_path` buy ACK `292.275ms`
- current winner: `cpp_fast_path`

즉 최신 실거래 비교에서는 **C++ REST fast executor가 Python WebSocket, C++ WebSocket보다 조금 더 빨랐고**, 현재 본선 매수 경로도 그 winner를 기본값으로 사용합니다.

Linux VPS 배포:

- 가이드: [deploy/linux/README.md](deploy/linux/README.md)
- 서비스 파일: [02-exchange-listing-sniper.service](deploy/linux/02-exchange-listing-sniper.service)

Bybit 자동매수 env 키:

- `BYBIT_API_KEY`
- `BYBIT_API_SECRET`
- `BYBIT_API_BASE_URL`
- `BYBIT_FAST_EXECUTOR_ENABLED`
- `BYBIT_FAST_EXECUTOR_AUTO_BUILD`
- `BYBIT_FAST_EXECUTOR_PATH`
- `BYBIT_FAST_EXECUTOR_BUILD_SCRIPT`
- `BYBIT_WS_ORDER_ENABLED`
- `BYBIT_WS_TRADE_URL`
- `BYBIT_SPOT_BUY_ENABLED`
- `BYBIT_SPOT_BUY_USDT_AMOUNT`
- `BYBIT_SPOT_BUY_MODE`
- `BYBIT_QUERY_FILL_AFTER_BUY`
- `BYBIT_RECV_WINDOW`
- `BYBIT_TIMESTAMP_BIAS_MS`
- `BYBIT_CLOCK_GATE_ENABLED`
- `BYBIT_MAX_CLOCK_SKEW_MS`
- `BYBIT_CLOCK_AHEAD_MARGIN_MS`
- `BYBIT_CLOCK_MAX_RTT_MS`
- `BYBIT_CLOCK_GATE_TIMEOUT_SEC`

주문 방식:

- Bybit spot `Market Buy`
- 기본값은 `marketUnit=quoteCoin`
- 즉 주문 전에 ask 조회/수량 계산을 하지 않고, 설정한 USDT 금액으로 바로 주문
- `BYBIT_QUERY_FILL_AFTER_BUY=false` 기본값이라 주문 직후 추가 fill 조회도 생략
- 중복 방지를 위해 `orderLinkId=ls-u-message_id-ticker` 또는 `ls-b-message_id-ticker` 형식 사용

C++ fast path:

- 소스: [bybit_fast_path.cpp](cpp/bybit_fast_path.cpp)
- 빌드: [build_fast_path.sh](cpp/build_fast_path.sh)
- 역할: Bybit spot 심볼 캐시 + keep-alive HTTP + `order/create` 직접 호출
- Python 쪽은 감지 후 C++ 프로세스에 `BUY` 명령만 전달

C++ trade WebSocket path:

- 소스: [bybit_ws_trade_path.cpp](cpp/bybit_ws_trade_path.cpp)
- 빌드: [build_ws_trade_path.sh](cpp/build_ws_trade_path.sh)
- 역할: Bybit trade WebSocket order entry를 C++ 프로세스로 유지
- 현재 용도: 최신 실거래 비교용 및 fallback transport

공지 형식 메모:

- 업비트 신규 상장형 제목은 최근 확인분 기준 `코인명(TICKER)` 패턴을 포함
- 빗썸 마켓 추가형 제목도 최근 확인분 기준 `코인명(TICKER)` 패턴을 포함
- 다만 업비트 `[거래]` 카테고리 안에는 `유의 촉구`, `거래 유의 종목`도 있어서, 괄호만 보고 매수하면 안 되고 현재처럼 키워드 필터가 필요
