from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
for module_name in list(sys.modules):
    if module_name == "src" or module_name.startswith("src."):
        sys.modules.pop(module_name, None)

import src.tdlib_realtime_client as tdlib_module  # noqa: E402
from src.tdlib_realtime_client import TdlibRealtimeChannelClient  # noqa: E402


class _StopAfterReady(RuntimeError):
    pass


class _FakeRelay:
    instances: list["_FakeRelay"] = []

    def __init__(self, relay_path):
        self.relay_path = relay_path
        self.raw_lines: list[str] = []
        self.attached = False
        self.closed = False
        _FakeRelay.instances.append(self)

    def start(self):
        return None

    def send_raw(self, line: str):
        self.raw_lines.append(line)

    def send_request(self, obj: dict, timeout: float = 10.0):
        self.raw_lines.append(f"REQUEST:{obj.get('@type')}")
        return {"@type": "error", "message": "unexpected request"}

    def wait_for_native_status(self, timeout: float = 15.0):
        self.raw_lines.append("__wait_native_status__")
        return {"ready": True, "reason": "ready"}

    def attach_async_loop(self, _loop):
        self.attached = True
        return None

    async def async_wait_for(self, _predicate, _timeout):
        raise _StopAfterReady()

    def close(self):
        self.closed = True
        return None


def test_tdlib_native_buy_warms_before_watch_registration(monkeypatch, tmp_path):
    _FakeRelay.instances.clear()
    monkeypatch.setattr(tdlib_module, "_TdlibRelay", _FakeRelay)
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ACTIVE", "1")
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ENABLED", "1")
    monkeypatch.setenv(
        "LISTING_TDLIB_WATCH_CHATS",
        "-1002562064658:upbit_news,-1001202540487:BithumbExchange",
    )

    client = TdlibRealtimeChannelClient(
        api_id=1,
        api_hash="hash",
        phone="+10000000000",
        relay_path=tmp_path / "fake-relay",
        database_dir=tmp_path / "tdlib-db",
    )
    client._ensure_ready = lambda _relay, _interactive: None
    ready_calls = []

    async def _run():
        await client.run(
            channel_handles=["upbit_news", "BithumbExchange"],
            on_post=lambda _post: None,
            trade_post=True,
            on_ready=lambda: ready_calls.append("ready"),
        )

    with pytest.raises(_StopAfterReady):
        asyncio.run(_run())

    relay = _FakeRelay.instances[-1]
    start_index = next(
        index
        for index, line in enumerate(relay.raw_lines)
        if line.startswith("__native_start__\t")
    )
    wait_index = relay.raw_lines.index("__wait_native_status__")
    assert start_index < wait_index
    assert ready_calls == ["ready"]
    assert "__native_buy_on__" not in relay.raw_lines
    assert "__native_listing_on__" not in relay.raw_lines
    assert "REQUEST:searchPublicChat" not in relay.raw_lines


def test_tdlib_uses_cached_watch_chats_without_search_public_chat(monkeypatch, tmp_path):
    _FakeRelay.instances.clear()
    monkeypatch.setattr(tdlib_module, "_TdlibRelay", _FakeRelay)
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ACTIVE", "1")
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ENABLED", "1")
    monkeypatch.delenv("LISTING_TDLIB_WATCH_CHATS", raising=False)
    cache_path = tmp_path / "tdlib_watch_chats.json"
    cache_path.write_text(
        '{"bithumbexchange": -1001202540487, "upbit_news": -1002562064658}',
        encoding="utf-8",
    )
    monkeypatch.setattr(tdlib_module, "DEFAULT_WATCH_CHAT_CACHE_PATH", cache_path)

    client = TdlibRealtimeChannelClient(
        api_id=1,
        api_hash="hash",
        phone="+10000000000",
        relay_path=tmp_path / "fake-relay",
        database_dir=tmp_path / "tdlib-db",
    )
    client._ensure_ready = lambda _relay, _interactive: None

    async def _run():
        await client.run(
            channel_handles=["upbit_news", "BithumbExchange"],
            on_post=lambda _post: None,
            trade_post=True,
        )

    with pytest.raises(_StopAfterReady):
        asyncio.run(_run())

    relay = _FakeRelay.instances[-1]
    native_start = next(line for line in relay.raw_lines if line.startswith("__native_start__\t"))
    assert "-1002562064658:upbit_news" in native_start
    assert "-1001202540487:BithumbExchange" in native_start
    assert "REQUEST:searchPublicChat" not in relay.raw_lines


def test_tdlib_native_relay_only_skips_python_event_loop(monkeypatch, tmp_path):
    _FakeRelay.instances.clear()
    monkeypatch.setattr(tdlib_module, "_TdlibRelay", _FakeRelay)
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ACTIVE", "1")
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ENABLED", "1")
    monkeypatch.setenv(
        "LISTING_TDLIB_WATCH_CHATS",
        "-1002562064658:upbit_news,-1001202540487:BithumbExchange",
    )

    client = TdlibRealtimeChannelClient(
        api_id=1,
        api_hash="hash",
        phone="+10000000000",
        relay_path=tmp_path / "fake-relay",
        database_dir=tmp_path / "tdlib-db",
    )
    client._ensure_ready = lambda _relay, _interactive: None
    ready_calls = []

    def _on_ready():
        ready_calls.append("ready")
        raise _StopAfterReady()

    with pytest.raises(_StopAfterReady):
        asyncio.run(
            client.run_native_buy_relay_only(
                channel_handles=["upbit_news", "BithumbExchange"],
                on_ready=_on_ready,
            )
        )

    relay = _FakeRelay.instances[-1]
    native_start = next(line for line in relay.raw_lines if line.startswith("__native_start__\t"))
    wait_index = relay.raw_lines.index("__wait_native_status__")
    assert relay.raw_lines.index(native_start) < wait_index
    assert "-1002562064658:upbit_news" in native_start
    assert "-1001202540487:BithumbExchange" in native_start
    assert ready_calls == ["ready"]
    assert relay.attached is False
    assert relay.closed is True
    assert "REQUEST:searchPublicChat" not in relay.raw_lines


def test_tdlib_native_relay_only_requires_native_buy_active(monkeypatch, tmp_path):
    _FakeRelay.instances.clear()
    monkeypatch.setattr(tdlib_module, "_TdlibRelay", _FakeRelay)
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ACTIVE", "0")
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ENABLED", "1")

    client = TdlibRealtimeChannelClient(
        api_id=1,
        api_hash="hash",
        phone="+10000000000",
        relay_path=tmp_path / "fake-relay",
        database_dir=tmp_path / "tdlib-db",
    )

    with pytest.raises(RuntimeError, match="LISTING_TDLIB_NATIVE_BUY_ACTIVE=1"):
        asyncio.run(client.run_native_buy_relay_only(["upbit_news"]))

    assert _FakeRelay.instances == []
