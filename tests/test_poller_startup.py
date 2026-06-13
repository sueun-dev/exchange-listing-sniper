from __future__ import annotations

import copy
import sys
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
for module_name in list(sys.modules):
    if module_name == "src" or module_name.startswith("src."):
        sys.modules.pop(module_name, None)

import src.poller as poller_module  # noqa: E402
from src.poller import ExchangeListingPoller  # noqa: E402


class _MarketClient:
    def __init__(self):
        self.refresh_calls = 0

    def refresh_market_cache(self):
        self.refresh_calls += 1


class _LookupRecorderClient(_MarketClient):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def lookup_ticker(self, ticker):
        self.events.append("lookup")
        return {
            "symbol": f"{ticker}USDT",
            "spot": True,
            "perp": False,
            "any": True,
        }


class _ProofRecorderSignalEmitter:
    def __init__(self, events):
        self.events = events

    def persist_trade_proof(self, *, post, listing, trade):
        self.events.append("proof")
        return None

    def build(self, *, post, listing, bybit, trade, latency):
        self.events.append("build")
        return {
            "exchange": listing["exchange"],
            "ticker": listing["ticker"],
            "message_id": post["message_id"],
            "trade": trade,
        }

    def persist(self, signal):
        self.events.append("persist")
        return None


class _TraceWithPythonClock:
    def as_dict(self):
        return {
            "total_ns": 1000,
            "total_us": 1.0,
            "total_ms": 0.001,
            "stages_ns": {},
            "marks": ["start", "done"],
        }

    def start_ns(self):
        return 2000

    def last_ns(self):
        return 3000


class _SpotBuyer:
    pass


class _BulkSpotBuyer:
    def __init__(self):
        self.orders = []

    def buy_markets(self, orders):
        self.orders = list(orders)
        return [
            {
                "enabled": True,
                "attempted": True,
                "executed": False,
                "reason": "test",
                "symbol": f"{order['ticker']}USDT",
                "order_link_id": order["order_link_id"],
            }
            for order in orders
        ]


class _StateStore:
    def snapshot_last_seen(self):
        return {}

    def mark_seen(self, channel_id: str, message_id: int, persist: bool = True):
        return True


class _HotSnapshotStateStore(_StateStore):
    def __init__(self):
        self.replaced = None

    def snapshot_last_seen(self):
        return {"bithumb": 321989}

    def snapshot_seen_message_ids(self):
        return {"bithumb": [321989]}

    def replace_hot_state_snapshot(self, last_seen, seen_message_ids, persist: bool = True):
        self.replaced = (dict(last_seen), {key: list(value) for key, value in seen_message_ids.items()}, persist)


class _CppUltraEngine:
    def __init__(self):
        self.is_enabled_calls = 0
        self.warmup_calls = 0

    def is_enabled(self):
        self.is_enabled_calls += 1
        return True

    def warmup(self):
        self.warmup_calls += 1
        return {"ok": True}


class _FailingWarmupCppUltraEngine(_CppUltraEngine):
    def warmup(self):
        self.warmup_calls += 1
        return {"ok": False, "reason": "symbol_cache_empty"}


class _RawUltraResult:
    duplicate = False
    matched = True
    reason = b""


class _MultiTickerRawUltraResult:
    duplicate = False
    matched = False
    reason = b"multi_ticker"


class _CallableCppUltraEngine(_CppUltraEngine):
    def __init__(self, raw_result=None):
        super().__init__()
        self.handle_calls = 0
        self.raw_result = raw_result or _RawUltraResult()

    def handle_post_raw(self, *, exchange: str, message_id: int, title: str):
        self.handle_calls += 1
        self.last_call = {
            "exchange": exchange,
            "message_id": message_id,
            "title": title,
        }
        return self.raw_result

    def payload_from_raw(self, raw_result, **_kwargs):
        if not raw_result.matched or raw_result.duplicate:
            return None
        return {
            "duplicate": False,
            "matched": True,
            "signal_type": "market_add",
            "ticker": "STRK",
            "tickers": ["STRK"],
            "asset_name": "스타크넷",
            "markets": ["KRW"],
            "trade": {
                "ticker": "STRK",
                "symbol": "STRKUSDT",
                "order_link_id": "ls-b-321987-STRK",
                "attempted": True,
                "executed": False,
            },
            "trades": [
                {
                    "ticker": "STRK",
                    "symbol": "STRKUSDT",
                    "order_link_id": "ls-b-321987-STRK",
                    "attempted": True,
                    "executed": False,
                }
            ],
        }


class _PayloadCppUltraEngine(_CppUltraEngine):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    def payload_from_raw(self, raw_result, **_kwargs):
        return self.payload


def test_native_tdlib_startup_can_skip_python_bybit_and_ultra_warmups():
    market_client = _MarketClient()
    ultra_engine = _CppUltraEngine()

    poller = ExchangeListingPoller(
        bybit_client=market_client,
        spot_buyer=_SpotBuyer(),
        cpp_ultra_engine=ultra_engine,
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=False,
        defer_post_trade_work=True,
    )

    assert market_client.refresh_calls == 0
    assert ultra_engine.is_enabled_calls == 0
    assert ultra_engine.warmup_calls == 0
    assert poller._cpp_ultra_hot_path_enabled is False


def test_non_native_startup_still_warms_enabled_ultra_engine():
    market_client = _MarketClient()
    ultra_engine = _CppUltraEngine()

    poller = ExchangeListingPoller(
        bybit_client=market_client,
        spot_buyer=_SpotBuyer(),
        cpp_ultra_engine=ultra_engine,
        enable_trading=True,
        enable_bybit_warmup=True,
        enable_cpp_ultra_warmup=True,
        defer_post_trade_work=True,
    )

    assert market_client.refresh_calls == 1
    assert ultra_engine.is_enabled_calls >= 1
    assert ultra_engine.warmup_calls == 1
    assert poller._cpp_ultra_hot_path_enabled is True


def test_required_cpp_ultra_warmup_fails_startup_when_not_ready():
    ultra_engine = _FailingWarmupCppUltraEngine()

    try:
        ExchangeListingPoller(
            bybit_client=_MarketClient(),
            spot_buyer=_SpotBuyer(),
            cpp_ultra_engine=ultra_engine,
            enable_trading=True,
            enable_bybit_warmup=False,
            enable_cpp_ultra_warmup=True,
            require_cpp_ultra_warmup=True,
            defer_post_trade_work=True,
        )
    except RuntimeError as exc:
        assert "C++ ultra engine warmup is required" in str(exc)
    else:
        raise AssertionError("required C++ ultra warmup should fail startup")


def test_cpp_ultra_no_ack_uses_direct_fast_impl():
    poller = ExchangeListingPoller(
        bybit_client=_MarketClient(),
        spot_buyer=_SpotBuyer(),
        cpp_ultra_engine=_CppUltraEngine(),
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=True,
        defer_post_trade_work=True,
        emit_ultra_ack=False,
    )

    assert poller._cpp_ultra_hot_path_enabled is True
    assert poller._process_post_impl.__name__ == "_process_post_cpp_ultra_fire_fast"


def test_cpp_ultra_no_ack_fast_impl_calls_raw_engine_once():
    ultra_engine = _CallableCppUltraEngine()
    poller = ExchangeListingPoller(
        bybit_client=_MarketClient(),
        spot_buyer=_SpotBuyer(),
        state_store=_StateStore(),
        cpp_ultra_engine=ultra_engine,
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=True,
        defer_persistence=True,
        defer_post_trade_work=True,
        hot_state_enabled=True,
        state_flush_interval_sec=0,
        emit_ultra_ack=False,
    )
    poller._submit_background = lambda *_args, **_kwargs: None

    result = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 321987,
            "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
        },
    )

    assert result is None
    assert ultra_engine.handle_calls == 1
    assert ultra_engine.last_call["exchange"] == "bithumb"
    assert ultra_engine.last_call["message_id"] == 321987
    assert poller._hot_last_seen["bithumb"] == 321987


def test_cpp_ultra_no_ack_skips_raw_engine_when_tdlib_native_trade_exists():
    ultra_engine = _CallableCppUltraEngine()
    spot_buyer = _BulkSpotBuyer()
    finalized = []
    poller = ExchangeListingPoller(
        bybit_client=_MarketClient(),
        spot_buyer=spot_buyer,
        state_store=_StateStore(),
        cpp_ultra_engine=ultra_engine,
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=True,
        defer_persistence=True,
        defer_post_trade_work=True,
        hot_state_enabled=True,
        state_flush_interval_sec=0,
        emit_ultra_ack=False,
    )
    poller._submit_background = lambda fn, *args: finalized.append((fn.__name__, args))

    result = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 321987,
            "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "STRK",
                "tickers": ["STRK"],
                "asset_name": "스타크넷",
                "markets": ["KRW"],
            },
            "native_trade": {
                "enabled": True,
                "attempted": True,
                "executed": False,
                "reason": "tdlib_native_rest_preflight",
                "symbol": "STRKUSDT",
                "order_link_id": "ls-b-321987-STRK",
            },
        },
    )

    assert result is None
    assert ultra_engine.handle_calls == 0
    assert spot_buyer.orders == []
    assert poller._hot_last_seen["bithumb"] == 321987
    assert [item[0] for item in finalized] == ["_finalize_native_trades_post_trade_work"]


def test_cpp_ultra_no_ack_duplicate_tdlib_native_trade_still_finalizes_proof():
    ultra_engine = _CallableCppUltraEngine()
    spot_buyer = _BulkSpotBuyer()
    finalized = []
    poller = ExchangeListingPoller(
        bybit_client=_MarketClient(),
        spot_buyer=spot_buyer,
        state_store=_StateStore(),
        cpp_ultra_engine=ultra_engine,
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=True,
        defer_persistence=True,
        defer_post_trade_work=True,
        hot_state_enabled=True,
        state_flush_interval_sec=0,
        emit_ultra_ack=False,
    )
    poller._remember_seen("bithumb", 321987)
    poller._submit_background = lambda fn, *args: finalized.append((fn.__name__, args))

    result = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 321987,
            "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "STRK",
                "tickers": ["STRK"],
                "asset_name": "스타크넷",
                "markets": ["KRW"],
            },
            "native_trade": {
                "enabled": True,
                "attempted": True,
                "executed": False,
                "reason": "tdlib_native_rest_preflight",
                "symbol": "STRKUSDT",
                "order_link_id": "ls-b-321987-STRK",
            },
        },
    )

    assert result is None
    assert ultra_engine.handle_calls == 0
    assert spot_buyer.orders == []
    assert poller._hot_last_seen["bithumb"] == 321987
    assert [item[0] for item in finalized] == ["_finalize_native_trades_post_trade_work"]


def test_cpp_ultra_no_ack_hot_state_allows_lower_unseen_native_trade_after_higher_id():
    ultra_engine = _CallableCppUltraEngine()
    spot_buyer = _BulkSpotBuyer()
    finalized = []
    poller = ExchangeListingPoller(
        bybit_client=_MarketClient(),
        spot_buyer=spot_buyer,
        state_store=_StateStore(),
        cpp_ultra_engine=ultra_engine,
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=True,
        defer_persistence=True,
        defer_post_trade_work=True,
        hot_state_enabled=True,
        state_flush_interval_sec=0,
        emit_ultra_ack=False,
    )
    poller._submit_background = lambda fn, *args: finalized.append((fn.__name__, args))

    poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 321989,
            "title": "[마켓 추가] 센티언트(SENT) 원화 마켓 추가",
        },
    )
    finalized.clear()

    result = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 321988,
            "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "STRK",
                "tickers": ["STRK"],
                "asset_name": "스타크넷",
                "markets": ["KRW"],
            },
            "native_trade": {
                "enabled": True,
                "attempted": True,
                "executed": False,
                "reason": "tdlib_native_rest_preflight",
                "symbol": "STRKUSDT",
                "order_link_id": "ls-b-321988-STRK",
            },
        },
    )

    assert result is None
    assert ultra_engine.handle_calls == 1
    assert spot_buyer.orders == []
    assert poller._hot_last_seen["bithumb"] == 321989
    assert [item[0] for item in finalized] == ["_finalize_native_trades_post_trade_work"]


def test_hot_state_restart_keeps_loaded_last_seen_as_replay_floor():
    ultra_engine = _CallableCppUltraEngine()
    spot_buyer = _BulkSpotBuyer()
    state_store = _HotSnapshotStateStore()
    finalized = []
    poller = ExchangeListingPoller(
        bybit_client=_MarketClient(),
        spot_buyer=spot_buyer,
        state_store=state_store,
        cpp_ultra_engine=ultra_engine,
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=True,
        defer_persistence=True,
        defer_post_trade_work=True,
        hot_state_enabled=True,
        state_flush_interval_sec=0,
        emit_ultra_ack=False,
    )
    poller._submit_background = lambda fn, *args: finalized.append((fn.__name__, args))

    duplicate_result = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 321989,
            "title": "[마켓 추가] 센티언트(SENT) 원화 마켓 추가",
        },
    )
    lower_unseen_result = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 321988,
            "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "STRK",
                "tickers": ["STRK"],
                "asset_name": "스타크넷",
                "markets": ["KRW"],
            },
            "native_trade": {
                "enabled": True,
                "attempted": True,
                "executed": False,
                "reason": "tdlib_native_rest_preflight",
                "symbol": "STRKUSDT",
                "order_link_id": "ls-b-321988-STRK",
            },
        },
    )
    poller._mark_state_dirty()
    poller._flush_state_if_dirty()

    assert duplicate_result is None
    assert lower_unseen_result is None
    assert ultra_engine.handle_calls == 0
    assert spot_buyer.orders == []
    assert [item[0] for item in finalized] == ["_finalize_native_trades_post_trade_work"]
    assert state_store.replaced == (
        {"upbit": 0, "bithumb": 321989},
        {"upbit": [], "bithumb": [321989]},
        True,
    )


def test_ultra_fire_fast_uses_tdlib_native_trade_without_python_rebuy():
    spot_buyer = _BulkSpotBuyer()
    finalized = []
    poller = ExchangeListingPoller(
        bybit_client=_MarketClient(),
        spot_buyer=spot_buyer,
        state_store=_StateStore(),
        cpp_ultra_engine=None,
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=False,
        defer_persistence=True,
        defer_post_trade_work=True,
        hot_state_enabled=True,
        state_flush_interval_sec=0,
        emit_ultra_ack=False,
    )
    poller._submit_background = lambda fn, *args: finalized.append((fn.__name__, args))

    result = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 321987,
            "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
            "native_listing": {
                "signal_type": "market_add",
                "ticker": "STRK",
                "tickers": ["STRK"],
                "asset_name": "스타크넷",
                "markets": ["KRW"],
            },
            "native_trade": {
                "enabled": True,
                "attempted": True,
                "executed": False,
                "reason": "tdlib_native_rest_preflight",
                "symbol": "STRKUSDT",
                "order_link_id": "ls-b-321987-STRK",
            },
        },
    )

    assert result is None
    assert spot_buyer.orders == []
    assert poller._hot_last_seen["bithumb"] == 321987
    assert [item[0] for item in finalized] == ["_finalize_post_trade_work"]


def test_native_listing_processing_does_not_mutate_input_post_payload():
    events = []
    native_listing = {
        "signal_type": "market_add",
        "ticker": "STRK",
        "tickers": ["STRK"],
        "asset_name": "스타크넷",
        "markets": ["KRW"],
        "assets": [{"ticker": "STRK", "asset_name": "스타크넷"}],
    }
    post = {
        "channel_handle": "BithumbExchange",
        "message_id": 321987,
        "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
        "text": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
        "published_at": "2026-06-01T00:00:00+00:00",
        "native_listing": native_listing,
        "native_trade": {
            "enabled": True,
            "attempted": True,
            "executed": False,
            "reason": "tdlib_native_rest_preflight",
            "symbol": "STRKUSDT",
            "order_link_id": "ls-b-321987-STRK",
        },
    }
    original_native_listing = copy.deepcopy(native_listing)
    poller = ExchangeListingPoller(
        bybit_client=_LookupRecorderClient(events),
        spot_buyer=_SpotBuyer(),
        state_store=_StateStore(),
        signal_emitter=_ProofRecorderSignalEmitter(events),
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=False,
    )

    signal = poller.process_post("bithumb", post)

    assert post["native_listing"] == original_native_listing
    assert signal["exchange"] == "bithumb"
    assert signal["ticker"] == "STRK"
    assert signal["message_id"] == 321987
    assert events == ["lookup", "build", "persist"]


def test_cpp_ultra_no_ack_multi_ticker_falls_back_to_bulk_python_path():
    ultra_engine = _CallableCppUltraEngine(_MultiTickerRawUltraResult())
    spot_buyer = _BulkSpotBuyer()
    poller = ExchangeListingPoller(
        bybit_client=_MarketClient(),
        spot_buyer=spot_buyer,
        state_store=_StateStore(),
        cpp_ultra_engine=ultra_engine,
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=True,
        defer_persistence=True,
        defer_post_trade_work=True,
        hot_state_enabled=True,
        state_flush_interval_sec=0,
        emit_ultra_ack=False,
    )
    poller._submit_background = lambda *_args, **_kwargs: None

    result = poller.process_post(
        "bithumb",
        {
            "channel_handle": "BithumbExchange",
            "message_id": 321988,
            "title": "[마켓 추가] 센티언트(SENT), 헤이엘사(ELSA) 원화 마켓 추가",
        },
    )

    assert result is None
    assert ultra_engine.handle_calls == 1
    assert [order["ticker"] for order in spot_buyer.orders] == ["SENT", "ELSA"]
    assert poller._hot_last_seen["bithumb"] == 321988


def test_cpp_ultra_raw_finalize_preserves_all_multi_ticker_trades():
    payload = {
        "duplicate": False,
        "matched": True,
        "signal_type": "market_add",
        "ticker": "SENT",
        "tickers": ["SENT", "ELSA"],
        "asset_name": "센티언트",
        "markets": ["KRW"],
        "trade": {
            "ticker": "SENT",
            "symbol": "SENTUSDT",
            "order_link_id": "ls-b-321988-SENT",
            "attempted": True,
            "executed": False,
            "reason": "test",
        },
        "trades": [
            {
                "ticker": "SENT",
                "symbol": "SENTUSDT",
                "order_link_id": "ls-b-321988-SENT",
                "attempted": True,
                "executed": False,
                "reason": "test",
            },
            {
                "ticker": "ELSA",
                "symbol": "ELSAUSDT",
                "order_link_id": "ls-b-321988-ELSA",
                "attempted": True,
                "executed": False,
                "reason": "test",
            },
        ],
    }
    poller = ExchangeListingPoller(
        bybit_client=_MarketClient(),
        spot_buyer=_SpotBuyer(),
        state_store=_StateStore(),
        cpp_ultra_engine=_PayloadCppUltraEngine(payload),
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=False,
        defer_post_trade_work=True,
    )
    finalized = []
    poller._finalize_post_trade_work = (
        lambda _trace, _post, listing, trade: finalized.append(
            (listing["ticker"], trade["symbol"], trade["order_link_id"])
        )
    )

    poller._finalize_cpp_ultra_raw_post_trade_work(
        poller_module.NOOP_LATENCY_TRACE,
        {
            "message_id": 321988,
            "title": "[마켓 추가] 센티언트(SENT), 헤이엘사(ELSA) 원화 마켓 추가",
        },
        poller._channel_runtime_by_id["bithumb"],
        payload,
        0,
        0,
    )

    assert finalized == [
        ("SENT", "SENTUSDT", "ls-b-321988-SENT"),
        ("ELSA", "ELSAUSDT", "ls-b-321988-ELSA"),
    ]


def test_native_tdlib_startup_can_skip_spot_buyer_and_ultra_engine_construction(monkeypatch):
    def fail_bybit_client(*_args, **_kwargs):
        raise AssertionError("Python Bybit client should not be constructed")

    def fail_spot_buyer(*_args, **_kwargs):
        raise AssertionError("Python spot buyer should not be constructed")

    def fail_channel_client(*_args, **_kwargs):
        raise AssertionError("HTML channel client should not be constructed")

    def fail_ultra_engine(*_args, **_kwargs):
        raise AssertionError("C++ ultra engine should not be constructed")

    def fail_signal_emitter(*_args, **_kwargs):
        raise AssertionError("Signal emitter should not be constructed before post-trade work")

    def fail_source_emitter(*_args, **_kwargs):
        raise AssertionError("Source emitter should not be constructed before source persistence")

    monkeypatch.setattr(poller_module, "BybitClient", fail_bybit_client)
    monkeypatch.setattr(poller_module, "BybitSpotBuyer", fail_spot_buyer)
    monkeypatch.setattr(poller_module, "TelegramChannelClient", fail_channel_client)
    monkeypatch.setattr(poller_module, "CppUltraListingEngineBridge", fail_ultra_engine)
    monkeypatch.setattr(poller_module, "SignalEmitter", fail_signal_emitter)
    monkeypatch.setattr(poller_module, "SourceEventEmitter", fail_source_emitter)

    poller = ExchangeListingPoller(
        enable_trading=True,
        enable_python_spot_buyer=False,
        enable_bybit_warmup=False,
        enable_channel_client=False,
        enable_cpp_ultra_warmup=False,
        defer_post_trade_work=True,
    )

    assert poller.bybit_client is None
    assert poller.channel_client is None
    assert poller.spot_buyer is None
    assert poller.cpp_ultra_engine is None
    assert poller.signal_emitter is None
    assert poller.source_emitter is None
    assert poller._cpp_ultra_hot_path_enabled is False


def test_deferred_finalize_persists_trade_proof_before_slow_signal_work():
    events = []
    poller = ExchangeListingPoller(
        bybit_client=_LookupRecorderClient(events),
        spot_buyer=_SpotBuyer(),
        state_store=_StateStore(),
        signal_emitter=_ProofRecorderSignalEmitter(events),
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=False,
        defer_post_trade_work=True,
    )

    poller._finalize_post_trade_work(
        poller_module.NOOP_LATENCY_TRACE,
        {
            "channel_handle": "BithumbExchange",
            "message_id": 321987,
            "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
            "published_at": "2026-06-01T00:00:00+00:00",
            "received_monotonic_ns": 1000,
        },
        {
            "exchange": "bithumb",
            "display_name": "빗썸",
            "signal_type": "market_add",
            "ticker": "STRK",
            "asset_name": "스타크넷",
            "markets": ["KRW"],
        },
        {
            "enabled": True,
            "attempted": True,
            "executed": False,
            "reason": "tdlib_native_rest_preflight",
            "symbol": "STRKUSDT",
            "order_link_id": "ls-b-321987-STRK",
        },
    )

    assert events == ["proof", "lookup", "build", "persist"]


def test_latency_payload_uses_python_clock_only_for_python_trace_deltas():
    payload = ExchangeListingPoller._build_latency_payload(
        object(),
        _TraceWithPythonClock(),
        {
            "message_id": 321987,
            "received_monotonic_ns": 1000,
            "received_python_monotonic_ns": 1500,
        },
        {"ticker": "STRK"},
        {
            "attempted": True,
            "executed": False,
            "order_send_started_monotonic_ns": 1700,
            "trade_finished_monotonic_ns": 1800,
        },
    )

    assert payload["receive_to_trace_start_ns"] == 500
    assert payload["receive_to_signal_ns"] == 1500
    assert payload["receive_to_order_send_started_ns"] == 700
    assert payload["receive_to_trade_finished_ns"] == 800
