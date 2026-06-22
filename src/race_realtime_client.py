"""Race Telethon, TDLib, and Pyrogram; accept the first arrival for each Telegram post."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict

from .tdlib_realtime_client import TdlibRealtimeChannelClient
from .telegram_realtime_client import RealtimeTelegramChannelClient

logger = logging.getLogger(__name__)

# Auto-reconnect backoff for a dropped backend. A backend that stays up longer
# than the stable-reset window is treated as a transient blip and reconnects
# from the initial (fast) delay; a persistently failing one backs off up to max.
RECONNECT_INITIAL_BACKOFF_SEC = 1.0
RECONNECT_MAX_BACKOFF_SEC = 30.0
RECONNECT_STABLE_RESET_SEC = 60.0

# Lazy import: Pyrogram may not be installed on all environments.
_pyrogram_client_class = None


def _get_pyrogram_client_class():
    global _pyrogram_client_class
    if _pyrogram_client_class is not None:
        return _pyrogram_client_class
    try:
        from .pyrogram_realtime_client import PyrogramRealtimeChannelClient

        _pyrogram_client_class = PyrogramRealtimeChannelClient
    except ImportError:
        _pyrogram_client_class = None
    return _pyrogram_client_class


def _has_native_trade(post: dict) -> bool:
    if isinstance(post.get("native_trade"), dict):
        return True
    native_trades = post.get("native_trades")
    return isinstance(native_trades, list) and any(
        isinstance(trade, dict) for trade in native_trades
    )


class _FirstArrivalGate:
    def __init__(self, max_entries: int = 8192):
        self._max_entries = max(1, int(max_entries))
        self._seen: OrderedDict[tuple[str, int], None] = OrderedDict()

    def claim(self, channel_handle: str, message_id: int) -> bool:
        if not isinstance(message_id, int):
            message_id = int(message_id)
        key = (channel_handle, message_id)
        if key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)
        return True


class RaceRealtimeChannelClient:
    """Run all available realtime backends and forward only the earliest event per post."""

    _UNSET = object()

    def __init__(
        self,
        telethon_client: RealtimeTelegramChannelClient | None = None,
        tdlib_client: TdlibRealtimeChannelClient | None = None,
        pyrogram_client=_UNSET,
        gate_max_entries: int = 8192,
    ):
        self.telethon = telethon_client or RealtimeTelegramChannelClient()
        self.tdlib = tdlib_client or TdlibRealtimeChannelClient()
        self._gate = _FirstArrivalGate(max_entries=gate_max_entries)
        # Reconnect tuning (overridable in tests).
        self._reconnect_initial_sec = RECONNECT_INITIAL_BACKOFF_SEC
        self._reconnect_max_sec = RECONNECT_MAX_BACKOFF_SEC
        self._reconnect_stable_reset_sec = RECONNECT_STABLE_RESET_SEC

        # Auto-create Pyrogram client only when not explicitly passed
        if pyrogram_client is not self._UNSET:
            # Caller explicitly set pyrogram_client (including None = disable)
            self.pyrogram = pyrogram_client
        else:
            cls = _get_pyrogram_client_class()
            if cls is not None:
                try:
                    self.pyrogram = cls()
                except Exception:
                    self.pyrogram = None
            else:
                self.pyrogram = None

    def _configured_backends(self) -> list[tuple[str, object]]:
        backends: list[tuple[str, object]] = []
        if self.telethon.is_configured():
            backends.append(("telethon", self.telethon))
        if self.tdlib.is_configured():
            backends.append(("tdlib", self.tdlib))
        if self.pyrogram is not None and self.pyrogram.is_configured():
            backends.append(("pyrogram", self.pyrogram))
        return backends

    def _session_ready_backends(self) -> list[tuple[str, object]]:
        return [
            (name, client)
            for name, client in self._configured_backends()
            if client.has_session_file()
        ]

    def is_configured(self) -> bool:
        return bool(self._configured_backends())

    def has_session_file(self) -> bool:
        return bool(self._session_ready_backends())

    async def login_interactive(self) -> bool:
        successes = 0
        for name, client in self._configured_backends():
            try:
                if await client.login_interactive():
                    successes += 1
            except Exception as exc:
                logger.warning("%s login failed (non-fatal): %s", name, exc)
        return successes > 0

    async def run(
        self,
        channel_handles: list[str],
        on_post,
        minimal_post: bool = False,
        trade_post: bool = False,
        required_backends: set[str] | None = None,
        min_ready_backends: int = 1,
    ):
        required_backend_names = set(required_backends or ())
        min_ready_backends = max(1, int(min_ready_backends))

        async def _first_wins(post: dict):
            claimed = self._gate.claim(
                post["channel_handle"],
                post["message_id"],
            )
            if not claimed and not _has_native_trade(post):
                return None
            maybe_result = on_post(post)
            if hasattr(maybe_result, "__await__"):
                return await maybe_result
            return maybe_result

        async def _run_backend(name: str, client):
            # Supervisor loop: keep the backend connected. A drop (exception OR a
            # clean return from run()) triggers an automatic reconnect with
            # exponential backoff + jitter, so a transient network blip or a
            # relay restart never permanently removes a receiver. Only an
            # explicit cancellation (shutdown) ends the loop.
            backoff = self._reconnect_initial_sec
            while True:
                started = time.monotonic()
                try:
                    await client.run(
                        channel_handles=channel_handles,
                        on_post=_first_wins,
                        minimal_post=minimal_post,
                        trade_post=trade_post,
                    )
                    drop_reason = "stream ended"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    drop_reason = str(exc) or exc.__class__.__name__
                if time.monotonic() - started >= self._reconnect_stable_reset_sec:
                    backoff = self._reconnect_initial_sec
                delay = backoff + random.uniform(0.0, backoff * 0.25)
                emit = logger.error if name in required_backend_names else logger.warning
                emit(
                    "%s 백엔드 끊김(%s) — %.1fs 후 자동 재연결",
                    name,
                    drop_reason,
                    delay,
                )
                await asyncio.sleep(delay)
                backoff = min(backoff * 2.0, self._reconnect_max_sec)

        active_backends = self._session_ready_backends()
        if not active_backends:
            raise RuntimeError(
                "race realtime 백엔드에 로그인된 세션이 없습니다. "
                "Telethon/TDLib/Pyrogram 중 하나 이상 로그인하세요."
            )

        backend_names = [name for name, _ in active_backends]
        if len(backend_names) < min_ready_backends:
            raise RuntimeError(
                "race realtime 백엔드 세션 수 부족: "
                f"ready={len(backend_names)} required={min_ready_backends} "
                f"active={', '.join(backend_names) or 'none'}"
            )
        missing_required = sorted(required_backend_names.difference(backend_names))
        if missing_required:
            raise RuntimeError(
                "race realtime 필수 백엔드 세션이 없습니다: "
                + ", ".join(missing_required)
            )
        logger.info("%d-way race 활성: %s", len(backend_names), " + ".join(name.title() for name in backend_names))
        tasks = [
            asyncio.create_task(
                _run_backend(name, client),
                name=f"{name}-race-listener",
            )
            for name, client in active_backends
        ]

        try:
            # Each backend self-heals via its own reconnect loop, so this only
            # returns when the run is cancelled (shutdown).
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
