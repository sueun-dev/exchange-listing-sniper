#!/usr/bin/env python3
from __future__ import annotations

"""Bounded live Telegram ingest smoke benchmark."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from src.race_realtime_client import RaceRealtimeChannelClient  # noqa: E402
from src.tdlib_realtime_client import (  # noqa: E402
    TdlibRealtimeChannelClient,
    _TdlibRelay,
    _handle_key,
    _load_watch_chat_cache,
    _parse_watch_chat_ids,
    _save_watch_chat_cache,
)
from src.telegram_realtime_client import RealtimeTelegramChannelClient  # noqa: E402


def _make_client(backend: str):
    if backend == "telethon":
        return RealtimeTelegramChannelClient()
    if backend == "tdlib":
        return TdlibRealtimeChannelClient()
    if backend == "race":
        return RaceRealtimeChannelClient()
    raise ValueError(f"unknown backend: {backend}")


def _truthy_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_utc_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _delta_ms(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds() * 1_000.0, 3)


def _receive_to_trade_field_us(relay_received_monotonic_ns, trades: list[dict], field: str):
    if relay_received_monotonic_ns is None:
        return None
    values = []
    for trade in trades:
        try:
            value = int(trade.get(field, 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    if not values:
        return None
    return round((max(values) - int(relay_received_monotonic_ns)) / 1_000.0, 3)


def _summarize_event_field_us(events: list[dict], field: str) -> dict | None:
    values = []
    for event in events:
        if not isinstance(event, dict):
            continue
        value = event.get(field)
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return None

    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return {
        "count": len(ordered),
        "min_us": ordered[0],
        "p50_us": ordered[len(ordered) // 2],
        "p95_us": ordered[p95_index],
        "max_us": ordered[-1],
        "avg_us": round(sum(ordered) / len(ordered), 3),
    }


def _event_latency_summaries(events: list[dict]) -> dict:
    return {
        "receive_to_last_order_send_started_us_summary": _summarize_event_field_us(
            events,
            "receive_to_last_order_send_started_us",
        ),
        "receive_to_last_trade_finished_us_summary": _summarize_event_field_us(
            events,
            "receive_to_last_trade_finished_us",
        ),
        "inject_to_listing_matched_us_summary": _summarize_event_field_us(
            events,
            "inject_to_listing_matched_us",
        ),
    }


def _synthetic_tdlib_update(*, chat_id: int, message_id: int, date: int, title: str) -> str:
    return json.dumps(
        {
            "@type": "updateNewMessage",
            "message": {
                "@type": "message",
                "id": message_id,
                "chat_id": chat_id,
                "date": date,
                "content": {
                    "@type": "messageText",
                    "text": {
                        "@type": "formattedText",
                        "text": title,
                        "entities": [],
                    },
                },
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _expanded_test_cases(test_cases: list[dict], iterations: int) -> list[dict]:
    if iterations <= 0 or not test_cases:
        return []
    expanded = []
    count = len(test_cases)
    for index in range(iterations):
        base = test_cases[index % count]
        cycle = index // count
        item = dict(base)
        item["message_id"] = int(base["message_id"]) + cycle * 1000
        expanded.append(item)
    return expanded


async def _resolve_tdlib_chat_ids(
    client: TdlibRealtimeChannelClient,
    relay: _TdlibRelay,
    handles: list[str],
) -> dict[str, int]:
    cached_chat_ids = _load_watch_chat_cache()
    configured_chat_ids = dict(cached_chat_ids)
    configured_chat_ids.update(_parse_watch_chat_ids(os.environ.get("LISTING_TDLIB_WATCH_CHATS")))
    resolved: dict[str, int] = {}
    newly_resolved: dict[str, int] = {}
    for handle in handles:
        username = handle.lstrip("@")
        key = _handle_key(username)
        chat_id = configured_chat_ids.get(key)
        if chat_id is None:
            response = await asyncio.to_thread(
                relay.send_request,
                {"@type": "searchPublicChat", "username": username},
                20,
            )
            if response.get("@type") != "chat":
                raise RuntimeError(f"TDLib failed to resolve chat {username}: {response}")
            chat_id = int(response["id"])
            newly_resolved[key] = chat_id
        resolved[username] = int(chat_id)

    if newly_resolved:
        cache_to_save = dict(cached_chat_ids)
        cache_to_save.update(newly_resolved)
        await asyncio.to_thread(_save_watch_chat_cache, cache_to_save)
    return resolved


async def _run_native_preflight_inject(args) -> dict:
    client = TdlibRealtimeChannelClient()
    if not client.is_configured():
        return {
            "ok": False,
            "backend": "tdlib",
            "reason": "telegram_api_not_configured",
        }
    if not client.has_session_file():
        return {
            "ok": False,
            "backend": "tdlib",
            "reason": "telegram_session_missing",
        }

    os.environ["BYBIT_SPOT_BUY_ENABLED"] = "1"
    os.environ["BYBIT_API_KEY"] = os.environ.get("BYBIT_API_KEY", "inject-smoke-key")
    os.environ["BYBIT_API_SECRET"] = os.environ.get("BYBIT_API_SECRET", "inject-smoke-secret")
    os.environ["BYBIT_SPOT_BUY_USDT_AMOUNT"] = os.environ.get("BYBIT_SPOT_BUY_USDT_AMOUNT", "5")
    os.environ["BYBIT_API_BASE_URL"] = os.environ.get(
        "LISTING_LIVE_SMOKE_BYBIT_BASE_URL",
        "https://127.0.0.1:1",
    )
    os.environ["LISTING_TDLIB_SKIP_CLOCK_CALIBRATION"] = "1"
    os.environ["LISTING_TDLIB_NATIVE_IMMEDIATE_KEEPWARM_REFRESH"] = "0"

    relay = _TdlibRelay(client.relay_path)
    await asyncio.to_thread(relay.start)
    try:
        await asyncio.to_thread(client._ensure_ready, relay, False)
        chat_ids = await _resolve_tdlib_chat_ids(
            client,
            relay,
            ["upbit_news", "BithumbExchange"],
        )
        watch_spec = ",".join(
            f"{chat_ids[handle]}:{handle}"
            for handle in ("upbit_news", "BithumbExchange")
        )
        await asyncio.to_thread(relay.send_raw, f"__watch_chats__\t{watch_spec}")
        for symbol in ("STRKUSDT", "VVVUSDT", "SENTUSDT", "ELSAUSDT"):
            await asyncio.to_thread(relay.send_raw, f"__selftest_native_preflight_on__\t{symbol}")

        test_cases = [
            {
                "handle": "BithumbExchange",
                "message_id": 921987,
                "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
                "expected_symbols": ["STRKUSDT"],
            },
            {
                "handle": "upbit_news",
                "message_id": 921988,
                "title": "[거래] 베니스토큰(VVV) 신규 거래지원 안내 (KRW 마켓)",
                "expected_symbols": ["VVVUSDT"],
            },
            {
                "handle": "BithumbExchange",
                "message_id": 921989,
                "title": "[마켓 추가] 센티언트(SENT), 헤이엘사(ELSA) 원화 마켓 추가",
                "expected_symbols": ["SENTUSDT", "ELSAUSDT"],
            },
        ]
        events = []
        selected_cases = _expanded_test_cases(test_cases, args.iterations)
        for item in selected_cases:
            payload = _synthetic_tdlib_update(
                chat_id=chat_ids[item["handle"]],
                message_id=item["message_id"],
                date=1778680000,
                title=item["title"],
            )
            started_ns = time.monotonic_ns()
            await asyncio.to_thread(relay.send_raw, f"__selftest_update__\t{payload}")
            event = await asyncio.to_thread(
                relay.wait_for,
                lambda event: event.payload.get("@type") == "listingMatched"
                and int(event.payload.get("message_id", 0)) == item["message_id"],
                args.timeout,
            )
            elapsed_us = round((time.monotonic_ns() - started_ns) / 1_000.0, 3)
            trades = event.payload.get("native_trades") or []
            if not isinstance(trades, list) or not trades:
                trade = event.payload.get("native_trade") or {}
                trades = [trade] if trade else []
            expected_symbols = item["expected_symbols"]
            ok_trade = len(trades) >= len(expected_symbols) and all(
                isinstance(trade, dict)
                and trade.get("attempted") is True
                and trade.get("executed") is False
                and trade.get("ret_code") == 0
                and trade.get("reason") == "tdlib_native_rest_preflight"
                and trade.get("symbol") == expected_symbol
                for trade, expected_symbol in zip(trades, expected_symbols)
            )
            if not ok_trade:
                return {
                    "ok": False,
                    "backend": "tdlib",
                    "mode": "tdlib_native_preflight_inject_official_chat_ids",
                    "reason": "native_preflight_trade_mismatch",
                    "event": event.payload,
                }
            relay_received_monotonic_ns = event.payload.get("relay_received_monotonic_ns")
            events.append(
                {
                    "channel_handle": event.payload.get("channel_handle"),
                    "message_id": event.payload.get("message_id"),
                    "title": event.payload.get("title"),
                    "ticker": event.payload.get("ticker"),
                    "tickers": event.payload.get("tickers"),
                    "markets": event.payload.get("markets") or ["KRW"],
                    "native_trade": event.payload.get("native_trade"),
                    "native_trades": trades,
                    "relay_received_monotonic_ns": relay_received_monotonic_ns,
                    "receive_to_last_order_send_started_us": _receive_to_trade_field_us(
                        relay_received_monotonic_ns,
                        trades,
                        "order_send_started_monotonic_ns",
                    ),
                    "receive_to_last_trade_finished_us": _receive_to_trade_field_us(
                        relay_received_monotonic_ns,
                        trades,
                        "trade_finished_monotonic_ns",
                    ),
                    "inject_to_listing_matched_us": elapsed_us,
                }
            )

        return {
            "ok": True,
            "backend": "tdlib",
            "mode": "tdlib_native_preflight_inject_official_chat_ids",
            "channels": args.channel,
            "chat_ids": chat_ids,
            "events_seen": len(events),
            **_event_latency_summaries(events),
            "events": events,
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "tdlib",
            "mode": "tdlib_native_preflight_inject_official_chat_ids",
            "reason": "runtime_error",
            "error": str(exc),
        }
    finally:
        await asyncio.to_thread(relay.close)


def _start_mock_bybit_server(symbols: list[str]):
    orders: list[dict] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _write_json(self, status: int, payload: dict):
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.path.startswith("/v5/market/instruments-info"):
                self._write_json(404, {"retCode": 10001, "retMsg": "not found"})
                return
            self._write_json(
                200,
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "category": "spot",
                        "list": [{"symbol": symbol} for symbol in symbols],
                        "nextPageCursor": "",
                    },
                },
            )

        def do_POST(self):
            if self.path != "/v5/order/create":
                self._write_json(404, {"retCode": 10001, "retMsg": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length).decode()
            try:
                request = json.loads(raw_body)
            except json.JSONDecodeError:
                request = {"_raw": raw_body}
            with lock:
                orders.append(request)
                order_index = len(orders)
            self._write_json(
                200,
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": f"mock-order-{order_index}",
                        "orderLinkId": request.get("orderLinkId", ""),
                    },
                },
            )

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, orders


def _write_spot_symbol_cache(path: Path, symbols: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# saved_unix_sec=" + str(int(time.time())) + "\n" + "\n".join(symbols) + "\n",
        encoding="utf-8",
    )


def _write_file_order_fixture(root: Path, order_id: str = "file-order-1"):
    order_dir = root / "v5" / "order"
    order_dir.mkdir(parents=True, exist_ok=True)
    (order_dir / "create").write_text(
        json.dumps(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": order_id,
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


async def _run_native_file_order_inject(args) -> dict:
    client = TdlibRealtimeChannelClient()
    if not client.is_configured():
        return {
            "ok": False,
            "backend": "tdlib",
            "reason": "telegram_api_not_configured",
        }
    if not client.has_session_file():
        return {
            "ok": False,
            "backend": "tdlib",
            "reason": "telegram_session_missing",
        }

    symbols = ["STRKUSDT", "VVVUSDT", "SENTUSDT", "ELSAUSDT"]
    temp_dir = tempfile.TemporaryDirectory()
    root = Path(temp_dir.name)
    relay = _TdlibRelay(client.relay_path)
    try:
        _write_file_order_fixture(root)
        symbol_cache_path = root / "spot_symbols.txt"
        _write_spot_symbol_cache(symbol_cache_path, symbols)

        os.environ["BYBIT_SPOT_BUY_ENABLED"] = "1"
        os.environ["BYBIT_API_KEY"] = os.environ.get("BYBIT_API_KEY", "file-order-key")
        os.environ["BYBIT_API_SECRET"] = os.environ.get("BYBIT_API_SECRET", "file-order-secret")
        os.environ["BYBIT_SPOT_BUY_USDT_AMOUNT"] = os.environ.get("BYBIT_SPOT_BUY_USDT_AMOUNT", "5")
        os.environ["BYBIT_API_BASE_URL"] = "file://" + root.as_posix()
        os.environ["LISTING_TDLIB_SKIP_CLOCK_CALIBRATION"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_BUY_ENABLED"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_BUY_ACTIVE"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS"] = "0"
        os.environ["LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH"] = str(symbol_cache_path)
        os.environ["LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MAX_AGE_SEC"] = "300"
        os.environ["LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MIN_COUNT"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_TIMING_ENABLED"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_IMMEDIATE_KEEPWARM_REFRESH"] = "0"
        os.environ["LISTING_TDLIB_NATIVE_BLOCKING_HOT_ORDER_WARMUP"] = "0"
        os.environ["LISTING_TDLIB_NATIVE_KEEPWARM_INTERVAL"] = "3600"
        os.environ["LISTING_TDLIB_NATIVE_SYMBOL_REFRESH_INTERVAL"] = "3600"

        await asyncio.to_thread(relay.start)
        await asyncio.to_thread(client._ensure_ready, relay, False)
        chat_ids = await _resolve_tdlib_chat_ids(
            client,
            relay,
            ["upbit_news", "BithumbExchange"],
        )
        watch_spec = ",".join(
            f"{chat_ids[handle]}:{handle}"
            for handle in ("upbit_news", "BithumbExchange")
        )
        await asyncio.to_thread(relay.send_raw, f"__native_start__\t{watch_spec}")
        native_status = await asyncio.to_thread(relay.wait_for_native_status, args.timeout)
        if not native_status.get("ready"):
            return {
                "ok": False,
                "backend": "tdlib",
                "mode": "tdlib_native_file_order_inject",
                "reason": "native_buy_not_ready",
                "native_status": native_status,
            }

        test_cases = [
            {
                "handle": "BithumbExchange",
                "message_id": 941987,
                "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
                "expected_symbols": ["STRKUSDT"],
            },
            {
                "handle": "upbit_news",
                "message_id": 941988,
                "title": "[거래] 베니스토큰(VVV) 신규 거래지원 안내 (KRW 마켓)",
                "expected_symbols": ["VVVUSDT"],
            },
            {
                "handle": "BithumbExchange",
                "message_id": 941989,
                "title": "[마켓 추가] 센티언트(SENT), 헤이엘사(ELSA) 원화 마켓 추가",
                "expected_symbols": ["SENTUSDT", "ELSAUSDT"],
            },
        ]
        events = []
        selected_cases = _expanded_test_cases(test_cases, args.iterations)
        emit_listing_events = _truthy_env("LISTING_TDLIB_EMIT_LISTING_EVENTS", default=True)
        for item in selected_cases:
            payload = _synthetic_tdlib_update(
                chat_id=chat_ids[item["handle"]],
                message_id=item["message_id"],
                date=1778680000,
                title=item["title"],
            )
            started_ns = time.monotonic_ns()
            await asyncio.to_thread(relay.send_raw, f"__selftest_update__\t{payload}")
            expected_symbols = item["expected_symbols"]
            if not emit_listing_events:
                event = await asyncio.to_thread(
                    relay.wait_for,
                    lambda event: event.payload.get("@type") == "selftestUpdateStatus"
                    and int(event.payload.get("message_id", 0)) == item["message_id"],
                    args.timeout,
                )
                elapsed_us = round((time.monotonic_ns() - started_ns) / 1_000.0, 3)
                dispatches = event.payload.get("native_dispatches") or []
                expected_tickers = [
                    symbol[:-4] if symbol.endswith("USDT") else symbol
                    for symbol in expected_symbols
                ]
                observed_tickers = event.payload.get("tickers") or []
                attempted_count = int(event.payload.get("native_dispatch_attempt_count") or 0)
                fallback_count = int(event.payload.get("native_dispatch_fallback_count") or 0)
                expected_dispatches = dispatches[: len(expected_symbols)]
                ok_dispatch = (
                    event.payload.get("consumed") is True
                    and observed_tickers == expected_tickers
                    and isinstance(dispatches, list)
                    and len(dispatches) >= len(expected_symbols)
                    and attempted_count >= len(expected_symbols)
                    and all(
                        isinstance(dispatch, dict)
                        and (dispatch.get("dispatched") is True or fallback_count > 0)
                        for dispatch in expected_dispatches
                    )
                )
                if not ok_dispatch:
                    return {
                        "ok": False,
                        "backend": "tdlib",
                        "mode": "tdlib_native_file_order_inject",
                        "reason": "native_file_dispatch_mismatch",
                        "event": event.payload,
                    }
                events.append(
                    {
                        "channel_handle": event.payload.get("channel_handle"),
                        "message_id": event.payload.get("message_id"),
                        "title": event.payload.get("title"),
                        "ticker": event.payload.get("ticker"),
                        "tickers": observed_tickers,
                        "markets": event.payload.get("markets") or ["KRW"],
                        "native_dispatches": dispatches,
                        "native_dispatch_attempt_count": attempted_count,
                        "native_dispatch_fallback_count": fallback_count,
                        "relay_received_monotonic_ns": event.payload.get(
                            "relay_received_monotonic_ns"
                        ),
                        "inject_to_selftest_status_us": elapsed_us,
                    }
                )
                continue

            event = await asyncio.to_thread(
                relay.wait_for,
                lambda event: event.payload.get("@type") == "listingMatched"
                and int(event.payload.get("message_id", 0)) == item["message_id"],
                args.timeout,
            )
            elapsed_us = round((time.monotonic_ns() - started_ns) / 1_000.0, 3)
            trades = event.payload.get("native_trades") or []
            if not isinstance(trades, list) or not trades:
                trade = event.payload.get("native_trade") or {}
                trades = [trade] if trade else []
            async_dispatch = _truthy_env("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH")

            def _trade_matches(trade: dict, expected_symbol: str) -> bool:
                if not isinstance(trade, dict) or trade.get("symbol") != expected_symbol:
                    return False
                if trade.get("attempted") is not True:
                    return False
                if async_dispatch:
                    return (
                        trade.get("executed") is False
                        and trade.get("ret_code") == -1
                        and trade.get("reason") == "tdlib_native_rest_dispatched"
                        and int(trade.get("order_send_started_monotonic_ns") or 0) > 0
                    )
                return (
                    trade.get("executed") is True
                    and trade.get("ret_code") == 0
                    and trade.get("reason") == "tdlib_native_rest"
                    and trade.get("order_id") == "file-order-1"
                )

            ok_trade = len(trades) >= len(expected_symbols) and all(
                _trade_matches(trade, expected_symbol)
                for trade, expected_symbol in zip(trades, expected_symbols)
            )
            if not ok_trade:
                return {
                    "ok": False,
                    "backend": "tdlib",
                    "mode": "tdlib_native_file_order_inject",
                    "reason": "native_file_order_mismatch",
                    "event": event.payload,
                }
            relay_received_monotonic_ns = event.payload.get("relay_received_monotonic_ns")
            events.append(
                {
                    "channel_handle": event.payload.get("channel_handle"),
                    "message_id": event.payload.get("message_id"),
                    "title": event.payload.get("title"),
                    "ticker": event.payload.get("ticker"),
                    "tickers": event.payload.get("tickers"),
                    "markets": event.payload.get("markets") or ["KRW"],
                    "native_trade": event.payload.get("native_trade"),
                    "native_trades": trades,
                    "relay_received_monotonic_ns": relay_received_monotonic_ns,
                    "receive_to_last_order_send_started_us": _receive_to_trade_field_us(
                        relay_received_monotonic_ns,
                        trades,
                        "order_send_started_monotonic_ns",
                    ),
                    "receive_to_last_trade_finished_us": _receive_to_trade_field_us(
                        relay_received_monotonic_ns,
                        trades,
                        "trade_finished_monotonic_ns",
                    ),
                    "inject_to_listing_matched_us": elapsed_us,
                }
            )

        expected_order_count = sum(len(item["expected_symbols"]) for item in selected_cases)
        if emit_listing_events:
            orders_seen = sum(len(event["native_trades"]) for event in events)
        else:
            orders_seen = sum(
                int(event.get("native_dispatch_attempt_count") or 0)
                for event in events
            )
        return {
            "ok": orders_seen == expected_order_count,
            "backend": "tdlib",
            "mode": "tdlib_native_file_order_inject",
            "mode_detail": (
                "listing_matched_event"
                if emit_listing_events
                else "emit_off_fire_and_forget_dispatch"
            ),
            "channels": args.channel,
            "chat_ids": chat_ids,
            "native_status": native_status,
            "events_seen": len(events),
            "orders_seen": orders_seen,
            "expected_order_count": expected_order_count,
            **_event_latency_summaries(events),
            "events": events,
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "tdlib",
            "mode": "tdlib_native_file_order_inject",
            "reason": "runtime_error",
            "error": str(exc),
        }
    finally:
        await asyncio.to_thread(relay.close)
        temp_dir.cleanup()


async def _run_native_local_order_inject(args) -> dict:
    client = TdlibRealtimeChannelClient()
    if not client.is_configured():
        return {
            "ok": False,
            "backend": "tdlib",
            "reason": "telegram_api_not_configured",
        }
    if not client.has_session_file():
        return {
            "ok": False,
            "backend": "tdlib",
            "reason": "telegram_session_missing",
        }

    symbols = ["STRKUSDT", "VVVUSDT", "SENTUSDT", "ELSAUSDT"]
    try:
        server, thread, mock_orders = _start_mock_bybit_server(symbols)
    except OSError as exc:
        return {
            "ok": False,
            "backend": "tdlib",
            "mode": "tdlib_native_local_order_inject",
            "reason": "mock_bybit_server_start_failed",
            "error": str(exc),
        }
    mock_base_url = f"http://127.0.0.1:{server.server_port}"
    temp_dir = tempfile.TemporaryDirectory()
    relay = _TdlibRelay(client.relay_path)
    try:
        os.environ["BYBIT_SPOT_BUY_ENABLED"] = "1"
        os.environ["BYBIT_API_KEY"] = os.environ.get("BYBIT_API_KEY", "local-order-key")
        os.environ["BYBIT_API_SECRET"] = os.environ.get("BYBIT_API_SECRET", "local-order-secret")
        os.environ["BYBIT_SPOT_BUY_USDT_AMOUNT"] = os.environ.get("BYBIT_SPOT_BUY_USDT_AMOUNT", "5")
        os.environ["BYBIT_API_BASE_URL"] = mock_base_url
        os.environ["LISTING_TDLIB_SKIP_CLOCK_CALIBRATION"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_BUY_ENABLED"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_BUY_ACTIVE"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS"] = "0"
        os.environ["LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH"] = str(
            Path(temp_dir.name) / "spot_symbols.txt"
        )
        os.environ["LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MAX_AGE_SEC"] = "300"
        os.environ["LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MIN_COUNT"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_TIMING_ENABLED"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_IMMEDIATE_KEEPWARM_REFRESH"] = "0"
        os.environ["LISTING_TDLIB_NATIVE_BLOCKING_HOT_ORDER_WARMUP"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_KEEPWARM_INTERVAL"] = "3600"
        os.environ["LISTING_TDLIB_NATIVE_SYMBOL_REFRESH_INTERVAL"] = "3600"

        await asyncio.to_thread(relay.start)
        await asyncio.to_thread(client._ensure_ready, relay, False)
        chat_ids = await _resolve_tdlib_chat_ids(
            client,
            relay,
            ["upbit_news", "BithumbExchange"],
        )
        watch_spec = ",".join(
            f"{chat_ids[handle]}:{handle}"
            for handle in ("upbit_news", "BithumbExchange")
        )
        await asyncio.to_thread(relay.send_raw, f"__native_start__\t{watch_spec}")
        native_status = await asyncio.to_thread(relay.wait_for_native_status, args.timeout)
        if not native_status.get("ready"):
            return {
                "ok": False,
                "backend": "tdlib",
                "mode": "tdlib_native_local_order_inject",
                "reason": "native_buy_not_ready",
                "native_status": native_status,
            }

        test_cases = [
            {
                "handle": "BithumbExchange",
                "message_id": 931987,
                "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",
                "expected_symbols": ["STRKUSDT"],
            },
            {
                "handle": "upbit_news",
                "message_id": 931988,
                "title": "[거래] 베니스토큰(VVV) 신규 거래지원 안내 (KRW 마켓)",
                "expected_symbols": ["VVVUSDT"],
            },
            {
                "handle": "BithumbExchange",
                "message_id": 931989,
                "title": "[마켓 추가] 센티언트(SENT), 헤이엘사(ELSA) 원화 마켓 추가",
                "expected_symbols": ["SENTUSDT", "ELSAUSDT"],
            },
        ]
        events = []
        selected_cases = _expanded_test_cases(test_cases, args.iterations)
        for item in selected_cases:
            payload = _synthetic_tdlib_update(
                chat_id=chat_ids[item["handle"]],
                message_id=item["message_id"],
                date=1778680000,
                title=item["title"],
            )
            started_ns = time.monotonic_ns()
            await asyncio.to_thread(relay.send_raw, f"__selftest_update__\t{payload}")
            event = await asyncio.to_thread(
                relay.wait_for,
                lambda event: event.payload.get("@type") == "listingMatched"
                and int(event.payload.get("message_id", 0)) == item["message_id"],
                args.timeout,
            )
            elapsed_us = round((time.monotonic_ns() - started_ns) / 1_000.0, 3)
            trades = event.payload.get("native_trades") or []
            if not isinstance(trades, list) or not trades:
                trade = event.payload.get("native_trade") or {}
                trades = [trade] if trade else []
            expected_symbols = item["expected_symbols"]
            async_dispatch = _truthy_env("LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH")

            def _trade_matches(trade: dict, expected_symbol: str) -> bool:
                if not isinstance(trade, dict) or trade.get("symbol") != expected_symbol:
                    return False
                if trade.get("attempted") is not True:
                    return False
                if async_dispatch:
                    return (
                        trade.get("executed") is False
                        and trade.get("ret_code") == -1
                        and trade.get("reason") == "tdlib_native_rest_dispatched"
                        and int(trade.get("order_send_started_monotonic_ns") or 0) > 0
                    )
                return (
                    trade.get("executed") is True
                    and trade.get("ret_code") == 0
                    and trade.get("reason") == "tdlib_native_rest"
                    and bool(trade.get("order_id"))
                )

            ok_trade = len(trades) >= len(expected_symbols) and all(
                _trade_matches(trade, expected_symbol)
                for trade, expected_symbol in zip(trades, expected_symbols)
            )
            if not ok_trade:
                return {
                    "ok": False,
                    "backend": "tdlib",
                    "mode": "tdlib_native_local_order_inject",
                    "reason": "native_local_order_mismatch",
                    "event": event.payload,
                    "mock_orders_seen": len(mock_orders),
                }
            relay_received_monotonic_ns = event.payload.get("relay_received_monotonic_ns")
            events.append(
                {
                    "channel_handle": event.payload.get("channel_handle"),
                    "message_id": event.payload.get("message_id"),
                    "title": event.payload.get("title"),
                    "ticker": event.payload.get("ticker"),
                    "tickers": event.payload.get("tickers"),
                    "markets": event.payload.get("markets") or ["KRW"],
                    "native_trade": event.payload.get("native_trade"),
                    "native_trades": trades,
                    "relay_received_monotonic_ns": relay_received_monotonic_ns,
                    "receive_to_last_order_send_started_us": _receive_to_trade_field_us(
                        relay_received_monotonic_ns,
                        trades,
                        "order_send_started_monotonic_ns",
                    ),
                    "receive_to_last_trade_finished_us": _receive_to_trade_field_us(
                        relay_received_monotonic_ns,
                        trades,
                        "trade_finished_monotonic_ns",
                    ),
                    "inject_to_listing_matched_us": elapsed_us,
                }
            )

        expected_order_count = sum(len(item["expected_symbols"]) for item in selected_cases)
        return {
            "ok": len(mock_orders) == expected_order_count,
            "backend": "tdlib",
            "mode": "tdlib_native_local_order_inject",
            "channels": args.channel,
            "chat_ids": chat_ids,
            "mock_base_url": mock_base_url,
            "native_status": native_status,
            "events_seen": len(events),
            "mock_orders_seen": len(mock_orders),
            "expected_order_count": expected_order_count,
            **_event_latency_summaries(events),
            "events": events,
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "tdlib",
            "mode": "tdlib_native_local_order_inject",
            "reason": "runtime_error",
            "error": str(exc),
            "mock_orders_seen": len(mock_orders),
        }
    finally:
        await asyncio.to_thread(relay.close)
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)
        temp_dir.cleanup()


async def _run_bench(args) -> dict:
    if args.native_preflight_inject:
        return await _run_native_preflight_inject(args)
    if args.native_local_order_inject:
        return await _run_native_local_order_inject(args)
    if args.native_file_order_inject:
        return await _run_native_file_order_inject(args)

    client = _make_client(args.backend)
    if not client.is_configured():
        return {
            "ok": False,
            "backend": args.backend,
            "reason": "telegram_api_not_configured",
        }
    if not client.has_session_file():
        return {
            "ok": False,
            "backend": args.backend,
            "reason": "telegram_session_missing",
        }

    if args.native_listing:
        os.environ["LISTING_TDLIB_NATIVE_BUY_ACTIVE"] = "0"
        os.environ["LISTING_TDLIB_NATIVE_BUY_ENABLED"] = "0"
    if args.native_buy_ready:
        os.environ["BYBIT_SPOT_BUY_ENABLED"] = "1"
        os.environ["BYBIT_API_KEY"] = os.environ.get("BYBIT_API_KEY", "live-smoke-key")
        os.environ["BYBIT_API_SECRET"] = os.environ.get("BYBIT_API_SECRET", "live-smoke-secret")
        os.environ["BYBIT_SPOT_BUY_USDT_AMOUNT"] = os.environ.get(
            "BYBIT_SPOT_BUY_USDT_AMOUNT",
            "5",
        )
        # Never send a smoke-test order to real Bybit. This mode proves TDLib
        # native-buy startup/readiness only; a rare matching live post would hit
        # localhost and fail safely instead of trading.
        os.environ["BYBIT_API_BASE_URL"] = os.environ.get(
            "LISTING_LIVE_SMOKE_BYBIT_BASE_URL",
            "https://127.0.0.1:1",
        )
        os.environ["LISTING_TDLIB_NATIVE_BUY_ACTIVE"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_BUY_ENABLED"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS"] = "1"
        os.environ["LISTING_TDLIB_NATIVE_IMMEDIATE_KEEPWARM_REFRESH"] = "0"
        os.environ["LISTING_TDLIB_NATIVE_KEEPWARM_INTERVAL"] = "3600"

    events: list[dict] = []
    first_event = asyncio.Event()
    startup_ready = asyncio.Event()

    def on_post(post: dict):
        now_ns = time.monotonic_ns()
        callback_at = datetime.now(timezone.utc)
        received_ns = int(post.get("received_monotonic_ns") or now_ns)
        receive_to_callback_us = round((now_ns - received_ns) / 1_000.0, 3)
        published_at = _as_utc_datetime(post.get("published_at"))
        received_at = _as_utc_datetime(post.get("received_at"))
        if received_at is None:
            received_at = callback_at - timedelta(microseconds=receive_to_callback_us)
        event = {
            "channel_handle": post.get("channel_handle"),
            "message_id": post.get("message_id"),
            "published_at": published_at.isoformat() if published_at else None,
            "received_at": received_at.isoformat() if received_at else None,
            "callback_at": callback_at.isoformat(),
            "published_to_received_ms": _delta_ms(received_at, published_at),
            "published_to_callback_ms": _delta_ms(callback_at, published_at),
            "receive_to_callback_us": receive_to_callback_us,
            "title": post.get("title") or post.get("text", "").splitlines()[0][:120],
        }
        if "native_listing" in post:
            event["native_listing"] = post["native_listing"]
        if "native_trade" in post:
            event["native_trade"] = post["native_trade"]
        if "native_trades" in post:
            event["native_trades"] = post["native_trades"]
        events.append(event)
        if len(events) >= args.iterations:
            first_event.set()

    trade_post = bool(args.native_listing or args.native_buy_ready)
    task = asyncio.create_task(
        client.run(
            channel_handles=args.channel,
            on_post=on_post,
            minimal_post=not trade_post,
            trade_post=trade_post,
            on_ready=startup_ready.set,
        )
    )
    wait_task = asyncio.create_task(first_event.wait())
    ready_task = asyncio.create_task(startup_ready.wait())
    try:
        started_at = time.monotonic()
        done, _pending = await asyncio.wait(
            {task, ready_task},
            timeout=args.timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            task.result()
        if not startup_ready.is_set():
            return {
                "ok": False,
                "backend": args.backend,
                "reason": "startup_ready_timeout",
                "timeout_sec": args.timeout,
            }
        if not args.native_buy_ready:
            remaining = max(0.0, args.timeout - (time.monotonic() - started_at))
            done, _pending = await asyncio.wait(
                {task, wait_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                task.result()
    except asyncio.TimeoutError:
        pass
    except Exception as exc:
        return {
            "ok": False,
            "backend": args.backend,
            "reason": "runtime_error",
            "error": str(exc),
        }
    finally:
        wait_task.cancel()
        ready_task.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, wait_task, ready_task, return_exceptions=True)

    return {
        "ok": True,
        "backend": args.backend,
        "channels": args.channel,
        "mode": (
            "tdlib_native_buy_ready_safe_no_real_bybit"
            if args.native_buy_ready
            else "tdlib_native_listing_no_buy"
            if args.native_listing
            else "raw_ingest"
        ),
        "timeout_sec": args.timeout,
        "startup_ready": startup_ready.is_set(),
        "target_iterations": args.iterations,
        "timestamp_note": "Telegram message dates are second-precision; published_to_* can include up to about 1s quantization.",
        "events_seen": len(events),
        "events": events,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    bench = subparsers.add_parser("bench", help="Run a bounded live ingest check")
    bench.add_argument("--backend", choices=["race", "telethon", "tdlib"], default="race")
    bench.add_argument("--iterations", type=int, default=24)
    bench.add_argument("--timeout", type=float, default=20.0)
    bench.add_argument("--pause-sec", type=float, default=0.75, help="Accepted for old README compatibility")
    bench.add_argument(
        "--native-listing",
        action="store_true",
        help="Use TDLib C++ native listing mode with live buy forcibly disabled",
    )
    bench.add_argument(
        "--native-buy-ready",
        action="store_true",
        help="Start TDLib C++ native-buy safely against localhost to prove readiness without real Bybit trading",
    )
    bench.add_argument(
        "--native-preflight-inject",
        action="store_true",
        help="Use live TDLib auth and official chat ids, then inject synthetic listing updates through the C++ native preflight path",
    )
    bench.add_argument(
        "--native-local-order-inject",
        action="store_true",
        help="Use live TDLib auth and official chat ids, then execute synthetic listing updates against a local mock Bybit order API",
    )
    bench.add_argument(
        "--native-file-order-inject",
        action="store_true",
        help="Use live TDLib auth and official chat ids, then execute synthetic listing updates through a file:// mock Bybit order API",
    )
    bench.add_argument(
        "--channel",
        action="append",
        default=["upbit_news", "BithumbExchange"],
        help="Telegram channel handle to watch; can be repeated",
    )
    args = parser.parse_args()

    if args.iterations <= 0:
        parser.error("--iterations must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    native_modes = [
        args.native_listing,
        args.native_buy_ready,
        args.native_preflight_inject,
        args.native_local_order_inject,
        args.native_file_order_inject,
    ]
    if sum(1 for enabled in native_modes if enabled) > 1:
        parser.error(
            "--native-listing, --native-buy-ready, --native-preflight-inject, "
            "--native-local-order-inject, and --native-file-order-inject are mutually exclusive"
        )
    if any(native_modes) and args.backend != "tdlib":
        parser.error(
            "--native-listing/--native-buy-ready/--native-preflight-inject/"
            "--native-local-order-inject/--native-file-order-inject requires --backend tdlib"
        )

    payload = asyncio.run(_run_bench(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
