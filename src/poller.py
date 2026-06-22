"""Main poller for exchange listing announcements."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from .announcement_filter import (
    extract_listing_assets,
    extract_primary_ticker,
    has_multiple_listing_assets_fast,
    make_listing_title_classifier,
)
from .latency import LatencyTrace, NOOP_LATENCY_TRACE
from .payload_normalization import string_list

if TYPE_CHECKING:
    from .bybit_client import BybitClient as BybitClientType
    from .bybit_spot_buyer import BybitSpotBuyer as BybitSpotBuyerType
    from .channel_client import TelegramChannelClient as TelegramChannelClientType
    from .cpp_ultra_engine import CppUltraListingEngineBridge as CppUltraListingEngineBridgeType
    from .signal_emitter import SignalEmitter as SignalEmitterType
    from .source_emitter import SourceEventEmitter as SourceEventEmitterType
    from .state_store import StateStore as StateStoreType

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent.parent / "config" / "channels.json"
HOT_SEEN_MAX_ENTRIES_PER_CHANNEL = 8192
# Warn if the local clock drifts this far from Bybit's server clock during a
# long-running session. Bybit signs orders against recv_window (default 5000ms),
# so sustained drift past this makes every buy fail auth at fire time. The
# preflight clock gate only checks at startup; this re-checks on keep-warm.
CLOCK_SKEW_WARN_MS = 2000.0

BybitClient = None
BybitSpotBuyer = None
TelegramChannelClient = None
CppUltraListingEngineBridge = None
SignalEmitter = None
SourceEventEmitter = None
StateStore = None


def _get_bybit_client_class():
    global BybitClient
    if BybitClient is None:
        from .bybit_client import BybitClient as loaded_class

        BybitClient = loaded_class
    return BybitClient


def _get_spot_buyer_class():
    global BybitSpotBuyer
    if BybitSpotBuyer is None:
        from .bybit_spot_buyer import BybitSpotBuyer as loaded_class

        BybitSpotBuyer = loaded_class
    return BybitSpotBuyer


def _get_channel_client_class():
    global TelegramChannelClient
    if TelegramChannelClient is None:
        from .channel_client import TelegramChannelClient as loaded_class

        TelegramChannelClient = loaded_class
    return TelegramChannelClient


def _get_cpp_ultra_engine_class():
    global CppUltraListingEngineBridge
    if CppUltraListingEngineBridge is None:
        from .cpp_ultra_engine import CppUltraListingEngineBridge as loaded_class

        CppUltraListingEngineBridge = loaded_class
    return CppUltraListingEngineBridge


def _get_signal_emitter_class():
    global SignalEmitter
    if SignalEmitter is None:
        from .signal_emitter import SignalEmitter as loaded_class

        SignalEmitter = loaded_class
    return SignalEmitter


def _get_source_emitter_class():
    global SourceEventEmitter
    if SourceEventEmitter is None:
        from .source_emitter import SourceEventEmitter as loaded_class

        SourceEventEmitter = loaded_class
    return SourceEventEmitter


def _get_state_store_class():
    global StateStore
    if StateStore is None:
        from .state_store import StateStore as loaded_class

        StateStore = loaded_class
    return StateStore


class _ChannelRuntime:
    __slots__ = (
        "channel_handle",
        "channel_id",
        "classify_title",
        "classify_title_fast",
        "display_name",
        "exchange",
        "order_link_prefix",
    )

    def __init__(
        self,
        *,
        channel_id: str,
        channel_handle: str,
        exchange: str,
        display_name: str,
        order_link_prefix: str,
        classify_title: Callable[[str], dict | None],
        classify_title_fast: Callable[[str], dict | None],
    ):
        self.channel_id = channel_id
        self.channel_handle = channel_handle
        self.exchange = exchange
        self.display_name = display_name
        self.order_link_prefix = order_link_prefix
        self.classify_title = classify_title
        self.classify_title_fast = classify_title_fast


class ExchangeListingPoller:
    """Poll official Telegram channels for listing announcements."""

    def __init__(
        self,
        config_file: Path | str = CONFIG_FILE,
        poll_interval: int = 15,
        channel_client: TelegramChannelClientType | None = None,
        bybit_client: BybitClientType | None = None,
        spot_buyer: BybitSpotBuyerType | None = None,
        state_store: StateStoreType | None = None,
        signal_emitter: SignalEmitterType | None = None,
        source_emitter: SourceEventEmitterType | None = None,
        cpp_ultra_engine: CppUltraListingEngineBridgeType | None = None,
        enable_trading: bool = True,
        defer_persistence: bool = False,
        prefer_cached_lookup: bool = False,
        latency_trace_enabled: bool = False,
        keep_warm_enabled: bool = False,
        keep_warm_interval_sec: int = 30,
        persist_source_events: bool = True,
        state_flush_interval_sec: float = 1.0,
        enable_bybit_warmup: bool = True,
        enable_channel_client: bool = True,
        enable_python_spot_buyer: bool = True,
        enable_cpp_ultra_warmup: bool = True,
        require_cpp_ultra_warmup: bool = False,
        defer_post_trade_work: bool = False,
        hot_state_enabled: bool = False,
        emit_ultra_ack: bool = True,
    ):
        self.poll_interval = poll_interval
        self.config = self._load_config(config_file)
        self._channels_by_id = {
            channel["id"]: channel for channel in self.config["channels"]
        }
        self._channel_ids_by_handle = {
            channel["channel_handle"].lstrip("@"): channel["id"]
            for channel in self.config["channels"]
        }
        self._order_link_prefix_by_channel_id = {
            channel["id"]: f"ls-{self._exchange_code(channel['exchange'])}-"
            for channel in self.config["channels"]
        }
        self._channel_runtime_by_id = {
            channel["id"]: _ChannelRuntime(
                channel_id=channel["id"],
                channel_handle=channel["channel_handle"],
                exchange=channel["exchange"],
                display_name=channel["display_name"],
                order_link_prefix=self._order_link_prefix_by_channel_id[channel["id"]],
                classify_title=make_listing_title_classifier(
                    exchange=channel["exchange"],
                    display_name=channel["display_name"],
                ),
                classify_title_fast=make_listing_title_classifier(
                    exchange=channel["exchange"],
                    display_name=channel["display_name"],
                    minimal=True,
                ),
            )
            for channel in self.config["channels"]
        }
        self.channel_client = channel_client
        if self.channel_client is None and enable_channel_client:
            self.channel_client = _get_channel_client_class()()
        self.bybit_client = bybit_client
        self.spot_buyer = spot_buyer
        if self.spot_buyer is None and enable_trading and enable_python_spot_buyer:
            self.spot_buyer = _get_spot_buyer_class()(market_client=self._ensure_bybit_client())
        self.state_store = state_store or _get_state_store_class()()
        self.signal_emitter = signal_emitter
        self.source_emitter = source_emitter
        self.cpp_ultra_engine = cpp_ultra_engine
        if self.cpp_ultra_engine is None and enable_cpp_ultra_warmup:
            self.cpp_ultra_engine = _get_cpp_ultra_engine_class()()
        self.enable_trading = enable_trading
        self.defer_persistence = defer_persistence
        self.prefer_cached_lookup = prefer_cached_lookup
        self.latency_trace_enabled = latency_trace_enabled
        self.keep_warm_enabled = keep_warm_enabled
        self.keep_warm_interval_sec = max(5, int(keep_warm_interval_sec))
        self.persist_source_events = persist_source_events
        self.state_flush_interval_sec = max(0.0, float(state_flush_interval_sec))
        self.enable_bybit_warmup = enable_bybit_warmup
        self.enable_channel_client = enable_channel_client
        self.enable_python_spot_buyer = enable_python_spot_buyer
        self.enable_cpp_ultra_warmup = enable_cpp_ultra_warmup
        self.require_cpp_ultra_warmup = bool(require_cpp_ultra_warmup)
        self.defer_post_trade_work = defer_post_trade_work
        self.hot_state_enabled = bool(hot_state_enabled and self.defer_persistence)
        self.emit_ultra_ack = bool(emit_ultra_ack)
        self._cpp_ultra_hot_path_enabled = (
            self.defer_post_trade_work
            and self.enable_trading
            and self.enable_cpp_ultra_warmup
            and self.cpp_ultra_engine is not None
            and self.cpp_ultra_engine.is_enabled()
        )
        self._bg_executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="listing-sniper-bg")
            if self.defer_persistence or self.defer_post_trade_work
            else None
        )
        self._warm_stop = threading.Event()
        self._warm_thread: threading.Thread | None = None
        self._state_flush_stop = threading.Event()
        self._state_flush_thread: threading.Thread | None = None
        self._state_dirty_epoch = 0
        self._state_flushed_epoch = 0
        if self.hot_state_enabled:
            snapshot = self.state_store.snapshot_last_seen()
            seen_snapshot_fn = getattr(self.state_store, "snapshot_seen_message_ids", None)
            seen_snapshot = seen_snapshot_fn() if callable(seen_snapshot_fn) else {}
            hot_seen = {
                channel_id: self._ordered_recent_seen_ids(
                    seen_snapshot.get(channel_id, [])
                )
                for channel_id in self._channels_by_id
            }
            self._hot_start_last_seen = {
                channel_id: int(snapshot.get(channel_id, 0))
                for channel_id in self._channels_by_id
            }
            self._hot_last_seen = {
                channel_id: max(
                    [int(snapshot.get(channel_id, 0)), *hot_seen[channel_id].keys()]
                )
                for channel_id in self._channels_by_id
            }
            self._hot_seen_message_ids = hot_seen
        else:
            self._hot_start_last_seen = {}
            self._hot_last_seen = {}
            self._hot_seen_message_ids = {}
        refresh_client = self._ensure_bybit_client() if self.enable_bybit_warmup else self.bybit_client
        refresh = getattr(refresh_client, "refresh_market_cache", None)
        if self.enable_bybit_warmup and callable(refresh):
            try:
                refresh()
            except Exception as exc:  # pragma: no cover - warmup safeguard
                logger.warning("Bybit 심볼 캐시 사전 로드 실패: %s", exc)
        if (
            self.enable_cpp_ultra_warmup
            and self.cpp_ultra_engine is not None
            and self.cpp_ultra_engine.is_enabled()
        ):
            try:
                warmup_result = self.cpp_ultra_engine.warmup()
                if (
                    self.require_cpp_ultra_warmup
                    and isinstance(warmup_result, dict)
                    and not warmup_result.get("ok")
                ):
                    raise RuntimeError(f"C++ ultra engine warmup not ready: {warmup_result}")
            except Exception as exc:  # pragma: no cover - warmup safeguard
                if self.require_cpp_ultra_warmup:
                    raise RuntimeError("C++ ultra engine warmup is required") from exc
                logger.warning("C++ ultra engine warmup 실패: %s", exc)
        if self.defer_persistence and self.state_flush_interval_sec > 0:
            self._start_state_flush_thread()
        if self.enable_bybit_warmup and self.keep_warm_enabled:
            self._start_keep_warm_thread()
        self._process_post_impl = self._select_process_post_impl()

    def _load_config(self, config_file: Path | str) -> dict:
        with open(config_file) as handle:
            return json.load(handle)

    def _ensure_bybit_client(self):
        if self.bybit_client is None:
            self.bybit_client = _get_bybit_client_class()()
        return self.bybit_client

    def _ensure_signal_emitter(self):
        if self.signal_emitter is None:
            self.signal_emitter = _get_signal_emitter_class()()
        return self.signal_emitter

    def _ensure_source_emitter(self):
        if self.source_emitter is None:
            self.source_emitter = _get_source_emitter_class()()
        return self.source_emitter

    def _get_channel(self, channel_id: str) -> dict | None:
        return self._channels_by_id.get(channel_id)

    def get_channel_handles(self, channel_id: str | None = None) -> list[str]:
        if channel_id is not None:
            channel = self._get_channel(channel_id)
            return [channel["channel_handle"]] if channel else []
        return [channel["channel_handle"] for channel in self.config["channels"]]

    def get_channel_id_by_handle(self, channel_handle: str) -> str | None:
        return self._channel_ids_by_handle.get(channel_handle.lstrip("@"))

    def process_post(self, channel_id: str, post: dict) -> dict | list[dict] | None:
        return self._process_post_impl(channel_id, post)

    def _select_process_post_impl(self):
        if (
            self.defer_post_trade_work
            and not self.emit_ultra_ack
            and not self.latency_trace_enabled
        ):
            if self._cpp_ultra_hot_path_enabled:
                return self._process_post_cpp_ultra_fire_fast
            return self._process_post_ultra_fire_fast
        return self._process_post_general

    def _process_post_general(self, channel_id: str, post: dict) -> dict | list[dict] | None:

        trace = (
            LatencyTrace(enabled=True)
            if self.latency_trace_enabled
            else NOOP_LATENCY_TRACE
        )
        channel = self._channel_runtime_by_id.get(channel_id)
        if channel is None:
            logger.error("거래소 설정 없음: %s", channel_id)
            return None

        message_id = int(post["message_id"])
        native_trades = self._extract_native_trades(post)
        if (
            native_trades is None
            and self._cpp_ultra_hot_path_enabled
            and not self._has_multiple_tickers_fast(post.get("title", ""))
        ):
            signal = self._process_post_cpp_ultra(
                channel_id=channel_id,
                channel=channel,
                post=post,
                message_id=message_id,
                trace=trace,
            )
            if signal is not None:
                return signal

        marked = self._mark_seen(channel_id, message_id)
        if not marked:
            self._submit_duplicate_native_trades(
                post=post,
                channel=channel,
                native_trades=native_trades,
            )
            return None
        trace.mark("dedup")

        if self.defer_persistence:
            self._mark_state_dirty()

        listing = self._prepare_native_listing(post=post, channel=channel)
        if listing is not None:
            trace.mark("classify_native")
        else:
            listing = channel.classify_title(post.get("title", ""))
            if listing is None:
                return None
            trace.mark("classify")

        listings = self._filter_new_listing_tickers(
            channel_id=channel_id,
            message_id=message_id,
            listings=self._expand_listing_by_ticker(listing),
        )
        if not listings:
            return None
        if native_trades is not None:
            trades = self._native_or_python_trades_for_listings(
                channel=channel,
                post=post,
                listings=listings,
                native_trades=native_trades,
            )
        else:
            trades = self._maybe_buy_spots(channel=channel, post=post, listings=listings)
        trace.mark("trade")
        if self.defer_post_trade_work:
            for item, trade in zip(listings, trades):
                self._submit_background(
                    self._finalize_post_trade_work,
                    trace,
                    post,
                    item,
                    trade,
                )
            if not self.emit_ultra_ack:
                return None
            signals = [
                self._build_ultra_trade_ack(trace, post, item, trade)
                for item, trade in zip(listings, trades)
            ]
            return signals[0] if len(signals) == 1 else signals
        signals = []
        for item, trade in zip(listings, trades):
            signal_emitter = self._ensure_signal_emitter()
            self._log_trade_latency(post=post, listing=item, trade=trade)
            bybit = self._lookup_bybit_snapshot(item["ticker"])
            trace.mark("bybit_snapshot")
            initial_latency = self._build_latency_payload(trace, post, item, trade)
            signal = signal_emitter.build(
                post=post,
                listing=item,
                bybit=bybit,
                trade=trade,
                latency=initial_latency,
            )
            trace.mark("build_signal")
            final_latency = self._build_latency_payload(trace, post, item, trade)
            if final_latency is not None:
                signal["latency"] = final_latency
            if self.defer_persistence:
                self._submit_background(signal_emitter.persist, signal)
            else:
                signal_emitter.persist(signal)
            logger.info(
                "[%s] 상장 공지 감지: %s (%s)",
                channel_id,
                item["asset_name"],
                item["ticker"],
            )
            signals.append(signal)
        return signals[0] if len(signals) == 1 else signals

    def _process_post_ultra_fire_fast(self, channel_id: str, post: dict) -> dict | None:
        channel = self._channel_runtime_by_id.get(channel_id)
        if channel is None:
            logger.error("거래소 설정 없음: %s", channel_id)
            return None

        message_id = int(post["message_id"])
        has_native_trade_payload = "native_trades" in post or "native_trade" in post
        native_trades = (
            self._extract_native_trades(post)
            if has_native_trade_payload
            else None
        )
        marked = self._mark_seen(channel_id, message_id)
        if not marked:
            self._submit_duplicate_native_trades(
                post=post,
                channel=channel,
                native_trades=native_trades,
            )
            return None

        if self.defer_persistence:
            self._mark_state_dirty()

        listing = self._prepare_native_listing(post=post, channel=channel)
        if listing is None:
            listing = channel.classify_title_fast(post.get("title", ""))
            if listing is None:
                return None

        listings = self._filter_new_listing_tickers(
            channel_id=channel_id,
            message_id=message_id,
            listings=self._expand_listing_by_ticker(listing),
        )
        if not listings:
            return None
        if native_trades is not None:
            trades = self._native_or_python_trades_for_listings(
                channel=channel,
                post=post,
                listings=listings,
                native_trades=native_trades,
            )
        else:
            trades = self._maybe_buy_spots(channel=channel, post=post, listings=listings)
        for item, trade in zip(listings, trades):
            self._submit_background(
                self._finalize_post_trade_work,
                NOOP_LATENCY_TRACE,
                post,
                item,
                trade,
            )
        return None

    def _process_post_cpp_ultra_fire_fast(self, channel_id: str, post: dict) -> dict | None:
        channel = self._channel_runtime_by_id.get(channel_id)
        if channel is None:
            logger.error("거래소 설정 없음: %s", channel_id)
            return None

        message_id = post["message_id"]
        if not isinstance(message_id, int):
            message_id = int(message_id)
        has_native_trade_payload = "native_trades" in post or "native_trade" in post
        if not self._would_mark_seen(channel_id, message_id):
            native_trades = (
                self._extract_native_trades(post)
                if has_native_trade_payload
                else None
            )
            self._submit_duplicate_native_trades(
                post=post,
                channel=channel,
                native_trades=native_trades,
            )
            return None

        native_trades = (
            self._extract_native_trades(post)
            if has_native_trade_payload
            else None
        )
        if native_trades:
            listing = self._prepare_native_listing(post=post, channel=channel)
            if listing is None:
                return None
            self._remember_seen(channel_id, message_id)
            if self.defer_persistence:
                self._mark_state_dirty()

            listings = self._expand_listing_by_ticker(listing)
            tickers = [item.get("ticker") for item in listings]
            expected_trade_count = len([ticker for ticker in tickers if ticker]) or 1
            if len(native_trades) >= expected_trade_count:
                self._submit_background(
                    self._finalize_native_trades_post_trade_work,
                    post,
                    channel,
                    list(native_trades),
                )
                return None
            listings = self._filter_new_listing_tickers(
                channel_id=channel_id,
                message_id=message_id,
                listings=listings,
            )
            if not listings:
                return None
            trades = self._native_or_python_trades_for_listings(
                channel=channel,
                post=post,
                listings=listings,
                native_trades=native_trades,
            )
            for item, trade in zip(listings, trades):
                self._submit_background(
                    self._finalize_post_trade_work,
                    NOOP_LATENCY_TRACE,
                    post,
                    item,
                    trade,
            )
            return None

        title = post.get("title", "")
        seen_ticker = self._title_ticker_already_seen(channel_id, title)
        if seen_ticker is not None:
            logger.info(
                "[%s] C++ ultra 이전 중복 상장 티커 스킵: %s (message_id=%s)",
                channel_id,
                seen_ticker,
                message_id,
            )
            self._remember_seen(channel_id, message_id)
            if self.defer_persistence:
                self._mark_state_dirty()
            return None
        raw_result = self.cpp_ultra_engine.handle_post_raw(
            exchange=channel.exchange,
            message_id=message_id,
            title=title,
        )
        if raw_result.duplicate:
            return None
        if not raw_result.matched:
            reason = getattr(raw_result, "reason", b"")
            if bytes(reason).split(b"\0", 1)[0] == b"multi_ticker":
                return self._process_post_ultra_fire_fast(channel_id, post)
            return None
        self._remember_seen(channel_id, message_id)
        if self.defer_persistence:
            self._mark_state_dirty()
        raw_payload = self.cpp_ultra_engine.payload_from_raw(
            raw_result,
            exchange=channel.exchange,
            message_id=message_id,
        )
        if raw_payload is not None and not self._remember_listing_ticker(
            channel_id=channel_id,
            message_id=message_id,
            ticker=str(raw_payload["ticker"]),
        ):
            logger.info(
                "[%s] C++ ultra 중복 상장 티커 finalize 스킵: %s (message_id=%s)",
                channel_id,
                raw_payload["ticker"],
                message_id,
            )
            return None
        self._submit_background(
            self._finalize_cpp_ultra_raw_post_trade_work,
            NOOP_LATENCY_TRACE,
            post,
            channel,
            raw_payload,
            0,
            0,
        )
        return None

    def poll_exchange(self, channel_id: str) -> list[dict]:
        channel = self._get_channel(channel_id)
        if channel is None:
            logger.error("거래소 설정 없음: %s", channel_id)
            return []

        if self.channel_client is None:
            self.channel_client = _get_channel_client_class()()
        posts = self.channel_client.fetch_recent_posts(channel["channel_handle"])
        posts.sort(key=lambda post: post["message_id"])

        signals = []

        for post in posts:
            signal = self.process_post(channel_id, post)
            if signal is not None:
                if isinstance(signal, list):
                    signals.extend(signal)
                else:
                    signals.append(signal)

        return signals

    def _process_post_cpp_ultra(
        self,
        *,
        channel_id: str,
        channel: _ChannelRuntime,
        post: dict,
        message_id: int,
        trace: LatencyTrace,
    ) -> dict | None:
        if not self._would_mark_seen(channel_id, message_id):
            return None
        title = post.get("title", "")
        seen_ticker = self._title_ticker_already_seen(channel_id, title)
        if seen_ticker is not None:
            logger.info(
                "[%s] C++ ultra 이전 중복 상장 티커 스킵: %s (message_id=%s)",
                channel_id,
                seen_ticker,
                message_id,
            )
            self._remember_seen(channel_id, message_id)
            if self.defer_persistence:
                self._mark_state_dirty()
            return None
        measure_trade_timing = self.emit_ultra_ack or self.latency_trace_enabled
        trade_started_ns = time.monotonic_ns() if measure_trade_timing else 0
        if self.emit_ultra_ack:
            result = self.cpp_ultra_engine.handle_post(
                exchange=channel.exchange,
                message_id=message_id,
                title=title,
            )
        else:
            raw_result = self.cpp_ultra_engine.handle_post_raw(
                exchange=channel.exchange,
                message_id=message_id,
                title=title,
            )
            result = None
        trade_finished_ns = time.monotonic_ns() if measure_trade_timing else 0
        trace.mark("cpp_ultra")
        if not self.emit_ultra_ack:
            if raw_result.duplicate or not raw_result.matched:
                return None
            self._remember_seen(channel_id, message_id)
            if self.defer_persistence:
                self._mark_state_dirty()
            raw_payload = self.cpp_ultra_engine.payload_from_raw(
                raw_result,
                exchange=channel.exchange,
                message_id=message_id,
            )
            if raw_payload is not None and not self._remember_listing_ticker(
                channel_id=channel_id,
                message_id=message_id,
                ticker=str(raw_payload["ticker"]),
            ):
                logger.info(
                    "[%s] C++ ultra 중복 상장 티커 finalize 스킵: %s (message_id=%s)",
                    channel_id,
                    raw_payload["ticker"],
                    message_id,
                )
                return None
            self._submit_background(
                self._finalize_cpp_ultra_raw_post_trade_work,
                trace,
                post,
                channel,
                raw_payload,
                trade_started_ns,
                trade_finished_ns,
            )
            return None
        if result is None or result.get("duplicate"):
            return None
        self._remember_seen(channel_id, message_id)
        if self.defer_persistence:
            self._mark_state_dirty()
        if not self._remember_listing_ticker(
            channel_id=channel_id,
            message_id=message_id,
            ticker=str(result["ticker"]),
        ):
            logger.info(
                "[%s] C++ ultra 중복 상장 티커 finalize 스킵: %s (message_id=%s)",
                channel_id,
                result["ticker"],
                message_id,
            )
            return None
        listing = {
            "exchange": channel.exchange,
            "display_name": channel.display_name,
            "signal_type": result["signal_type"],
            "ticker": result["ticker"],
            "tickers": result.get("tickers") or [result["ticker"]],
            "asset_name": result["asset_name"],
            "markets": result["markets"],
        }
        self._attach_post_assets_to_listing(post=post, listing=listing)
        listings = self._expand_listing_by_ticker(listing)
        trades = list(result.get("trades") or [result["trade"]])
        trade_elapsed_ns = (
            max(0, trade_finished_ns - trade_started_ns)
            if trade_started_ns and trade_finished_ns
            else 0
        )
        for trade in trades:
            trade.setdefault("trade_started_monotonic_ns", int(trade_started_ns))
            trade["trade_finished_monotonic_ns"] = int(trade_finished_ns)
            trade["trade_elapsed_ns"] = int(trade_elapsed_ns)
            trade["trade_elapsed_us"] = trade_elapsed_ns / 1_000.0
            trade["trade_elapsed_ms"] = trade_elapsed_ns / 1_000_000.0
        signal = self._build_ultra_trade_ack(trace, post, listings[0], trades[0])
        for item, trade in zip(listings, trades):
            self._submit_background(
                self._finalize_post_trade_work,
                trace,
                post,
                item,
                trade,
            )
        return signal

    def _finalize_cpp_ultra_raw_post_trade_work(
        self,
        trace: LatencyTrace,
        post: dict,
        channel: _ChannelRuntime,
        result: dict | None,
        trade_started_ns: int,
        trade_finished_ns: int,
    ):
        if result is None or result.get("duplicate"):
            return
        listing = {
            "exchange": channel.exchange,
            "display_name": channel.display_name,
            "signal_type": result["signal_type"],
            "ticker": result["ticker"],
            "tickers": result.get("tickers") or [result["ticker"]],
            "asset_name": result["asset_name"],
            "markets": result["markets"],
        }
        self._attach_post_assets_to_listing(post=post, listing=listing)
        listings = self._expand_listing_by_ticker(listing)
        trades = list(result.get("trades") or [result["trade"]])
        trade_elapsed_ns = (
            max(0, trade_finished_ns - trade_started_ns)
            if trade_started_ns and trade_finished_ns
            else 0
        )
        for item, trade in zip(listings, trades):
            trade.setdefault("trade_started_monotonic_ns", int(trade_started_ns))
            trade["trade_finished_monotonic_ns"] = int(trade_finished_ns)
            trade["trade_elapsed_ns"] = int(trade_elapsed_ns)
            trade["trade_elapsed_us"] = trade_elapsed_ns / 1_000.0
            trade["trade_elapsed_ms"] = trade_elapsed_ns / 1_000_000.0
            self._finalize_post_trade_work(trace, post, item, trade)

    def poll_all(self) -> list[dict]:
        signals = []
        for channel in self.config["channels"]:
            try:
                signals.extend(self.poll_exchange(channel["id"]))
            except Exception as exc:
                logger.error("[%s] 폴링 중 오류: %s", channel["id"], exc, exc_info=True)
        return signals

    def _finalize_native_trades_post_trade_work(
        self,
        post: dict,
        channel: _ChannelRuntime,
        native_trades: list[dict],
        dedupe_tickers: bool = True,
    ):
        listing = self._prepare_native_listing(post=post, channel=channel)
        if listing is None:
            return
        listings = self._expand_listing_by_ticker(listing)
        if dedupe_tickers:
            listings = self._filter_new_listing_tickers(
                channel_id=channel.channel_id,
                message_id=int(post["message_id"]),
                listings=listings,
            )
        if not listings:
            return
        aligned_trades = self._align_native_trades_to_listings(
            listings=listings,
            native_trades=native_trades,
        )
        for item, trade in zip(listings, aligned_trades):
            if trade is None:
                continue
            self._finalize_post_trade_work(
                NOOP_LATENCY_TRACE,
                post,
                item,
                trade,
            )

    @staticmethod
    def _extract_native_trades(post: dict) -> list[dict] | None:
        native_trades = post.get("native_trades")
        if isinstance(native_trades, list):
            trades = [trade for trade in native_trades if isinstance(trade, dict)]
            if trades:
                return trades
        native_trade = post.get("native_trade")
        if isinstance(native_trade, dict):
            return [native_trade]
        return None

    def _submit_duplicate_native_trades(
        self,
        *,
        post: dict,
        channel: _ChannelRuntime,
        native_trades: list[dict] | None,
    ) -> bool:
        if not native_trades or not isinstance(post.get("native_listing"), dict):
            return False
        self._submit_background(
            self._finalize_native_trades_post_trade_work,
            post,
            channel,
            list(native_trades),
            False,
        )
        return True

    def run(self, on_signals: Callable[[list[dict]], None] | None = None):
        logger.info(
            "상장 공지 모니터 시작 — %d개 채널, %d초 간격",
            len(self.config["channels"]),
            self.poll_interval,
        )
        try:
            while True:
                logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 폴링 시작 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                signals = self.poll_all()
                if signals:
                    logger.info("이번 폴링 결과: %d건 신규 시그널", len(signals))
                    if on_signals is not None:
                        on_signals(signals)
                else:
                    logger.info("이번 폴링 결과: 신규 시그널 없음")
                logger.info("다음 폴링까지 %d초 대기...", self.poll_interval)
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("사용자 중단. 종료.")

    def close(self):
        self._warm_stop.set()
        if self._warm_thread is not None:
            self._warm_thread.join(timeout=2)
        self._state_flush_stop.set()
        if self._state_flush_thread is not None:
            self._state_flush_thread.join(timeout=2)
        if self.defer_persistence:
            self._flush_state_if_dirty()
        bybit_close = getattr(self.bybit_client, "close", None)
        if callable(bybit_close):
            bybit_close()
        buyer_close = getattr(self.spot_buyer, "close", None)
        if callable(buyer_close):
            buyer_close()
        if self._bg_executor is not None:
            self._bg_executor.shutdown(wait=True)
        return None

    @staticmethod
    def _has_multiple_tickers_fast(title: str) -> bool:
        return has_multiple_listing_assets_fast(title)

    def _title_ticker_already_seen(self, channel_id: str, title: str) -> str | None:
        """Per-ticker dedup pre-check that does NOT depend on classifier parity.

        Extracts the ticker straight from the title (same extractor used on
        every path) rather than relying on the minimal classifier agreeing with
        the authoritative C++ engine. For a single-ticker title whose ticker was
        already bought this session, returns that ticker so the caller can skip
        firing the C++ engine — closing the re-post double-buy when the minimal
        Python classifier and the C++ classifier disagree.
        """
        if self._has_multiple_tickers_fast(title):
            return None
        has_seen_listing = getattr(self.state_store, "has_seen_listing", None)
        if not callable(has_seen_listing):
            return None
        ticker = extract_primary_ticker(title)
        if not ticker:
            return None
        ticker = str(ticker).upper()
        return ticker if has_seen_listing(channel_id, ticker) else None

    @staticmethod
    def _attach_post_assets_to_listing(*, post: dict, listing: dict):
        assets = extract_listing_assets(post.get("title", ""))
        if not assets:
            return
        tickers = [asset["ticker"] for asset in assets]
        listing["assets"] = assets
        listing["tickers"] = tickers
        listing.setdefault("ticker", tickers[0])
        listing.setdefault("asset_name", assets[0]["asset_name"])

    @staticmethod
    def _copy_listing_payload(listing: dict) -> dict:
        copied: dict = {}
        for key, value in listing.items():
            if isinstance(value, list):
                copied[key] = [
                    dict(item) if isinstance(item, dict) else item
                    for item in value
                ]
            elif isinstance(value, dict):
                copied[key] = dict(value)
            else:
                copied[key] = value
        return copied

    def _prepare_native_listing(
        self,
        *,
        post: dict,
        channel: _ChannelRuntime,
    ) -> dict | None:
        native_listing = post.get("native_listing")
        if not isinstance(native_listing, dict):
            return None
        listing = self._copy_listing_payload(native_listing)
        listing["exchange"] = channel.exchange
        listing["display_name"] = channel.display_name
        self._attach_post_assets_to_listing(post=post, listing=listing)
        return listing

    @staticmethod
    def _asset_name_for_ticker(listing: dict, ticker: str) -> str | None:
        for asset in listing.get("assets") or []:
            if asset.get("ticker") == ticker:
                return asset.get("asset_name")
        if listing.get("ticker") == ticker:
            return listing.get("asset_name")
        return None

    def _expand_listing_by_ticker(self, listing: dict) -> list[dict]:
        tickers = string_list(listing.get("tickers"))
        if not tickers:
            tickers = string_list(listing.get("ticker"))
        if not tickers:
            return [listing]
        listing["tickers"] = tickers
        if "markets" in listing:
            listing["markets"] = string_list(listing.get("markets"))
        if len(tickers) == 1 and tickers[0] == listing.get("ticker"):
            return [listing]
        expanded: list[dict] = []
        total = len(tickers)
        for index, ticker in enumerate(tickers, start=1):
            item = dict(listing)
            item["ticker"] = ticker
            item["tickers"] = tickers
            item["multi_ticker_count"] = total
            item["multi_ticker_index"] = index
            asset_name = self._asset_name_for_ticker(listing, ticker)
            if asset_name:
                item["asset_name"] = asset_name
            expanded.append(item)
        return expanded

    def _filter_new_listing_tickers(
        self,
        *,
        channel_id: str,
        message_id: int,
        listings: list[dict],
    ) -> list[dict]:
        fresh: list[dict] = []
        for listing in listings:
            ticker = str(listing.get("ticker") or "").upper()
            if not ticker:
                continue
            if self._remember_listing_ticker(
                channel_id=channel_id,
                message_id=message_id,
                ticker=ticker,
            ):
                fresh.append(listing)
            else:
                logger.info(
                    "[%s] 중복 상장 티커 스킵: %s (message_id=%s)",
                    channel_id,
                    ticker,
                    message_id,
                )
        return fresh

    def _maybe_buy_spot(self, *, channel: _ChannelRuntime, post: dict, listing: dict) -> dict:
        order_link_id = self._make_order_link_id(
            prefix=channel.order_link_prefix,
            message_id=int(post["message_id"]),
            ticker=listing["ticker"],
        )
        if not self.enable_trading:
            return self._disabled_trade(
                listing=listing,
                order_link_id=order_link_id,
                reason="cli_disabled",
            )
        if self.spot_buyer is None:
            return self._disabled_trade(
                listing=listing,
                order_link_id=order_link_id,
                reason="python_spot_buyer_unavailable",
            )
        trade = self.spot_buyer.buy_market(
            ticker=listing["ticker"],
            order_link_id=order_link_id,
        )
        return self._with_trade_defaults(
            trade,
            listing=listing,
            order_link_id=order_link_id,
        )

    def _maybe_buy_spots(
        self,
        *,
        channel: _ChannelRuntime,
        post: dict,
        listings: list[dict],
    ) -> list[dict]:
        if not listings:
            return []
        if not self.enable_trading:
            return [
                self._disabled_trade_for_post(
                    channel=channel,
                    post=post,
                    listing=listing,
                    reason="cli_disabled",
                )
                for listing in listings
            ]
        if self.spot_buyer is None:
            return [
                self._disabled_trade_for_post(
                    channel=channel,
                    post=post,
                    listing=listing,
                    reason="python_spot_buyer_unavailable",
                )
                for listing in listings
            ]
        if len(listings) == 1:
            return [
                self._maybe_buy_spot(
                    channel=channel,
                    post=post,
                    listing=listings[0],
                )
            ]

        buy_markets = getattr(self.spot_buyer, "buy_markets", None)
        if not callable(buy_markets):
            return [
                self._maybe_buy_spot(channel=channel, post=post, listing=item)
                for item in listings
            ]
        orders = [
            {
                "ticker": item["ticker"],
                "order_link_id": self._make_order_link_id(
                    prefix=channel.order_link_prefix,
                    message_id=int(post["message_id"]),
                    ticker=item["ticker"],
                ),
            }
            for item in listings
        ]
        bought = list(buy_markets(orders) or [])
        aligned_bought = self._align_bulk_trades_to_listings(
            listings=listings,
            trades=bought,
        )
        trades: list[dict] = []
        for listing, order, trade in zip(listings, orders, aligned_bought):
            if trade is None:
                trades.append(
                    self._disabled_trade(
                        listing=listing,
                        order_link_id=order["order_link_id"],
                        reason="python_spot_buyer_missing_result",
                    )
                )
            else:
                trades.append(
                    self._with_trade_defaults(
                        trade,
                        listing=listing,
                        order_link_id=order["order_link_id"],
                    )
                )
        return trades

    @staticmethod
    def _disabled_trade(*, listing: dict, order_link_id: str, reason: str) -> dict:
        return {
            "enabled": False,
            "attempted": False,
            "executed": False,
            "ticker": listing["ticker"],
            "order_link_id": order_link_id,
            "reason": reason,
        }

    def _disabled_trade_for_post(
        self,
        *,
        channel: _ChannelRuntime,
        post: dict,
        listing: dict,
        reason: str,
    ) -> dict:
        return self._disabled_trade(
            listing=listing,
            order_link_id=self._make_order_link_id(
                prefix=channel.order_link_prefix,
                message_id=int(post["message_id"]),
                ticker=listing["ticker"],
            ),
            reason=reason,
        )

    @staticmethod
    def _with_trade_defaults(
        trade: dict,
        *,
        listing: dict,
        order_link_id: str,
    ) -> dict:
        enriched = dict(trade)
        enriched.setdefault("ticker", listing["ticker"])
        enriched.setdefault("order_link_id", order_link_id)
        return enriched

    @staticmethod
    def _align_bulk_trades_to_listings(
        *,
        listings: list[dict],
        trades: list[dict],
    ) -> list[dict | None]:
        return ExchangeListingPoller._align_trades_to_listings(
            listings=listings,
            trades=trades,
        )

    @staticmethod
    def _align_native_trades_to_listings(
        *,
        listings: list[dict],
        native_trades: list[dict],
    ) -> list[dict | None]:
        return ExchangeListingPoller._align_trades_to_listings(
            listings=listings,
            trades=native_trades,
        )

    @staticmethod
    def _align_trades_to_listings(
        *,
        listings: list[dict],
        trades: list[dict],
    ) -> list[dict | None]:
        trades_by_ticker: dict[str, list[dict]] = {}
        positional_trades: list[dict] = []
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            ticker = str(trade.get("ticker") or "").upper()
            if ticker:
                trades_by_ticker.setdefault(ticker, []).append(trade)
            else:
                positional_trades.append(trade)

        aligned: list[dict | None] = []
        for listing in listings:
            ticker = str(listing.get("ticker") or "").upper()
            if ticker and trades_by_ticker.get(ticker):
                aligned.append(trades_by_ticker[ticker].pop(0))
            elif positional_trades:
                aligned.append(positional_trades.pop(0))
            else:
                aligned.append(None)
        return aligned

    def _native_or_python_trades_for_listings(
        self,
        *,
        channel: _ChannelRuntime,
        post: dict,
        listings: list[dict],
        native_trades: list[dict],
    ) -> list[dict]:
        aligned = self._align_native_trades_to_listings(
            listings=listings,
            native_trades=native_trades,
        )
        missing = [
            (index, listing)
            for index, (listing, trade) in enumerate(zip(listings, aligned))
            if trade is None
        ]
        if missing:
            bought = self._maybe_buy_spots(
                channel=channel,
                post=post,
                listings=[listing for _, listing in missing],
            )
            for (index, _), trade in zip(missing, bought):
                aligned[index] = trade
        return [
            trade
            if trade is not None
            else self._disabled_trade_for_post(
                channel=channel,
                post=post,
                listing=listing,
                reason="native_trade_missing",
            )
            for listing, trade in zip(listings, aligned)
        ]

    def _lookup_bybit_snapshot(self, ticker: str) -> dict:
        bybit_client = self._ensure_bybit_client()
        if self.prefer_cached_lookup:
            return bybit_client.lookup_ticker_cached(ticker)
        return bybit_client.lookup_ticker(ticker)

    def _build_ultra_trade_ack(
        self,
        trace: LatencyTrace,
        post: dict,
        listing: dict,
        trade: dict,
    ) -> dict:
        signal = {
            "exchange": listing["exchange"],
            "exchange_name": listing["display_name"],
            "signal_type": listing["signal_type"],
            "ticker": listing["ticker"],
            "asset_name": listing["asset_name"],
            "markets": listing["markets"],
            "channel_handle": post["channel_handle"],
            "message_id": post["message_id"],
            "title": post.get("title", ""),
            "text": post.get("text", post.get("title", "")),
            "post_url": post.get(
                "post_url",
                f"https://t.me/{post['channel_handle']}/{post['message_id']}",
            ),
            "published_at": self._format_post_timestamp(post["published_at"]),
            "trade": trade,
            "ultra_deferred": True,
        }
        latency = self._build_latency_payload(trace, post, listing, trade)
        if latency is not None:
            signal["latency"] = latency
        return signal

    @staticmethod
    def _format_post_timestamp(value) -> str:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    def _finalize_post_trade_work(
        self,
        trace: LatencyTrace,
        post: dict,
        listing: dict,
        trade: dict,
    ):
        self._fill_trade_timing_fields(trade)
        listing = self._enrich_listing_if_needed(post=post, listing=listing)
        signal_emitter = self._ensure_signal_emitter()
        signal_emitter.persist_trade_proof(
            post=post,
            listing=listing,
            trade=trade,
        )
        self._log_trade_result(listing=listing, trade=trade)
        self._log_trade_latency(post=post, listing=listing, trade=trade)
        trace.mark("deferred_finalize")
        bybit = self._lookup_bybit_snapshot(listing["ticker"])
        trace.mark("bybit_snapshot")
        initial_latency = self._build_latency_payload(trace, post, listing, trade)
        signal = signal_emitter.build(
            post=post,
            listing=listing,
            bybit=bybit,
            trade=trade,
            latency=initial_latency,
        )
        trace.mark("build_signal")
        final_latency = self._build_latency_payload(trace, post, listing, trade)
        if final_latency is not None:
            signal["latency"] = final_latency
        signal_emitter.persist(signal)
        logger.info(
            "[%s] 상장 공지 감지: %s (%s)",
            listing["exchange"],
            listing["asset_name"],
            listing["ticker"],
        )

    def _enrich_listing_if_needed(self, *, post: dict, listing: dict) -> dict:
        if "asset_name" in listing and "markets" in listing:
            return listing
        channel = self._channel_runtime_by_id.get(listing["exchange"])
        if channel is None:
            channel_id = self.get_channel_id_by_handle(post.get("channel_handle", ""))
            if channel_id is not None:
                channel = self._channel_runtime_by_id.get(channel_id)
        if channel is None:
            return listing
        enriched = channel.classify_title(post.get("title", ""))
        if enriched is None:
            return listing
        ticker = listing["ticker"]
        asset_name = self._asset_name_for_ticker(enriched, ticker) or enriched.get("asset_name")
        enriched.update(
            {
                "exchange": listing["exchange"],
                "display_name": listing["display_name"],
                "signal_type": listing["signal_type"],
                "ticker": ticker,
            }
        )
        if asset_name:
            enriched["asset_name"] = asset_name
        return enriched

    def _build_latency_payload(
        self,
        trace: LatencyTrace,
        post: dict,
        listing: dict,
        trade: dict,
    ) -> dict | None:
        payload = trace.as_dict()
        if not payload:
            return None
        payload["message_id"] = int(post["message_id"])
        payload["ticker"] = listing["ticker"]
        payload["trade_attempted"] = bool(trade.get("attempted"))
        payload["trade_executed"] = bool(trade.get("executed"))
        if trade.get("transport"):
            payload["trade_transport"] = trade["transport"]
        if trade.get("trade_elapsed_ns") is not None:
            payload["trade_elapsed_ns"] = int(trade["trade_elapsed_ns"])
            payload["trade_elapsed_ms"] = float(trade.get("trade_elapsed_ms", 0.0))
        received_monotonic_ns = post.get("received_monotonic_ns")
        if received_monotonic_ns is not None:
            received_monotonic_ns = int(received_monotonic_ns)
            payload["received_monotonic_ns"] = received_monotonic_ns
            trace_received_monotonic_ns = post.get("received_python_monotonic_ns")
            if trace_received_monotonic_ns is not None:
                trace_received_monotonic_ns = int(trace_received_monotonic_ns)
                payload["received_python_monotonic_ns"] = trace_received_monotonic_ns
            else:
                trace_received_monotonic_ns = received_monotonic_ns
            start_ns = trace.start_ns()
            end_ns = trace.last_ns()
            if start_ns is not None:
                payload["receive_to_trace_start_ns"] = max(
                    0,
                    start_ns - trace_received_monotonic_ns,
                )
            if end_ns is not None:
                receive_to_signal_ns = max(0, end_ns - trace_received_monotonic_ns)
                payload["receive_to_signal_ns"] = receive_to_signal_ns
                payload["receive_to_signal_ms"] = receive_to_signal_ns / 1_000_000.0
            trade_finished_ns = trade.get("trade_finished_monotonic_ns")
            if trade_finished_ns is not None:
                receive_to_trade_ns = max(0, int(trade_finished_ns) - received_monotonic_ns)
                payload["receive_to_trade_finished_ns"] = receive_to_trade_ns
                payload["receive_to_trade_finished_ms"] = receive_to_trade_ns / 1_000_000.0
            order_send_started_ns = trade.get("order_send_started_monotonic_ns")
            if order_send_started_ns is not None:
                receive_to_send_ns = max(0, int(order_send_started_ns) - received_monotonic_ns)
                payload["receive_to_order_send_started_ns"] = receive_to_send_ns
                payload["receive_to_order_send_started_ms"] = receive_to_send_ns / 1_000_000.0
        return payload

    @staticmethod
    def _fill_trade_timing_fields(trade: dict):
        try:
            started_ns = int(trade.get("trade_started_monotonic_ns") or 0)
            send_started_ns = int(trade.get("order_send_started_monotonic_ns") or 0)
            finished_ns = int(trade.get("trade_finished_monotonic_ns") or 0)
        except (TypeError, ValueError):
            return
        if started_ns > 0 and send_started_ns > 0 and "order_prepare_elapsed_ns" not in trade:
            elapsed_ns = max(0, send_started_ns - started_ns)
            trade["order_prepare_elapsed_ns"] = int(elapsed_ns)
            trade["order_prepare_elapsed_us"] = elapsed_ns / 1_000.0
            trade["order_prepare_elapsed_ms"] = elapsed_ns / 1_000_000.0
        if started_ns > 0 and finished_ns > 0 and "trade_elapsed_ns" not in trade:
            elapsed_ns = max(0, finished_ns - started_ns)
            trade["trade_elapsed_ns"] = int(elapsed_ns)
            trade["trade_elapsed_us"] = elapsed_ns / 1_000.0
            trade["trade_elapsed_ms"] = elapsed_ns / 1_000_000.0

    @staticmethod
    def _log_trade_result(listing: dict, trade: dict):
        if trade.get("executed"):
            logger.info(
                "[%s] Bybit spot 매수 성공: %s order_id=%s",
                listing["exchange"],
                trade.get("symbol"),
                trade.get("order_id", ""),
            )
        elif trade.get("attempted"):
            logger.warning(
                "[%s] Bybit spot 매수 실패: %s (%s)",
                listing["exchange"],
                trade.get("symbol"),
                trade.get("reason", "unknown"),
            )

    @staticmethod
    def _log_trade_latency(post: dict, listing: dict, trade: dict):
        if not trade.get("attempted"):
            return
        received_monotonic_ns = post.get("received_monotonic_ns")
        order_send_started_ns = trade.get("order_send_started_monotonic_ns")
        if received_monotonic_ns is not None and order_send_started_ns is not None:
            receive_to_send_ns = max(0, int(order_send_started_ns) - int(received_monotonic_ns))
            logger.info(
                "[%s] 발견→주문전송시작 %.3fms transport=%s executed=%s symbol=%s",
                listing["exchange"],
                receive_to_send_ns / 1_000_000.0,
                trade.get("transport", "python_rest"),
                bool(trade.get("executed")),
                trade.get("symbol", ""),
            )
        trade_finished_ns = trade.get("trade_finished_monotonic_ns")
        if received_monotonic_ns is None or trade_finished_ns is None:
            return
        receive_to_trade_ns = max(0, int(trade_finished_ns) - int(received_monotonic_ns))
        logger.info(
            "[%s] 발견→주문완료 %.3fms transport=%s executed=%s symbol=%s",
            listing["exchange"],
            receive_to_trade_ns / 1_000_000.0,
            trade.get("transport", "python_rest"),
            bool(trade.get("executed")),
            trade.get("symbol", ""),
        )

    def process_source_post(self, channel_id: str, post: dict) -> dict | None:
        trace = (
            LatencyTrace(enabled=True)
            if self.latency_trace_enabled
            else NOOP_LATENCY_TRACE
        )
        channel = self._channel_runtime_by_id.get(channel_id)
        if channel is None:
            logger.error("거래소 설정 없음: %s", channel_id)
            return None

        message_id = int(post["message_id"])
        marked = self._mark_seen(channel_id, message_id)
        if not marked:
            return None
        trace.mark("dedup")

        if self.defer_persistence:
            self._mark_state_dirty()

        event: dict | None = None
        if self.persist_source_events:
            source_emitter = self._ensure_source_emitter()
            event = source_emitter.build(
                channel={
                    "id": channel.channel_id,
                    "exchange": channel.exchange,
                    "display_name": channel.display_name,
                    "channel_handle": channel.channel_handle,
                },
                post=post,
                latency=self._build_source_latency_payload(trace, post, channel),
            )
            trace.mark("build_source_event")
            final_latency = self._build_source_latency_payload(trace, post, channel)
            if final_latency is not None:
                event["latency"] = final_latency
            if self.defer_persistence:
                self._submit_background(source_emitter.persist, event)
            else:
                source_emitter.persist(event)
            return event

        return self._build_source_ack(trace, post, channel)

    def _build_source_latency_payload(
        self,
        trace: LatencyTrace,
        post: dict,
        channel: _ChannelRuntime,
    ) -> dict | None:
        payload = trace.as_dict()
        if not payload:
            return None
        payload["message_id"] = int(post["message_id"])
        payload["channel_id"] = channel.channel_id
        payload["source_only"] = True
        return payload

    def _build_source_ack(
        self,
        trace: LatencyTrace,
        post: dict,
        channel: _ChannelRuntime,
    ) -> dict:
        event = {
            "event_type": "telegram_source_post",
            "channel_id": channel.channel_id,
            "exchange": channel.exchange,
            "message_id": int(post["message_id"]),
        }
        received_monotonic_ns = post.get("received_monotonic_ns")
        if received_monotonic_ns is not None:
            event["received_monotonic_ns"] = int(received_monotonic_ns)
        latency = self._build_source_latency_payload(trace, post, channel)
        if latency is not None:
            event["latency"] = latency
        return event

    def _start_keep_warm_thread(self):
        if self._warm_thread is not None:
            return
        self._warm_thread = threading.Thread(
            target=self._keep_warm_loop,
            name="listing-sniper-warm",
            daemon=True,
        )
        self._warm_thread.start()

    def _keep_warm_loop(self):
        logger.info(
            "저지연 keep-warm 시작 (interval=%ss)",
            self.keep_warm_interval_sec,
        )
        while not self._warm_stop.wait(self.keep_warm_interval_sec):
            self._run_keep_warm_once()

    def _start_state_flush_thread(self):
        if self._state_flush_thread is not None:
            return
        self._state_flush_thread = threading.Thread(
            target=self._state_flush_loop,
            name="listing-sniper-state-flush",
            daemon=True,
        )
        self._state_flush_thread.start()

    def _state_flush_loop(self):
        while not self._state_flush_stop.wait(self.state_flush_interval_sec):
            self._flush_state_if_dirty()

    def _check_clock_skew(self):
        client = self.bybit_client
        server_time_fn = getattr(client, "server_time_ms", None)
        if not callable(server_time_fn):
            return
        try:
            server_ms = server_time_fn()
        except Exception:  # pragma: no cover - keep-warm safeguard
            return
        if server_ms is None:
            return
        skew_ms = abs(time.time() * 1000.0 - server_ms)
        if skew_ms > CLOCK_SKEW_WARN_MS:
            logger.warning(
                "로컬 시계가 Bybit 서버와 %.0fms 차이 — recv_window 초과 시 주문 인증 "
                "실패 위험. NTP 동기화를 확인하세요.",
                skew_ms,
            )

    def _run_keep_warm_once(self):
        self._check_clock_skew()
        if self.cpp_ultra_engine is not None and self.cpp_ultra_engine.is_enabled():
            try:
                self.cpp_ultra_engine.warmup()
            except Exception as exc:  # pragma: no cover - keep-warm safeguard
                logger.warning("C++ ultra engine keep-warm 실패: %s", exc)

        warmup = getattr(self.spot_buyer, "warmup", None)
        if callable(warmup):
            try:
                warmup(force_refresh_market_cache=True)
                return
            except Exception as exc:  # pragma: no cover - keep-warm safeguard
                logger.warning("저지연 warmup 실패: %s", exc)

        refresh = getattr(self._ensure_bybit_client(), "refresh_market_cache", None)
        if callable(refresh):
            try:
                refresh(force=True)
            except Exception as exc:  # pragma: no cover - keep-warm safeguard
                logger.warning("Bybit 심볼 캐시 keep-warm 실패: %s", exc)

    def _submit_background(self, fn, *args, **kwargs):
        if self._bg_executor is None:
            fn(*args, **kwargs)
            return
        self._bg_executor.submit(self._run_background_task, fn, *args, **kwargs)

    def reset_state(self):
        self.state_store.clear()
        if self.hot_state_enabled:
            self._hot_start_last_seen = dict.fromkeys(self._channels_by_id, 0)
            self._hot_last_seen = dict.fromkeys(self._channels_by_id, 0)
            self._hot_seen_message_ids = {
                channel_id: OrderedDict()
                for channel_id in self._channels_by_id
            }
        self._state_dirty_epoch = 0
        self._state_flushed_epoch = 0

    def _mark_seen(self, channel_id: str, message_id: int) -> bool:
        if not self.hot_state_enabled:
            return self.state_store.mark_seen(
                channel_id,
                message_id,
                persist=not self.defer_persistence,
            )

        if not self._would_mark_seen(channel_id, message_id):
            return False
        self._remember_hot_seen(channel_id, message_id)
        return True

    def _would_mark_seen(self, channel_id: str, message_id: int) -> bool:
        if not self.hot_state_enabled:
            can_mark_seen = getattr(self.state_store, "can_mark_seen", None)
            if callable(can_mark_seen):
                return can_mark_seen(channel_id, message_id)
            # Defensive fallback for a duck-typed store without can_mark_seen:
            # dedup on the seen-id set, NOT on last_seen. A bare last_seen floor
            # would drop a lower-id listing that arrives after a higher-id
            # non-listing — the exact out-of-order miss the seen-set prevents.
            message_id = int(message_id)
            seen_snapshot_fn = getattr(self.state_store, "snapshot_seen_message_ids", None)
            if callable(seen_snapshot_fn):
                seen = seen_snapshot_fn().get(channel_id, [])
                return message_id not in {int(value) for value in seen}
            state = self.state_store.snapshot_last_seen()
            return message_id > int(state.get(channel_id, 0))
        message_id = int(message_id)
        if message_id <= int(self._hot_start_last_seen.get(channel_id, 0)):
            return False
        seen_ids = self._hot_seen_message_ids.get(channel_id)
        return seen_ids is None or message_id not in seen_ids

    def _remember_seen(self, channel_id: str, message_id: int):
        if not self.hot_state_enabled:
            self.state_store.mark_seen(
                channel_id,
                message_id,
                persist=not self.defer_persistence,
            )
            return
        self._remember_hot_seen(channel_id, message_id)

    def _remember_hot_seen(self, channel_id: str, message_id: int):
        # SINGLE-WRITER INVARIANT: the read-modify-write below (max() then store,
        # OrderedDict insert/move/popitem) and _mark_state_dirty's epoch bump are
        # lock-free and correct ONLY because the hot path runs on exactly one
        # thread — process_post is driven synchronously from the single asyncio
        # event loop (race_realtime_client._first_wins calls on_post inline). Do
        # NOT wrap on_post in run_in_executor/to_thread or add a second loop
        # without adding a lock here, or dedup updates can be lost -> double-buys.
        message_id = int(message_id)
        self._hot_last_seen[channel_id] = max(
            int(self._hot_last_seen.get(channel_id, 0)),
            message_id,
        )
        seen_ids = self._hot_seen_message_ids.setdefault(channel_id, OrderedDict())
        if message_id in seen_ids:
            seen_ids.move_to_end(message_id)
            return
        seen_ids[message_id] = None
        if len(seen_ids) > HOT_SEEN_MAX_ENTRIES_PER_CHANNEL:
            seen_ids.popitem(last=False)

    def _remember_listing_ticker(
        self,
        *,
        channel_id: str,
        message_id: int,
        ticker: str,
    ) -> bool:
        mark_listing_seen = getattr(self.state_store, "mark_listing_seen", None)
        if not callable(mark_listing_seen):
            return True
        remembered = mark_listing_seen(
            channel_id,
            ticker,
            message_id,
            persist=not self.defer_persistence,
        )
        if remembered and self.defer_persistence:
            self._mark_state_dirty()
        return remembered

    def _mark_state_dirty(self):
        self._state_dirty_epoch += 1

    def _flush_state_if_dirty(self):
        target_epoch = self._state_dirty_epoch
        if target_epoch == self._state_flushed_epoch:
            return
        if self.hot_state_enabled:
            seen_snapshot = {
                channel_id: list(seen_ids.keys())
                for channel_id, seen_ids in self._hot_seen_message_ids.items()
            }
            replace_message_state = getattr(
                self.state_store,
                "replace_message_state_snapshot",
                None,
            )
            if callable(replace_message_state):
                replace_message_state(self._hot_last_seen, seen_snapshot, persist=True)
            else:
                replace_hot = getattr(self.state_store, "replace_hot_state_snapshot", None)
                if callable(replace_hot):
                    replace_hot(self._hot_last_seen, seen_snapshot, persist=True)
                else:
                    self.state_store.replace_last_seen_snapshot(self._hot_last_seen, persist=True)
        else:
            self.state_store.flush()
        self._state_flushed_epoch = target_epoch

    @staticmethod
    def _ordered_recent_seen_ids(message_ids: list[int]) -> OrderedDict[int, None]:
        ordered: OrderedDict[int, None] = OrderedDict()
        for value in message_ids[-HOT_SEEN_MAX_ENTRIES_PER_CHANNEL:]:
            try:
                message_id = int(value)
            except (TypeError, ValueError):
                continue
            if message_id <= 0:
                continue
            if message_id in ordered:
                ordered.move_to_end(message_id)
            else:
                ordered[message_id] = None
        return ordered

    @staticmethod
    def _run_background_task(fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - background safeguard
            logger.error("백그라운드 작업 실패: %s", exc, exc_info=True)

    # Bybit V5 caps orderLinkId at 36 chars; the C++ ultra engine truncates to
    # the same bound. Keep these identical so a repeated
    # (exchange, message_id, ticker) yields the same orderLinkId on both paths.
    ORDER_LINK_ID_MAX_LEN = 36

    @staticmethod
    def _exchange_code(exchange: str) -> str:
        # Must match the C++ ultra engine. Bybit deduplicates repeated buys by
        # orderLinkId, so Python and C++ must build the exact same value for a
        # repeated (exchange, message_id, ticker).
        return exchange

    def _make_order_link_id(self, *, prefix: str, message_id: int, ticker: str) -> str:
        return f"{prefix}{message_id}-{ticker}"[: self.ORDER_LINK_ID_MAX_LEN]
