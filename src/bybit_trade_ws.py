"""Persistent Bybit trade WebSocket order-entry client."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import socket
import threading
import time

from .env_loader import load_env_settings

logger = logging.getLogger(__name__)

DEFAULT_MAINNET_URL = "wss://stream.bybit.com/v5/trade"
DEFAULT_TESTNET_URL = "wss://stream-testnet.bybit.com/v5/trade"
DEFAULT_TIMEOUT_SEC = 3.0
DEFAULT_PING_INTERVAL_SEC = 15.0
DEFAULT_TIMESTAMP_BIAS_MS = -50


def _is_truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _guess_trade_url(base_url: str | None) -> str:
    target = (base_url or "").lower()
    if "testnet" in target or "stream-testnet" in target:
        return DEFAULT_TESTNET_URL
    return DEFAULT_MAINNET_URL


def _to_int(value: str | float | int | None, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class BybitTradeWebSocketExecutor:
    """Low-latency persistent order entry over Bybit trade WebSocket."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        ws_url: str | None = None,
        recv_window: int | None = None,
        enabled: bool | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        ping_interval_sec: float = DEFAULT_PING_INTERVAL_SEC,
        base_url: str | None = None,
        ws_factory=None,
        time_fn=None,
        timestamp_bias_ms: int | None = None,
    ):
        settings = load_env_settings(
            {
                "BYBIT_API_KEY",
                "BYBIT_API_SECRET",
                "BYBIT_API_BASE_URL",
                "BYBIT_RECV_WINDOW",
                "BYBIT_WS_ORDER_ENABLED",
                "BYBIT_WS_TRADE_URL",
                "BYBIT_WS_ORDER_TIMEOUT_SEC",
                "BYBIT_WS_PING_INTERVAL_SEC",
                "BYBIT_TIMESTAMP_BIAS_MS",
            }
        )
        self.api_key = api_key or settings.get("BYBIT_API_KEY", "")
        self.api_secret = api_secret or settings.get("BYBIT_API_SECRET", "")
        self.recv_window = int(
            recv_window
            or settings.get("BYBIT_RECV_WINDOW")
            or 5000
        )
        self.enabled = (
            _is_truthy(settings.get("BYBIT_WS_ORDER_ENABLED"))
            if enabled is None
            else bool(enabled)
        )
        resolved_base_url = (
            base_url
            or settings.get("BYBIT_API_BASE_URL")
            or ""
        )
        self.ws_url = (
            ws_url
            or settings.get("BYBIT_WS_TRADE_URL")
            or _guess_trade_url(resolved_base_url)
        )
        self.timeout_sec = float(
            settings.get("BYBIT_WS_ORDER_TIMEOUT_SEC") or timeout_sec
        )
        self.ping_interval_sec = float(
            settings.get("BYBIT_WS_PING_INTERVAL_SEC") or ping_interval_sec
        )
        self.timestamp_bias_ms = (
            _to_int(settings.get("BYBIT_TIMESTAMP_BIAS_MS"), DEFAULT_TIMESTAMP_BIAS_MS)
            if timestamp_bias_ms is None
            else int(timestamp_bias_ms)
        )
        self._time_fn = time_fn or time.time
        self._lock = threading.Lock()
        self._ws = None
        self._authenticated = False
        self._last_io_ns = 0

        if ws_factory is not None:
            self._ws_factory = ws_factory
            self._available = True
        else:
            try:
                import websocket  # type: ignore
            except ImportError:
                self._ws_factory = None
                self._available = False
            else:
                self._ws_factory = websocket.create_connection
                self._available = True

    def is_enabled(self) -> bool:
        return self.enabled and self._available and self.is_configured()

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.ws_url)

    def warmup(self):
        if not self.is_enabled():
            return
        with self._lock:
            self._ensure_ready_locked()

    def create_market_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: str,
        market_unit: str,
        order_link_id: str,
    ) -> dict:
        if not self.is_enabled():
            return {
                "attempted": False,
                "executed": False,
                "reason": "ws_disabled",
                "symbol": symbol,
            }

        with self._lock:
            try:
                return self._buy_market_once_locked(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    market_unit=market_unit,
                    order_link_id=order_link_id,
                )
            except Exception as exc:
                logger.warning("Bybit trade WS fast path failed, reconnecting once: %s", exc)
                self._close_locked()
                try:
                    return self._buy_market_once_locked(
                        symbol=symbol,
                        side=side,
                        qty=qty,
                        market_unit=market_unit,
                        order_link_id=order_link_id,
                    )
                except Exception as retry_exc:
                    return {
                        "attempted": True,
                        "executed": False,
                        "reason": str(retry_exc),
                        "symbol": symbol,
                        "transport": "ws_trade",
                    }

    def buy_market(
        self,
        *,
        symbol: str,
        qty: str,
        market_unit: str,
        order_link_id: str,
    ) -> dict:
        return self.create_market_order(
            symbol=symbol,
            side="Buy",
            qty=qty,
            market_unit=market_unit,
            order_link_id=order_link_id,
        )

    def sell_market(
        self,
        *,
        symbol: str,
        qty: str,
        order_link_id: str,
    ) -> dict:
        return self.create_market_order(
            symbol=symbol,
            side="Sell",
            qty=qty,
            market_unit="baseCoin",
            order_link_id=order_link_id,
        )

    def close(self):
        with self._lock:
            self._close_locked()

    def _buy_market_once_locked(
        self,
        *,
        symbol: str,
        side: str,
        qty: str,
        market_unit: str,
        order_link_id: str,
    ) -> dict:
        self._ensure_ready_locked()
        timestamp = str(int(self._time_fn() * 1000) + self.timestamp_bias_ms)
        request_id = f"ws-{time.monotonic_ns()}"
        payload = {
            "reqId": request_id,
            "header": {
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": str(self.recv_window),
            },
            "op": "order.create",
            "args": [
                {
                    "category": "spot",
                    "symbol": symbol,
                    "side": side,
                    "orderType": "Market",
                    "qty": qty,
                    "orderFilter": "Order",
                    "marketUnit": market_unit,
                    "orderLinkId": order_link_id,
                }
            ],
        }
        response = self._send_request_locked(
            payload=payload,
            matcher=lambda event: (
                event.get("reqId") == request_id
                and event.get("op") == "order.create"
            ),
        )
        ret_code = int(response.get("retCode", -1))
        result = {
            "attempted": True,
            "executed": ret_code == 0,
            "ret_code": ret_code,
            "reason": response.get("retMsg", ""),
            "symbol": symbol,
            "transport": "ws_trade",
        }
        data = response.get("data") or {}
        if isinstance(data, dict):
            if data.get("orderId"):
                result["order_id"] = data.get("orderId", "")
            if data.get("orderLinkId"):
                result["order_link_id"] = data.get("orderLinkId", "")
        if ret_code != 0 and not result["reason"]:
            result["reason"] = "order_create_failed"
        return result

    def _ensure_ready_locked(self):
        if self._ws is None:
            self._connect_locked()
            self._authenticate_locked()
            return

        if not self._authenticated:
            self._authenticate_locked()
            return

        if self.ping_interval_sec <= 0:
            return
        idle_ns = time.monotonic_ns() - self._last_io_ns if self._last_io_ns else 0
        if idle_ns >= int(self.ping_interval_sec * 1_000_000_000):
            self._ping_locked()

    def _connect_locked(self):
        if self._ws_factory is None:
            raise RuntimeError("websocket_client_unavailable")
        self._close_locked()
        self._ws = self._ws_factory(
            self.ws_url,
            timeout=self.timeout_sec,
            sockopt=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
        )
        self._authenticated = False
        self._last_io_ns = time.monotonic_ns()

    def _authenticate_locked(self):
        expires = int((self._time_fn() + 1.0) * 1000)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            f"GET/realtime{expires}".encode(),
            hashlib.sha256,
        ).hexdigest()
        payload = {
            "op": "auth",
            "args": [self.api_key, expires, signature],
        }
        response = self._send_request_locked(
            payload=payload,
            matcher=lambda event: event.get("op") == "auth",
        )
        success = bool(response.get("success")) or int(response.get("retCode", -1)) == 0
        if not success:
            raise RuntimeError(
                response.get("retMsg")
                or response.get("ret_msg")
                or "ws_auth_failed"
            )
        self._authenticated = True

    def _ping_locked(self):
        response = self._send_request_locked(
            payload={"op": "ping"},
            matcher=lambda event: event.get("op") in {"pong", "ping"},
        )
        if response.get("ret_msg") not in {"pong", "", None} and response.get("op") not in {"pong", "ping"}:
            raise RuntimeError("ws_ping_failed")

    def _send_request_locked(self, *, payload: dict, matcher) -> dict:
        if self._ws is None:
            raise RuntimeError("ws_not_connected")
        raw = json.dumps(payload, separators=(",", ":"))
        self._ws.send(raw)
        self._last_io_ns = time.monotonic_ns()
        deadline = time.monotonic() + self.timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ws_trade_timeout")
            if hasattr(self._ws, "settimeout"):
                self._ws.settimeout(remaining)
            event = self._recv_json_locked()
            if event.get("op") == "auth" and bool(event.get("success")):
                self._authenticated = True
            if matcher(event):
                return event

    def _recv_json_locked(self) -> dict:
        if self._ws is None:
            raise RuntimeError("ws_not_connected")
        payload = self._ws.recv()
        self._last_io_ns = time.monotonic_ns()
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return json.loads(payload)

    def _close_locked(self):
        if self._ws is None:
            return
        try:
            self._ws.close()
        except Exception:
            pass
        self._ws = None
        self._authenticated = False
        self._last_io_ns = 0
