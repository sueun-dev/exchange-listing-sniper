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

from src.race_realtime_client import RaceRealtimeChannelClient, _FirstArrivalGate  # noqa: E402


class _FakeRealtimeClient:
    def __init__(self, *, configured=True, session=True, error: Exception | None = None):
        self._configured = configured
        self._session = session
        self._error = error

    def is_configured(self):
        return self._configured

    def has_session_file(self):
        return self._session

    async def run(self, **_kwargs):
        if self._error is not None:
            raise self._error
        await asyncio.sleep(3600)


class _EmittingRealtimeClient(_FakeRealtimeClient):
    def __init__(self, posts):
        super().__init__()
        self.posts = posts

    async def run(self, **kwargs):
        on_post = kwargs["on_post"]
        for post in self.posts:
            maybe_result = on_post(post)
            if hasattr(maybe_result, "__await__"):
                await maybe_result


def test_first_arrival_gate_dedupes_per_channel():
    gate = _FirstArrivalGate()

    assert gate.claim("upbit_news", 100)
    assert not gate.claim("upbit_news", 100)
    assert gate.claim("upbit_news", 99)
    assert gate.claim("BithumbExchange", 100)
    assert gate.claim("upbit_news", 101)


def test_first_arrival_gate_evicts_old_entries():
    gate = _FirstArrivalGate(max_entries=2)

    assert gate.claim("upbit_news", 100)
    assert gate.claim("upbit_news", 101)
    assert not gate.claim("upbit_news", 100)
    assert gate.claim("BithumbExchange", 200)
    assert gate.claim("upbit_news", 100)


def test_race_required_backend_missing_session_fails_fast():
    client = RaceRealtimeChannelClient(
        telethon_client=_FakeRealtimeClient(),
        tdlib_client=_FakeRealtimeClient(session=False),
        pyrogram_client=None,
    )

    with pytest.raises(RuntimeError, match="tdlib"):
        asyncio.run(
            client.run(
                channel_handles=["upbit_news"],
                on_post=lambda _post: None,
                required_backends={"tdlib"},
            )
        )


def test_race_required_backend_runtime_failure_propagates():
    client = RaceRealtimeChannelClient(
        telethon_client=_FakeRealtimeClient(),
        tdlib_client=_FakeRealtimeClient(error=RuntimeError("tdlib native failed")),
        pyrogram_client=None,
    )

    with pytest.raises(RuntimeError, match="tdlib native failed"):
        asyncio.run(
            client.run(
                channel_handles=["upbit_news"],
                on_post=lambda _post: None,
                required_backends={"tdlib"},
            )
        )


def test_race_min_ready_backends_fails_when_only_one_session_ready():
    client = RaceRealtimeChannelClient(
        telethon_client=_FakeRealtimeClient(session=True),
        tdlib_client=_FakeRealtimeClient(session=False),
        pyrogram_client=None,
    )

    with pytest.raises(RuntimeError, match="세션 수 부족"):
        asyncio.run(
            client.run(
                channel_handles=["upbit_news"],
                on_post=lambda _post: None,
                min_ready_backends=2,
            )
        )


def test_race_drops_normal_duplicate_post():
    first = {
        "channel_handle": "BithumbExchange",
        "message_id": 321987,
        "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
    }
    duplicate = dict(first)
    seen = []
    client = RaceRealtimeChannelClient(
        telethon_client=_EmittingRealtimeClient([first]),
        tdlib_client=_EmittingRealtimeClient([duplicate]),
        pyrogram_client=None,
    )

    asyncio.run(client.run(channel_handles=["BithumbExchange"], on_post=seen.append))

    assert seen == [first]


def test_race_forwards_duplicate_native_trade_for_proof():
    first = {
        "channel_handle": "BithumbExchange",
        "message_id": 321987,
        "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
    }
    native_duplicate = {
        **first,
        "native_listing": {
            "signal_type": "market_add",
            "ticker": "STRK",
            "tickers": ["STRK"],
            "asset_name": "스타크넷",
            "markets": ["KRW"],
        },
        "native_trade": {
            "attempted": True,
            "executed": False,
            "reason": "tdlib_native_rest_preflight",
            "symbol": "STRKUSDT",
            "order_link_id": "ls-b-321987-STRK",
        },
    }
    seen = []
    client = RaceRealtimeChannelClient(
        telethon_client=_EmittingRealtimeClient([first]),
        tdlib_client=_EmittingRealtimeClient([native_duplicate]),
        pyrogram_client=None,
    )

    asyncio.run(client.run(channel_handles=["BithumbExchange"], on_post=seen.append))

    assert seen == [first, native_duplicate]
