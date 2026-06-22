from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from queue import Empty
from types import SimpleNamespace

import pytest

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
existing_src = sys.modules.get("src")
if existing_src is not None and str(MODULE_DIR) not in list(getattr(existing_src, "__path__", [])):
    sys.modules.pop("src", None)

from src import tdlib_realtime_client as tdlib_module  # noqa: E402
from src.tdlib_realtime_client import _TdlibRelay, _build_listing_matched_post  # noqa: E402


class _NoClockQueue:
    def get(self, timeout: float):
        raise Empty


class _OneClockQueue:
    def __init__(self, value: int):
        self.value = value

    def get(self, timeout: float):
        return self.value


def _relay_with_stdin() -> _TdlibRelay:
    relay = _TdlibRelay(MODULE_DIR / "bin" / "tdlib_json_relay")
    relay.proc = SimpleNamespace(stdin=io.StringIO())
    return relay


def test_measure_clock_offset_raises_timeout_when_relay_does_not_answer():
    relay = _relay_with_stdin()
    relay.clock_queue = _NoClockQueue()

    with pytest.raises(TimeoutError, match="clock calibration timed out"):
        relay.measure_clock_offset_ns(attempts=1)


def test_measure_clock_offset_returns_sample_when_relay_answers():
    relay = _relay_with_stdin()
    relay.clock_queue = _OneClockQueue(1_000_000)

    assert isinstance(relay.measure_clock_offset_ns(attempts=1), int)


def test_async_wait_for_fails_fast_when_relay_process_exits():
    relay = _TdlibRelay(MODULE_DIR / "bin" / "tdlib_json_relay")
    relay.proc = SimpleNamespace(poll=lambda: 1)

    async def scenario():
        relay._async_queue = asyncio.Queue()
        # Simulate the reader thread observing stdout close (relay died).
        relay._async_queue.put_nowait(tdlib_module._RELAY_EXITED)
        with pytest.raises(RuntimeError, match="relay process exited"):
            await relay.async_wait_for(lambda _event: True, timeout=5)

    asyncio.run(scenario())


def test_tdlib_relay_env_merges_bybit_settings_from_env_loader(monkeypatch):
    monkeypatch.setenv("EXISTING_ENV", "kept")
    monkeypatch.setattr(
        tdlib_module,
        "load_env_settings",
        lambda keys: {
            "BYBIT_API_KEY": "file-key",
            "BYBIT_API_SECRET": "file-secret",
            "BYBIT_SPOT_BUY_ENABLED": "1",
            "BYBIT_SPOT_BUY_USDT_AMOUNT": "5",
            "LISTING_BYBIT_ORDER_RESPONSE_TIMEOUT_MS": "250",
            "LISTING_TDLIB_RECEIVE_TIMEOUT_SEC": "0",
            "LISTING_TDLIB_FLUSH_LISTING_EVENTS": "0",
            "LISTING_TDLIB_EMIT_LISTING_EVENTS": "0",
            "LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH": "1",
            "LISTING_TDLIB_NATIVE_WORKER_SPIN_WAIT": "1",
            "LISTING_TDLIB_NATIVE_WORKER_SPIN_COUNT": "2",
            "LISTING_TDLIB_NATIVE_ORDER_START_SPIN_COUNT": "64",
            "LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH": "data/cache.txt",
            "LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MIN_COUNT": "100",
        },
    )

    env = tdlib_module._tdlib_relay_env()

    assert env["EXISTING_ENV"] == "kept"
    assert env["BYBIT_API_KEY"] == "file-key"
    assert env["BYBIT_API_SECRET"] == "file-secret"
    assert env["BYBIT_SPOT_BUY_ENABLED"] == "1"
    assert env["BYBIT_SPOT_BUY_USDT_AMOUNT"] == "5"
    assert env["LISTING_BYBIT_ORDER_RESPONSE_TIMEOUT_MS"] == "250"
    assert env["LISTING_TDLIB_RECEIVE_TIMEOUT_SEC"] == "0"
    assert env["LISTING_TDLIB_FLUSH_LISTING_EVENTS"] == "0"
    assert env["LISTING_TDLIB_EMIT_LISTING_EVENTS"] == "0"
    assert env["LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH"] == "1"
    assert env["LISTING_TDLIB_NATIVE_WORKER_SPIN_WAIT"] == "1"
    assert env["LISTING_TDLIB_NATIVE_WORKER_SPIN_COUNT"] == "2"
    assert env["LISTING_TDLIB_NATIVE_ORDER_START_SPIN_COUNT"] == "64"
    assert env["LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH"] == "data/cache.txt"
    assert env["LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MIN_COUNT"] == "100"


def test_tdlib_relay_start_passes_merged_env_to_child(monkeypatch):
    captured = {}

    class _FakeProc:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("__relay_ready__\n")

    class _FakeThread:
        def __init__(self, *args, **kwargs):
            captured["thread_args"] = args
            captured["thread_kwargs"] = kwargs

        def start(self):
            captured["thread_started"] = True

    monkeypatch.setattr(tdlib_module, "_tdlib_relay_env", lambda: {"BYBIT_API_KEY": "file-key"})
    monkeypatch.setattr(tdlib_module.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(tdlib_module.threading, "Thread", _FakeThread)

    relay = _TdlibRelay(MODULE_DIR / "bin" / "tdlib_json_relay")
    relay.start()

    assert captured["kwargs"]["env"] == {"BYBIT_API_KEY": "file-key"}
    assert captured["thread_started"] is True


def test_listing_matched_post_preserves_multi_ticker_payload():
    post = _build_listing_matched_post(
        payload={
            "@type": "listingMatched",
            "relay_received_monotonic_ns": 22_000,
            "channel_handle": "BithumbExchange",
            "message_id": 321987,
            "published_at_unix": 1778680000,
            "title": "[마켓 추가] 월드 리버티 파이낸셜(WLFI), 밈코어(M) 원화 마켓 추가",
            "ticker": "WLFI",
            "tickers": ["WLFI", "M"],
            "native_trades": [
                {"ticker": "WLFI", "reason": "tdlib_native_rest_preflight"},
                {"ticker": "M", "reason": "tdlib_native_rest_preflight"},
            ],
        },
        event_received_monotonic_ns=20_000,
        clock_offset_ns=500,
    )

    assert post["received_monotonic_ns"] == 22_000
    assert post["received_python_monotonic_ns"] == 21_500
    assert post["relay_received_monotonic_ns"] == 22_000
    assert post["native_listing"] == {
        "exchange": "bithumb",
        "signal_type": "market_add",
        "ticker": "WLFI",
        "tickers": ["WLFI", "M"],
        "markets": ["KRW"],
    }
    assert [trade["ticker"] for trade in post["native_trades"]] == ["WLFI", "M"]


def test_listing_matched_post_falls_back_to_single_ticker_when_tickers_omitted():
    post = _build_listing_matched_post(
        payload={
            "@type": "listingMatched",
            "channel_handle": "upbit_news",
            "message_id": 321988,
            "published_at_unix": 1778680001,
            "title": "[거래] 바빌론(BABY) KRW 마켓 디지털 자산 추가",
            "ticker": "BABY",
            "native_trade": {"ticker": "BABY", "reason": "tdlib_native_rest_preflight"},
        },
        event_received_monotonic_ns=23_000,
        clock_offset_ns=0,
    )

    assert post["native_listing"] == {
        "exchange": "upbit",
        "signal_type": "new_listing",
        "ticker": "BABY",
        "tickers": ["BABY"],
        "markets": ["KRW"],
    }
    assert post["native_trade"]["ticker"] == "BABY"


def test_listing_matched_post_keeps_string_lists_as_single_values():
    post = _build_listing_matched_post(
        payload={
            "@type": "listingMatched",
            "relay_received_monotonic_ns": 24_000,
            "channel_handle": "BithumbExchange",
            "message_id": 321990,
            "published_at_unix": 1778680003,
            "title": "[마켓 추가] 밈코어(M) 원화 마켓 추가",
            "ticker": "M",
            "tickers": "M",
            "markets": "KRW",
        },
        event_received_monotonic_ns=24_000,
        clock_offset_ns=0,
    )

    assert post["native_listing"]["tickers"] == ["M"]
    assert post["native_listing"]["markets"] == ["KRW"]
