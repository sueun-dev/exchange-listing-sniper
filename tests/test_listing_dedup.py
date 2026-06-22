from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path


os.environ["LISTING_CLASSIFIER_BACKEND"] = "python"

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
for module_name in list(sys.modules):
    if module_name == "src" or module_name.startswith("src."):
        sys.modules.pop(module_name, None)

from src.poller import ExchangeListingPoller  # noqa: E402
from src.state_store import StateStore  # noqa: E402


class FakeBybitClient:
    def lookup_ticker(self, ticker: str) -> dict:
        return {"ticker": ticker, "spot": True, "perp": True}


class FakeSpotBuyer:
    def __init__(self):
        self.calls: list[dict] = []

    def buy_market(self, *, ticker: str, order_link_id: str) -> dict:
        self.calls.append({"ticker": ticker, "order_link_id": order_link_id})
        return {
            "enabled": True,
            "attempted": True,
            "executed": False,
            "reason": "test_buyer",
            "order_link_id": order_link_id,
        }


class FakeSignalEmitter:
    def __init__(self):
        self.persisted: list[dict] = []
        self.trade_proofs: list[dict] = []

    def build(self, *, post: dict, listing: dict, bybit: dict, trade: dict, latency=None) -> dict:
        return {
            "message_id": post["message_id"],
            "ticker": listing["ticker"],
            "asset_name": listing["asset_name"],
            "markets": listing["markets"],
            "trade": trade,
        }

    def persist(self, signal: dict):
        self.persisted.append(signal)

    def persist_trade_proof(self, *, post: dict, listing: dict, trade: dict):
        self.trade_proofs.append(
            {
                "message_id": post["message_id"],
                "ticker": listing["ticker"],
                "trade": trade,
            }
        )


class DisabledUltraEngine:
    def is_enabled(self) -> bool:
        return False


class _FakeRawResult:
    def __init__(self, *, matched=True, duplicate=False, reason=b""):
        self.matched = matched
        self.duplicate = duplicate
        self.reason = reason


class FakeCppUltraEngine:
    """In-process ultra engine stub that classifies AND 'buys' like the real one."""

    def __init__(self, *, ticker="XYZ", payload_ticker=None, signal_type="market_add"):
        self.enabled = True
        self.raw_calls: list[int] = []
        self._ticker = ticker
        self._payload_ticker = payload_ticker or ticker
        self._signal_type = signal_type

    def is_enabled(self) -> bool:
        return self.enabled

    def warmup(self):
        return {"ok": True}

    def handle_post_raw(self, *, exchange, message_id, title):
        self.raw_calls.append(message_id)
        return _FakeRawResult(matched=True, duplicate=False)

    def payload_from_raw(self, raw, *, exchange=None, message_id=None):
        if raw is None or raw.duplicate or not raw.matched:
            return None
        trade = {
            "ticker": self._payload_ticker,
            "attempted": True,
            "executed": True,
            "order_link_id": f"ls-{exchange}-{message_id}-{self._payload_ticker}",
            "reason": "native",
        }
        return {
            "duplicate": False,
            "matched": True,
            "signal_type": self._signal_type,
            "ticker": self._payload_ticker,
            "tickers": [self._payload_ticker],
            "asset_name": "자산",
            "markets": ["KRW"],
            "trade": trade,
            "trades": [trade],
        }


def make_fire_fast_poller(tmp_path, engine, emitter):
    poller = ExchangeListingPoller(
        state_store=StateStore(tmp_path / "state.json"),
        bybit_client=FakeBybitClient(),
        spot_buyer=FakeSpotBuyer(),
        signal_emitter=emitter,
        cpp_ultra_engine=engine,
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=True,
        defer_post_trade_work=True,
        emit_ultra_ack=False,
    )
    # Run deferred finalize inline so the assertions are deterministic.
    if poller._bg_executor is not None:
        poller._bg_executor.shutdown(wait=True)
    poller._bg_executor = None
    return poller


def _bithumb_post(message_id: int, title: str) -> dict:
    return {
        "channel_handle": "BithumbExchange",
        "message_id": message_id,
        "published_at": "2026-06-11T13:00:00+00:00",
        "title": title,
        "text": title,
        "post_url": f"https://t.me/BithumbExchange/{message_id}",
    }


def test_cpp_ultra_fire_fast_skips_engine_when_ticker_already_bought(tmp_path):
    engine = FakeCppUltraEngine(ticker="XYZ")
    emitter = FakeSignalEmitter()
    poller = make_fire_fast_poller(tmp_path, engine, emitter)
    assert poller._cpp_ultra_hot_path_enabled
    poller.state_store.mark_listing_seen("bithumb", "XYZ", 5000)

    result = poller.process_post(
        "bithumb",
        _bithumb_post(5002, "[마켓 추가] 엑스코인(XYZ) 원화 마켓 추가 (거래 오픈 5시)"),
    )

    assert result is None
    assert engine.raw_calls == []  # engine never fired -> no second buy
    assert emitter.persisted == []


def test_cpp_ultra_fire_fast_buys_repost_ticker_only_once(tmp_path):
    engine = FakeCppUltraEngine(ticker="XYZ")
    emitter = FakeSignalEmitter()
    poller = make_fire_fast_poller(tmp_path, engine, emitter)

    poller.process_post(
        "bithumb",
        _bithumb_post(5001, "[마켓 추가] 엑스코인(XYZ) 원화 마켓 추가"),
    )
    # Title-augmented re-post with a NEW message_id, same ticker.
    poller.process_post(
        "bithumb",
        _bithumb_post(5002, "[마켓 추가] 엑스코인(XYZ) 원화 마켓 추가 (거래 오픈 5시)"),
    )

    assert engine.raw_calls == [5001]  # second re-post never fires the engine
    assert [item["ticker"] for item in emitter.persisted] == ["XYZ"]


def test_cpp_ultra_fire_fast_finalize_gate_blocks_duplicate_ticker(tmp_path):
    # Pre-check sees AAA (not bought), but the engine authoritatively matches
    # XYZ which was already bought -> finalize must skip (no duplicate proof).
    engine = FakeCppUltraEngine(ticker="AAA", payload_ticker="XYZ")
    emitter = FakeSignalEmitter()
    poller = make_fire_fast_poller(tmp_path, engine, emitter)
    poller.state_store.mark_listing_seen("bithumb", "XYZ", 5000)

    result = poller.process_post(
        "bithumb",
        _bithumb_post(5003, "[마켓 추가] 에이코인(AAA) 원화 마켓 추가"),
    )

    assert result is None
    assert engine.raw_calls == [5003]
    assert emitter.persisted == []
    assert emitter.trade_proofs == []


class _LegacyStateStoreWithoutCanMarkSeen:
    """Duck-typed store that exposes the seen-id snapshot but not can_mark_seen."""

    def __init__(self):
        self._seen = {"bithumb": [200]}

    def snapshot_last_seen(self):
        return {"bithumb": 200}

    def snapshot_seen_message_ids(self):
        return {channel: list(ids) for channel, ids in self._seen.items()}

    def mark_seen(self, channel_id, message_id, persist=True):
        self._seen.setdefault(channel_id, []).append(int(message_id))
        return True


def test_keep_warm_clock_skew_logs_warning(tmp_path, caplog):
    class SkewedClient(FakeBybitClient):
        def server_time_ms(self):
            return time.time() * 1000.0 + 10_000.0  # local clock 10s ahead

    poller = make_poller(tmp_path, FakeSpotBuyer(), FakeSignalEmitter())
    poller.bybit_client = SkewedClient()

    with caplog.at_level(logging.WARNING, logger="src.poller"):
        poller._check_clock_skew()

    assert any("시계" in record.message for record in caplog.records)


def test_keep_warm_clock_skew_silent_when_in_sync(tmp_path, caplog):
    class InSyncClient(FakeBybitClient):
        def server_time_ms(self):
            return time.time() * 1000.0  # aligned

    poller = make_poller(tmp_path, FakeSpotBuyer(), FakeSignalEmitter())
    poller.bybit_client = InSyncClient()

    with caplog.at_level(logging.WARNING, logger="src.poller"):
        poller._check_clock_skew()

    assert not any("시계" in record.message for record in caplog.records)


def test_would_mark_seen_fallback_allows_out_of_order_low_id(tmp_path):
    poller = ExchangeListingPoller(
        state_store=_LegacyStateStoreWithoutCanMarkSeen(),
        bybit_client=FakeBybitClient(),
        spot_buyer=FakeSpotBuyer(),
        signal_emitter=FakeSignalEmitter(),
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    # 199 < last_seen 200 but is NOT in the seen-id set -> must still be allowed.
    assert poller._would_mark_seen("bithumb", 199) is True
    # 200 was already processed -> rejected.
    assert poller._would_mark_seen("bithumb", 200) is False


def make_poller(tmp_path, buyer: FakeSpotBuyer, emitter: FakeSignalEmitter):
    return ExchangeListingPoller(
        state_store=StateStore(tmp_path / "state.json"),
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=emitter,
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )


def test_repeated_bithumb_listing_ticker_does_not_buy_twice(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    poller = make_poller(tmp_path, buyer, emitter)

    first = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 11403,
            "published_at": "2025-10-01T06:28:00+00:00",
            "title": "[마켓 추가/수수료 이벤트] 솜니아(SOMI) 원화 마켓 추가 (거래 수수료 무료)",
            "text": "[마켓 추가/수수료 이벤트] 솜니아(SOMI) 원화 마켓 추가 (거래 수수료 무료)",
            "post_url": "https://t.me/BithumbExchange/11403",
        },
    )
    second = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 11404,
            "published_at": "2025-10-01T06:31:00+00:00",
            "title": "[마켓 추가/수수료 이벤트] 솜니아(SOMI) 원화 마켓 추가 (거래 수수료 무료) (거래 오픈 3시 30분 )",
            "text": "[마켓 추가/수수료 이벤트] 솜니아(SOMI) 원화 마켓 추가 (거래 수수료 무료) (거래 오픈 3시 30분 )",
            "post_url": "https://t.me/BithumbExchange/11404",
        },
    )

    assert first is not None
    assert second is None
    assert [call["ticker"] for call in buyer.calls] == ["SOMI"]
    assert [signal["ticker"] for signal in emitter.persisted] == ["SOMI"]


def test_out_of_order_lower_listing_after_non_listing_is_not_dropped(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    poller = make_poller(tmp_path, buyer, emitter)

    non_listing = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 200,
            "published_at": "2026-06-11T13:00:00+00:00",
            "title": "[빗썸 시세알림] *전일 23:59 기준 대비",
            "text": "[빗썸 시세알림] *전일 23:59 기준 대비",
            "post_url": "https://t.me/BithumbExchange/200",
        },
    )
    listing = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 199,
            "published_at": "2026-06-11T12:59:58+00:00",
            "title": "[마켓 추가] 자마(ZAMA) 원화 마켓 추가(거래 오픈 오후 5시 예정)",
            "text": "[마켓 추가] 자마(ZAMA) 원화 마켓 추가(거래 오픈 오후 5시 예정)",
            "post_url": "https://t.me/BithumbExchange/199",
        },
    )

    assert non_listing is None
    assert listing is not None
    assert listing["ticker"] == "ZAMA"
    assert [call["ticker"] for call in buyer.calls] == ["ZAMA"]


def test_native_trades_align_to_fresh_tickers_after_listing_dedupe(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    state_store = StateStore(tmp_path / "state.json")
    state_store.mark_listing_seen("bithumb", "WLFI", 991019)
    poller = ExchangeListingPoller(
        state_store=state_store,
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=emitter,
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    signal = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 991020,
            "published_at": "2026-06-11T13:00:00+00:00",
            "title": "[마켓 추가] 월드 리버티 파이낸셜(WLFI), 밈코어(M) 원화 마켓 추가",
            "text": "[마켓 추가] 월드 리버티 파이낸셜(WLFI), 밈코어(M) 원화 마켓 추가",
            "post_url": "https://t.me/BithumbExchange/991020",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "WLFI",
                "tickers": ["WLFI", "M"],
                "asset_name": "월드 리버티 파이낸셜",
                "markets": ["KRW"],
                "assets": [
                    {"ticker": "WLFI", "asset_name": "월드 리버티 파이낸셜"},
                    {"ticker": "M", "asset_name": "밈코어"},
                ],
            },
            "native_trades": [
                {
                    "ticker": "WLFI",
                    "symbol": "WLFIUSDT",
                    "order_link_id": "native-WLFI",
                    "reason": "native_preflight",
                },
                {
                    "ticker": "M",
                    "symbol": "MUSDT",
                    "order_link_id": "native-M",
                    "reason": "native_preflight",
                },
            ],
        },
    )

    assert signal is not None
    assert signal["ticker"] == "M"
    assert signal["trade"]["ticker"] == "M"
    assert signal["trade"]["order_link_id"] == "native-M"
    assert [item["ticker"] for item in emitter.persisted] == ["M"]
    assert [item["trade"]["ticker"] for item in emitter.persisted] == ["M"]
    assert buyer.calls == []


def test_missing_native_trade_for_fresh_ticker_uses_python_buy_fallback(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    state_store = StateStore(tmp_path / "state.json")
    state_store.mark_listing_seen("bithumb", "WLFI", 991021)
    poller = ExchangeListingPoller(
        state_store=state_store,
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=emitter,
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    signal = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 991022,
            "published_at": "2026-06-11T13:00:00+00:00",
            "title": "[마켓 추가] 월드 리버티 파이낸셜(WLFI), 밈코어(M) 원화 마켓 추가",
            "text": "[마켓 추가] 월드 리버티 파이낸셜(WLFI), 밈코어(M) 원화 마켓 추가",
            "post_url": "https://t.me/BithumbExchange/991022",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "WLFI",
                "tickers": ["WLFI", "M"],
                "asset_name": "월드 리버티 파이낸셜",
                "markets": ["KRW"],
                "assets": [
                    {"ticker": "WLFI", "asset_name": "월드 리버티 파이낸셜"},
                    {"ticker": "M", "asset_name": "밈코어"},
                ],
            },
            "native_trades": [
                {
                    "ticker": "WLFI",
                    "symbol": "WLFIUSDT",
                    "order_link_id": "native-WLFI",
                    "reason": "native_preflight",
                },
            ],
        },
    )

    assert signal is not None
    assert signal["ticker"] == "M"
    assert signal["trade"]["reason"] == "test_buyer"
    assert signal["trade"]["ticker"] == "M"
    assert signal["trade"]["order_link_id"] == "ls-bithumb-991022-M"
    assert [call["ticker"] for call in buyer.calls] == ["M"]
    assert [item["ticker"] for item in emitter.persisted] == ["M"]


def test_short_bulk_buy_result_keeps_listing_aligned_disabled_trade(tmp_path):
    class ShortBulkBuyer(FakeSpotBuyer):
        def buy_markets(self, orders: list[dict]) -> list[dict]:
            self.calls.extend(orders)
            return [{"reason": "first_only"}]

    buyer = ShortBulkBuyer()
    poller = ExchangeListingPoller(
        state_store=StateStore(tmp_path / "state.json"),
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=FakeSignalEmitter(),
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    trades = poller._maybe_buy_spots(
        channel=poller._channel_runtime_by_id["bithumb"],
        post={"message_id": 991025},
        listings=[{"ticker": "AAA"}, {"ticker": "BBB"}],
    )

    assert trades == [
        {
            "reason": "first_only",
            "ticker": "AAA",
            "order_link_id": "ls-bithumb-991025-AAA",
        },
        {
            "enabled": False,
            "attempted": False,
            "executed": False,
            "ticker": "BBB",
            "order_link_id": "ls-bithumb-991025-BBB",
            "reason": "python_spot_buyer_missing_result",
        },
    ]


def test_out_of_order_bulk_buy_result_aligns_by_ticker(tmp_path):
    class OutOfOrderBulkBuyer(FakeSpotBuyer):
        def buy_markets(self, orders: list[dict]) -> list[dict]:
            self.calls.extend(orders)
            return [
                {"ticker": "BBB", "reason": "bbb_result"},
                {"ticker": "AAA", "reason": "aaa_result"},
            ]

    buyer = OutOfOrderBulkBuyer()
    poller = ExchangeListingPoller(
        state_store=StateStore(tmp_path / "state.json"),
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=FakeSignalEmitter(),
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    trades = poller._maybe_buy_spots(
        channel=poller._channel_runtime_by_id["bithumb"],
        post={"message_id": 991027},
        listings=[{"ticker": "AAA"}, {"ticker": "BBB"}],
    )

    assert [call["ticker"] for call in buyer.calls] == ["AAA", "BBB"]
    assert trades == [
        {
            "ticker": "AAA",
            "reason": "aaa_result",
            "order_link_id": "ls-bithumb-991027-AAA",
        },
        {
            "ticker": "BBB",
            "reason": "bbb_result",
            "order_link_id": "ls-bithumb-991027-BBB",
        },
    ]


def test_empty_bulk_buy_result_keeps_disabled_trade_for_every_listing(tmp_path):
    class EmptyBulkBuyer(FakeSpotBuyer):
        def buy_markets(self, orders: list[dict]):
            self.calls.extend(orders)
            return None

    buyer = EmptyBulkBuyer()
    poller = ExchangeListingPoller(
        state_store=StateStore(tmp_path / "state.json"),
        bybit_client=FakeBybitClient(),
        spot_buyer=buyer,
        signal_emitter=FakeSignalEmitter(),
        cpp_ultra_engine=DisabledUltraEngine(),
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
    )

    trades = poller._maybe_buy_spots(
        channel=poller._channel_runtime_by_id["bithumb"],
        post={"message_id": 991026},
        listings=[{"ticker": "AAA"}, {"ticker": "BBB"}],
    )

    assert [trade["ticker"] for trade in trades] == ["AAA", "BBB"]
    assert [trade["reason"] for trade in trades] == [
        "python_spot_buyer_missing_result",
        "python_spot_buyer_missing_result",
    ]
    assert [trade["order_link_id"] for trade in trades] == [
        "ls-bithumb-991026-AAA",
        "ls-bithumb-991026-BBB",
    ]


def test_native_trade_without_ticker_keeps_positional_fallback():
    listings = [{"ticker": "WLFI"}, {"ticker": "M"}]
    native_trades = [
        {"order_link_id": "first-positional"},
        {"order_link_id": "second-positional"},
    ]

    aligned = ExchangeListingPoller._align_native_trades_to_listings(
        listings=listings,
        native_trades=native_trades,
    )

    assert [trade["order_link_id"] for trade in aligned] == [
        "first-positional",
        "second-positional",
    ]


def test_native_listing_string_tickers_are_not_split_into_characters(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    poller = make_poller(tmp_path, buyer, emitter)

    signal = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 991011,
            "published_at": "2026-06-11T13:00:00+00:00",
            "title": "native matched payload",
            "text": "native matched payload",
            "post_url": "https://t.me/BithumbExchange/991011",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "WLFI",
                "tickers": "WLFI",
                "asset_name": "월드 리버티 파이낸셜",
                "markets": "KRW",
            },
        },
    )

    assert signal is not None
    assert signal["ticker"] == "WLFI"
    assert signal["markets"] == ["KRW"]
    assert [call["ticker"] for call in buyer.calls] == ["WLFI"]
    assert [item["ticker"] for item in emitter.persisted] == ["WLFI"]


def test_duplicate_native_trade_payload_is_persisted_without_rebuy(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    poller = make_poller(tmp_path, buyer, emitter)
    post = {
        "channel_handle": "BithumbExchange",
        "message_id": 991012,
        "published_at": "2026-06-11T13:00:00+00:00",
        "title": "[마켓 추가] 월드 리버티 파이낸셜(WLFI) 원화 마켓 추가",
        "text": "[마켓 추가] 월드 리버티 파이낸셜(WLFI) 원화 마켓 추가",
        "post_url": "https://t.me/BithumbExchange/991012",
        "native_listing": {
            "signal_type": "market_add",
            "ticker": "WLFI",
            "tickers": ["WLFI"],
            "asset_name": "월드 리버티 파이낸셜",
            "markets": ["KRW"],
        },
    }

    first = poller.process_post("bithumb", dict(post))
    duplicate = poller.process_post(
        "bithumb",
        {
            **post,
            "native_trades": [
                {
                    "ticker": "WLFI",
                    "attempted": True,
                    "executed": True,
                    "reason": "tdlib_native_rest_preflight",
                }
            ],
        },
    )

    assert first is not None
    assert duplicate is None
    assert [call["ticker"] for call in buyer.calls] == ["WLFI"]
    assert [item["ticker"] for item in emitter.persisted] == ["WLFI", "WLFI"]
    assert emitter.persisted[-1]["trade"]["executed"] is True
    assert emitter.persisted[-1]["trade"]["reason"] == "tdlib_native_rest_preflight"
    assert [item["ticker"] for item in emitter.trade_proofs] == ["WLFI"]
    assert emitter.trade_proofs[-1]["trade"]["executed"] is True


def test_invalid_native_listing_falls_back_to_title_classifier(tmp_path):
    buyer = FakeSpotBuyer()
    emitter = FakeSignalEmitter()
    poller = make_poller(tmp_path, buyer, emitter)

    signal = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 991013,
            "published_at": "2026-06-11T13:00:00+00:00",
            "title": "[마켓 추가] 밈코어(M) 원화 마켓 추가",
            "text": "[마켓 추가] 밈코어(M) 원화 마켓 추가",
            "post_url": "https://t.me/BithumbExchange/991013",
            "native_listing": "malformed",
        },
    )

    assert signal is not None
    assert signal["ticker"] == "M"
    assert [call["ticker"] for call in buyer.calls] == ["M"]
