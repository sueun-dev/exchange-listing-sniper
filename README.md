# Exchange Listing Sniper

Low-latency bot that watches the official Telegram channels of Korean exchanges
(**Upbit** `@upbit_news`, **Bithumb** `@BithumbExchange`), detects new KRW/원화
spot-listing announcements the instant they post, and places an immediate Bybit
spot market buy of the same ticker.

The thesis: when a Korean exchange announces a **new KRW listing** for a coin that
already trades on Bybit, the announcement tends to pump the price. The bot
front-runs that move by buying on Bybit within milliseconds of the announcement.

---

> ⚠️ **Disclaimer.** This is a personal research/automation project, **not financial
> advice**. It places **real market orders with real money**. Crypto trading carries
> substantial risk of loss; a low-liquidity new listing can move violently in either
> direction, and the bot has **no built-in stop-loss or auto-sell**. You are solely
> responsible for your own API keys, funds, configuration, and outcomes. Run it
> **disabled / paper-mode first**, start with a small size, and never risk money you
> can't afford to lose. No warranty of any kind.

---

## How it works

```
Telegram announcement
        │  (MTProto, ~ms)
        ▼
  Race ingest  ── TDLib (C++) + Pyrogram + Telethon, first-arrival wins, auto-reconnect
        │
        ▼
  First-arrival gate  ── (channel, message_id) dedup across backends
        │
        ▼
  3-layer dedup  ── replay-floor (restart) · message-id window · per-ticker guard
        │
        ▼
  Classify  ── Upbit/Bithumb rules, identical verdict across Python/C++/Rust/ultra/relay
        │
        ▼
  Buy  ── Bybit spot market order ({TICKER}USDT, quoteCoin), idempotent orderLinkId
        │
        ▼
  Background finalize → persist receipt → Telegram alert
```

The hot path (receive → order-ready) is a few **microseconds**; classification is
sub-microsecond. End-to-end latency is dominated almost entirely by **network
propagation to Bybit** (~tens of ms; co-locate near the matching engine to minimize).

### What counts as an actionable listing

- **Upbit** — `[거래] …(TICKER) KRW 마켓 디지털 자산 추가` or `…신규 거래지원 안내 (KRW 마켓)`.
  Excluded: BTC/USDT-only adds, deposits/withdrawals, circulation, caution, events,
  delistings, schedule-change follow-ups.
- **Bithumb** — `[마켓 추가] …(TICKER) 원화 마켓 추가` family (incl. fee-event and
  symbol-rename re-announcements). Excluded: time-change, postpone, deposit, re-listing,
  caution, suspension, delisting.
- A coin is only bought if `{TICKER}USDT` actually exists on Bybit spot; otherwise it
  is skipped (no order, no loss).

## Features

- **Correctness, proven across 5 implementations.** Python, the C++ classifier, the Rust
  classifier, the C++ ultra engine, and the TDLib relay all return an identical verdict
  for every title (enforced by golden fixtures + `bin/verify_listing_classifiers.py`).
  Validated against ~1,000 real announcements with zero misses / zero wrong buys.
- **No double-buys.** Three dedup layers (replay floor, bounded message-id window,
  unbounded per-`(channel, ticker)` guard) plus a deterministic `orderLinkId` that Bybit
  itself rejects duplicates on. Ambiguous send timeouts do not re-fire on another transport.
- **Money safety.** A hard ceiling (`BYBIT_SPOT_BUY_MAX_USDT_AMOUNT`) refuses any
  configured amount above it or non-finite, so a fat-finger / extra-zero can't place an
  oversized order. Optional split of a fixed amount across multiple tickers in a
  multi-coin listing.
- **Stable 24/7 ingest.** Each realtime backend auto-reconnects on drop (exponential
  backoff + jitter); a deployed instance runs under `systemd Restart=always`.
- **Pluggable order transports.** C++ fast path (libcurl), C++ WebSocket, Python
  WebSocket, REST — selectable and falling back in order.

## Tech stack

Python 3.9 orchestration · C++20 (ultra engine, TDLib JSON relay, fast/WS order paths) ·
Rust (classifier) · TDLib + Pyrogram + Telethon (MTProto ingest) · Bybit V5 API.

## Repository layout

```
main.py                     CLI entry point + run-mode dispatch
src/                        poller, classifier, realtime clients, Bybit buyer, state store, ...
cpp/                        ultra engine, TDLib relay, fast/WS order paths (+ build scripts)
rust/listing_classifier/    native Rust classifier
bin/                        run scripts, readiness gates, native build helpers
tests/                      pytest suite + golden classifier fixtures
deploy/linux/               systemd unit
config/channels.json        watched channels
.env.example                configuration template (copy to .env)
```

Compiled native artifacts (`bin/tdlib_json_relay`, `bin/*.dylib`/`*.so`, etc.) are
gitignored and built on the target machine.

## Setup

```bash
# 1. Python env + runtime deps
python3.9 -m venv .venv
.venv/bin/pip install httpx Telethon Pyrogram TgCrypto python-dotenv aiohttp \
    websocket-client uvloop

# 2. Build native artifacts (needs clang++ with C++20, Rust/cargo, libcurl, OpenSSL,
#    and TDLib for the relay)
bash bin/build_native_classifiers.sh    # C++ + Rust classifiers, ultra engine
bash cpp/build_tdlib_relay.sh           # TDLib JSON relay (requires TDLib)
bash cpp/build_fast_path.sh
bash cpp/build_ws_trade_path.sh

# 3. Configuration
cp .env.example .env                     # then fill in your own values

# 4. Telegram source session (MTProto user API — created at my.telegram.org)
.venv/bin/python main.py --login-source-telegram --realtime-backend race
```

### Configuration (`.env`)

The realtime **source** requires a Telegram **user** API (`API_ID` / `API_HASH` from
my.telegram.org), not a bot token. The bot token is only for sending **alerts**.

| Key | Purpose |
| --- | --- |
| `LISTING_SOURCE_TELEGRAM_API_ID` / `_API_HASH` / `_PHONE` | MTProto source login |
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | trade-enabled key (spot trade; **no withdraw**) |
| `BYBIT_SPOT_BUY_ENABLED` | master arm switch for live buying |
| `BYBIT_SPOT_BUY_USDT_AMOUNT` | quote amount per buy (USDT) |
| `BYBIT_SPOT_BUY_MAX_USDT_AMOUNT` | hard money-safety ceiling |
| `LISTING_TELEGRAM_BOT_TOKEN` / `_CHAT_ID` | alert delivery (optional) |

See `.env.example` for the full set (transports, native engine, readiness gates, etc.).

## Usage

```bash
# Detection only — no orders (safe; validate the pipeline live)
.venv/bin/python main.py --realtime --no-trade

# Standard live (race backend, Python order path)
.venv/bin/python main.py --realtime

# Fastest mode (TDLib C++ relay places orders directly; runs readiness gates first)
bash bin/run_race_native_buy_realtime.sh

# One-off single-exchange poll / test alert
.venv/bin/python main.py --exchange bithumb --no-trade
.venv/bin/python main.py --test-telegram
```

Key flags: `--realtime`, `--realtime-backend race|tdlib|telethon|pyrogram`,
`--no-trade`, `--no-telegram`, `--ultra-buy`, `--source-only`, `--strict-realtime`,
`--reset`, `--verbose`. Stop with `Ctrl+C` (graceful flush).

## Testing

```bash
.venv/bin/python -m pytest -q                     # full suite (offline, deterministic)
.venv/bin/python bin/verify_listing_classifiers.py # Python = C++ = Rust = relay parity
```

All external I/O (Telegram / Bybit / clock / filesystem) is mocked, so the suite runs
fully offline and deterministically.

## Running 24/7

Run on an always-on host (a VPS in the same cloud region as Bybit's matching engine
minimizes order latency) under the provided `systemd` unit (`deploy/linux/`), which uses
`Restart=always` so the process auto-recovers. The realtime backends additionally
self-heal via per-backend auto-reconnect, so transient network drops never stop monitoring.

## License

No license is granted. Provided as-is, for personal/educational reference only.
