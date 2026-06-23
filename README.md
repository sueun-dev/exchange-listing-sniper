# Exchange Listing Sniper

**English** · [한국어](README.ko.md)

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

## Why this exists

New-coin listings on Korea's dominant exchanges (Upbit, Bithumb) are among the most
reliable short-term catalysts in crypto. Korean spot markets trade at a structural
premium and are driven by concentrated retail flow, so when a Korean exchange
announces a **new KRW market** for a coin that *already* trades on a global venue like
Bybit, that announcement routinely triggers an immediate buy stampede — and the price
on Bybit jumps within the same minute (quantified in the backtest below).

The catalyst is public and the move is fast, so the only edge is **being early**:
parsing the announcement and submitting the order before the crowd. That is an
*engineering* problem, not a discretionary one — it reduces to (1) receiving the
Telegram message with minimal latency, (2) deciding "is this an actionable KRW listing?"
deterministically in microseconds, and (3) firing a correct, idempotent Bybit order,
all before the candle moves. This repository is that machine: a microsecond-class
detect-and-buy pipeline built to capture the few seconds of edge around each
announcement, and validated end-to-end on real historical data.

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

## Strategy validation — backtest on real data

> **100% real data, zero estimation.** Announcement times are the *actual* message
> timestamps from a live Telegram (MTProto) session; prices are Bybit's public V5
> 1-minute kline (OHLC). No price or time is inferred or model-generated.

**Question.** When Bithumb announces a new KRW listing for a coin already on Bybit,
does Bybit actually pump — and by how much?

**Method.** Over a 90-day window, every Bithumb `[마켓 추가] …원화 마켓 추가`
announcement is taken at its exact Telegram timestamp. If `{TICKER}USDT` trades on
Bybit spot, 1-minute OHLC bars are pulled from −5 to +10 minutes around it. Entry is
the **open of the announcement minute** (a proxy for "buy at the instant of the
announcement"); we measure the intra-window high (peak), low (worst dip), and the
+10-minute close.

**Sample.** 21 Bithumb KRW listings in the window. 13 were *not* listed on Bybit and
are excluded — the bot skips these (no order, no loss). The remaining **8 are the
testable set.**

| Ticker | Announce (UTC) | Entry | +0-bar close | Peak (≤+10m) | +10m close | Worst dip |
| --- | --- | --- | --- | --- | --- | --- |
| VVV | 2026-04-01 03:21 | 6.463 | +4.89% | **+7.18%** | +5.85% | −0.28% |
| ZAMA | 2026-04-14 05:39 | 0.02752 | +8.79% | **+19.91%** | +12.17% | −0.04% |
| BASED | 2026-04-21 05:23 | 0.13111 | +4.28% | **+25.41%** | +15.54% | −0.72% |
| BLEND | 2026-04-29 03:24 | 0.22656 | +5.48% | **+9.85%** | +3.75% | −1.79% |
| OPG | 2026-05-22 01:57 | 0.2491 | +18.75% | **+22.84%** | +15.94% | 0.00% |
| BILL | 2026-05-28 05:16 | 0.08565 | +4.47% | **+7.20%** | +4.73% | −1.05% |
| HNT | 2026-06-01 01:45 | 0.7207 | +12.06% | **+13.21%** | +8.48% | 0.00% |
| SPX | 2026-06-16 02:22 | 0.3628 | +3.94% | **+4.85%** | +2.40% | 0.00% |

**Aggregate (n = 8).**

- **Win rate: 8/8 (100%)** positive on both the peak and the +10-minute close.
- **Peak (≤+10m): mean +13.81%, median +11.53%, max +25.41%, min +4.85%.**
- **+10-minute hold: mean +8.61%, median +7.16%.**
- The pump lands *inside the announcement minute itself* — the +0-bar close is already
  +4–19% — direct evidence that Korean flow lifts Bybit immediately, and **why latency
  is the edge**: the faster you fill, the closer to the pre-pump entry.

**Honest limitations — do not over-read this.**

- **Small sample.** n = 8. 8/8 is a strong signal, not a guarantee; "sell-the-news",
  non-pumping coins, and fakeouts *will* occur eventually.
- **Entry is the pre-pump price.** The percentages are an *upper bound* relative to the
  moment of announcement. Your real fill is somewhere inside the pump
  (latency-dependent), so realized return < the table — which is exactly why a
  co-located low-latency host matters.
- **It is not monotonic.** 5 of 8 dipped intra-bar below entry at some point (worst
  −1.79%). The edge is *asymmetric* — small downside, large upside — not "up only".
- **No fees or slippage modeled.** A market order into a thin new-listing book pays real
  slippage plus ~round-trip taker fees.
- **No auto-sell.** The +8–14% is only realized if you exit near the peak; the bot buys
  but does not sell, and gains partially give back by +10 minutes. Exit is on you.
- **Past ≠ future.**

The dataset (`data/bithumb_listing_backtest_90d.json`, local/gitignored) and the method
above are reproducible from public Bybit kline plus the bot's own Telegram session — no
proprietary feed required.

### Cross-check on Upbit — the edge is *conditional*

The identical method run on **Upbit** (`@upbit_news`) over **180 days** tells a very
different story — and clarifies what the edge actually is.

| Market | Sample (tradable on Bybit) | Peak (≤+10m), mean | +10m hold, mean | +10m win rate |
| --- | --- | --- | --- | --- |
| **Bithumb** (90d) | 8 | **+13.81%** | **+8.61%** | 8/8 |
| **Upbit** (180d) | 28 | +1.19% | +0.13% | 13/28 |

Upbit KRW listings barely move Bybit: mean peak +1.19%, a statistically flat
+10-minute hold (+0.13%, 13/28 positive), no in-minute spike, and 27 of 28 dipped below
entry intra-bar — i.e. **after fees and slippage, buying Upbit listings was a net loser
in this sample.**

Why the divergence? Four coins were listed on *both* exchanges, and the data is
unambiguous:

| Coin | Bithumb (first) | Bithumb peak | Upbit (later) | Upbit peak |
| --- | --- | --- | --- | --- |
| ZAMA | 04-14 05:39 | **+19.91%** | 04-14 16:48 | +0.75% |
| BLEND | 04-29 03:24 | **+9.85%** | 04-29 11:23 | +2.09% |
| VVV | 04-01 03:21 | **+7.18%** | 05-12 14:00 | +0.61% |
| SPX | 06-16 02:22 | **+4.85%** | 06-16 11:12 | +0.16% |

In every case Bithumb listed **first** and captured the pump; the later Upbit listing of
the same coin was already priced in and stayed flat. The remaining 24 Upbit-only listings
were dominated by already-established large caps (ICP, WIF, ETHFI, IO, USDE, TAO, …) that
a Korean listing no longer moves.

**Refined thesis.** The catalyst is not "a Korean listing" — it is **the _first_ Korean
listing of a coin not yet globally priced in** (typically a smaller/newer coin, and in
this window usually on Bithumb). A second Korean listing, or a listing of an already-liquid
large cap, carries little to no edge. A bot that buys *every* Upbit and Bithumb listing
therefore dilutes the real edge with break-even-to-negative trades; the natural
refinements are to skip coins already listed on the other Korean exchange and to skip
established large caps. **The honest takeaway: the strategy is real but selective —
"snipe everything" is not the edge; "snipe the right listing, fast, and exit" is.**

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
