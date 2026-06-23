"""Immediate Bybit spot market buyer for new listing signals."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
from contextlib import contextmanager
from decimal import ROUND_DOWN, Decimal, InvalidOperation

import httpx

from .bybit_trade_ws import BybitTradeWebSocketExecutor
from .bybit_client import BybitClient
from .cpp_fast_buyer import CppFastBuyerBridge
from .cpp_ws_trade_buyer import CppWsTradeBuyerBridge
from .env_loader import load_env_settings

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.bybit.com"
DEFAULT_RECV_WINDOW = 5000
DEFAULT_TIMEOUT = 10
DEFAULT_BUY_MODE = "quoteCoin"
DEFAULT_ORDER_TRANSPORT_PREFERENCE = "cpp"
DEFAULT_TIMESTAMP_BIAS_MS = -50
# Hard ceiling on a single market buy. This is a money-safety backstop against a
# fat-finger / misplaced-decimal / non-finite BYBIT_SPOT_BUY_USDT_AMOUNT: any
# configured quote amount above it (or non-finite) refuses to send an order
# instead of spending the value verbatim. Override with
# BYBIT_SPOT_BUY_MAX_USDT_AMOUNT; the default is intentionally finite.
DEFAULT_MAX_BUY_USDT_AMOUNT = 1000.0


def _is_truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Bybit V5 marketUnit only accepts "baseCoin" or "quoteCoin". Map the common
# shorthands ("quote"/"base", any casing) so a stale config value does not get
# sent verbatim and rejected by some transports.
BUY_MODE_ALIASES = {
    "quotecoin": "quoteCoin",
    "quote": "quoteCoin",
    "basecoin": "baseCoin",
    "base": "baseCoin",
}
_MARKET_UNIT_ALIASES = BUY_MODE_ALIASES


def _normalize_buy_mode(value: str | None) -> str:
    if not value:
        return DEFAULT_BUY_MODE
    return BUY_MODE_ALIASES.get(value.strip().lower(), value.strip())


def _normalize_market_unit(value: str | None) -> str:
    return _normalize_buy_mode(value)


def _to_float(value: str | float | int | None, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: str | float | int | None, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class BybitSpotBuyer:
    """Execute immediate Bybit spot market buys using the V5 REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        buy_enabled: bool | None = None,
        buy_usdt_amount: float | None = None,
        max_usdt_amount: float | None = None,
        recv_window: int | None = None,
        buy_mode: str | None = None,
        query_fill_after_buy: bool | None = None,
        fast_executor: CppFastBuyerBridge | None = None,
        cpp_ws_executor: CppWsTradeBuyerBridge | None = None,
        ws_executor: BybitTradeWebSocketExecutor | None = None,
        prefer_cached_symbol_check: bool | None = None,
        order_transport_preference: str | None = None,
        resolve_duplicate_order_link_id: bool | None = None,
        split_across_tickers: bool | None = None,
        require_fast_executor_warmup: bool | None = None,
        timestamp_bias_ms: int | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        market_client: BybitClient | None = None,
    ):
        settings = load_env_settings(
            {
                "BYBIT_API_KEY",
                "BYBIT_API_SECRET",
                "BYBIT_API_BASE_URL",
                "BYBIT_SPOT_BUY_ENABLED",
                "BYBIT_SPOT_BUY_USDT_AMOUNT",
                "BYBIT_SPOT_BUY_MAX_USDT_AMOUNT",
                "BYBIT_RECV_WINDOW",
                "BYBIT_SPOT_BUY_MODE",
                "BYBIT_QUERY_FILL_AFTER_BUY",
                "BYBIT_FAST_EXECUTOR_ENABLED",
                "BYBIT_REQUIRE_FAST_EXECUTOR_WARMUP",
                "BYBIT_WS_ORDER_ENABLED",
                "BYBIT_CPP_WS_EXECUTOR_ENABLED",
                "BYBIT_PREFER_CACHED_SYMBOL_CHECK",
                "BYBIT_ORDER_TRANSPORT_PREFERENCE",
                "BYBIT_RESOLVE_DUPLICATE_ORDER_LINK_ID",
                "BYBIT_SPOT_BUY_SPLIT_ACROSS_TICKERS",
                "BYBIT_TIMESTAMP_BIAS_MS",
            }
        )

        self.api_key = (
            settings.get("BYBIT_API_KEY", "") if api_key is None else api_key
        )
        self.api_secret = (
            settings.get("BYBIT_API_SECRET", "") if api_secret is None else api_secret
        )
        self.base_url = (
            base_url
            or settings.get("BYBIT_API_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.buy_enabled = (
            _is_truthy(settings.get("BYBIT_SPOT_BUY_ENABLED"))
            if buy_enabled is None
            else bool(buy_enabled)
        )
        self.buy_usdt_amount = (
            _to_float(settings.get("BYBIT_SPOT_BUY_USDT_AMOUNT"))
            if buy_usdt_amount is None
            else float(buy_usdt_amount)
        )
        self.max_usdt_amount = (
            _to_float(
                settings.get("BYBIT_SPOT_BUY_MAX_USDT_AMOUNT"),
                DEFAULT_MAX_BUY_USDT_AMOUNT,
            )
            if max_usdt_amount is None
            else float(max_usdt_amount)
        )
        if not math.isfinite(self.max_usdt_amount) or self.max_usdt_amount <= 0:
            self.max_usdt_amount = DEFAULT_MAX_BUY_USDT_AMOUNT
        raw_split = settings.get("BYBIT_SPOT_BUY_SPLIT_ACROSS_TICKERS")
        self.split_across_tickers = (
            bool(split_across_tickers)
            if split_across_tickers is not None
            else (
                True
                if raw_split is None or str(raw_split).strip() == ""
                else _is_truthy(raw_split)
            )
        )
        self.recv_window = int(
            recv_window
            or settings.get("BYBIT_RECV_WINDOW")
            or DEFAULT_RECV_WINDOW
        )
        self.buy_mode = _normalize_buy_mode(
            buy_mode
            or settings.get("BYBIT_SPOT_BUY_MODE")
            or DEFAULT_BUY_MODE
        )
        self.query_fill_after_buy = (
            _is_truthy(settings.get("BYBIT_QUERY_FILL_AFTER_BUY"))
            if query_fill_after_buy is None
            else bool(query_fill_after_buy)
        )
        self.prefer_cached_symbol_check = (
            _is_truthy(settings.get("BYBIT_PREFER_CACHED_SYMBOL_CHECK", "1"))
            if prefer_cached_symbol_check is None
            else bool(prefer_cached_symbol_check)
        )
        self.order_transport_preference = (
            order_transport_preference
            or settings.get("BYBIT_ORDER_TRANSPORT_PREFERENCE")
            or DEFAULT_ORDER_TRANSPORT_PREFERENCE
        ).strip().lower()
        self.resolve_duplicate_order_link_id = (
            _is_truthy(settings.get("BYBIT_RESOLVE_DUPLICATE_ORDER_LINK_ID", "1"))
            if resolve_duplicate_order_link_id is None
            else bool(resolve_duplicate_order_link_id)
        )
        self.require_fast_executor_warmup = (
            _is_truthy(settings.get("BYBIT_REQUIRE_FAST_EXECUTOR_WARMUP", "0"))
            if require_fast_executor_warmup is None
            else bool(require_fast_executor_warmup)
        )
        self.timestamp_bias_ms = (
            _to_int(settings.get("BYBIT_TIMESTAMP_BIAS_MS"), DEFAULT_TIMESTAMP_BIAS_MS)
            if timestamp_bias_ms is None
            else int(timestamp_bias_ms)
        )
        self.timeout = timeout
        self.market_client = market_client or BybitClient(timeout=timeout)
        self.fast_executor = (
            fast_executor
            if fast_executor is not None
            else CppFastBuyerBridge()
        )
        self.cpp_ws_executor = (
            cpp_ws_executor
            if cpp_ws_executor is not None
            else CppWsTradeBuyerBridge()
        )
        self.ws_executor = (
            ws_executor
            if ws_executor is not None
            else BybitTradeWebSocketExecutor(
                api_key=self.api_key,
                api_secret=self.api_secret,
                recv_window=self.recv_window,
                base_url=self.base_url,
            )
        )
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
        self._market_buy_plan = self._plan_market_buy()
        self._market_buy_qty = float(self._market_buy_plan["qty"])
        self._market_buy_qty_str = self._market_buy_plan["qty_str"]
        self._market_buy_reason = self._market_buy_plan["reason"]
        self._amount_lock = threading.Lock()
        if self.cpp_ws_executor.is_enabled():
            try:
                self.cpp_ws_executor.warmup()
            except Exception as exc:
                logger.warning("C++ Bybit trade WS warmup failed: %s", exc)
        if self.ws_executor.is_enabled():
            try:
                self.ws_executor.warmup()
            except Exception as exc:
                logger.warning("Bybit trade WS warmup failed: %s", exc)
        if self.fast_executor.is_enabled():
            try:
                warmup_result = self.fast_executor.warmup()
                if (
                    self.require_fast_executor_warmup
                    and isinstance(warmup_result, dict)
                    and not warmup_result.get("ok")
                ):
                    raise RuntimeError(f"C++ fast executor warmup not ready: {warmup_result}")
            except Exception as exc:
                if self.require_fast_executor_warmup:
                    raise RuntimeError("C++ fast executor warmup is required") from exc
                logger.warning("C++ fast executor warmup failed: %s", exc)
        self._transport_order = self._iter_order_transports()
        self._buy_market_impl = self._select_buy_market_impl()

    def warmup(self, force_refresh_market_cache: bool = False) -> dict:
        warmed = {
            "market_cache_refreshed": False,
            "cpp_ws_executor_warmed": False,
            "ws_executor_warmed": False,
            "fast_executor_warmed": False,
        }
        refresh = getattr(self.market_client, "refresh_market_cache", None)
        if callable(refresh):
            try:
                refresh(force=force_refresh_market_cache)
                warmed["market_cache_refreshed"] = True
            except Exception as exc:  # pragma: no cover - warmup safeguard
                logger.warning("Bybit market cache warmup failed: %s", exc)

        if self.cpp_ws_executor.is_enabled():
            try:
                self.cpp_ws_executor.warmup()
                warmed["cpp_ws_executor_warmed"] = True
            except Exception as exc:  # pragma: no cover - warmup safeguard
                logger.warning("C++ Bybit trade WS warmup failed: %s", exc)

        if self.ws_executor.is_enabled():
            try:
                self.ws_executor.warmup()
                warmed["ws_executor_warmed"] = True
            except Exception as exc:  # pragma: no cover - warmup safeguard
                logger.warning("Bybit trade WS warmup failed: %s", exc)

        if self.fast_executor.is_enabled():
            try:
                warmup_result = self.fast_executor.warmup()
                warmed["fast_executor_warmed"] = True
                warmed["fast_executor_result"] = warmup_result
                if (
                    self.require_fast_executor_warmup
                    and isinstance(warmup_result, dict)
                    and not warmup_result.get("ok")
                ):
                    raise RuntimeError(f"C++ fast executor warmup not ready: {warmup_result}")
            except Exception as exc:  # pragma: no cover - warmup safeguard
                if self.require_fast_executor_warmup:
                    raise RuntimeError("C++ fast executor warmup is required") from exc
                logger.warning("C++ fast executor warmup failed: %s", exc)
        return warmed

    def is_enabled(self) -> bool:
        return self.buy_enabled and self.is_configured()

    def is_configured(self) -> bool:
        return bool(
            self.api_key
            and self.api_secret
            and self.buy_usdt_amount > 0
        )

    def buy_market(
        self,
        *,
        ticker: str,
        order_link_id: str,
    ) -> dict:
        return self._buy_market_impl(
            ticker=ticker,
            order_link_id=order_link_id,
        )

    def buy_markets(self, orders: list[dict]) -> list[dict]:
        if not orders:
            return []
        if len(orders) == 1:
            order = orders[0]
            return [
                self.buy_market(
                    ticker=order["ticker"],
                    order_link_id=order["order_link_id"],
                )
            ]
        # Treat BYBIT_SPOT_BUY_USDT_AMOUNT as a per-ANNOUNCEMENT budget: when one
        # announcement lists N tickers, split the budget equally so the total
        # spent stays at the configured amount (amount/N each) instead of
        # amount*N. ROUND_DOWN keeps the summed spend at or below budget.
        if self.split_across_tickers:
            with self._scoped_quote_amount(self._split_amount_per_order(len(orders))):
                return self._dispatch_buy_markets(orders)
        return self._dispatch_buy_markets(orders)

    def _dispatch_buy_markets(self, orders: list[dict]) -> list[dict]:
        if self._transport_order == ("cpp",) and callable(
            getattr(self.fast_executor, "buy_markets_quote_text", None)
        ):
            return self._buy_markets_cpp_only(orders)
        return [
            self.buy_market(
                ticker=order["ticker"],
                order_link_id=order["order_link_id"],
            )
            for order in orders
        ]

    def _split_amount_per_order(self, ticker_count: int) -> float:
        if ticker_count <= 1:
            return self.buy_usdt_amount
        per = (
            Decimal(str(self.buy_usdt_amount)) / Decimal(ticker_count)
        ).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        return float(per)

    @contextmanager
    def _scoped_quote_amount(self, usdt_amount: float):
        # Temporarily rebind the quote amount (and its derived qty plan) for a
        # batch so every transport reads the split per-order amount. Held under a
        # lock so a concurrent buy can never observe a half-swapped amount.
        plan = self._plan_market_buy(usdt_amount)
        with self._amount_lock:
            saved = (
                self.buy_usdt_amount,
                self._market_buy_qty,
                self._market_buy_qty_str,
                self._market_buy_reason,
            )
            self.buy_usdt_amount = float(usdt_amount)
            self._market_buy_qty = float(plan["qty"])
            self._market_buy_qty_str = plan["qty_str"]
            self._market_buy_reason = plan["reason"]
            try:
                yield
            finally:
                (
                    self.buy_usdt_amount,
                    self._market_buy_qty,
                    self._market_buy_qty_str,
                    self._market_buy_reason,
                ) = saved

    def _buy_market_general(
        self,
        *,
        ticker: str,
        order_link_id: str,
    ) -> dict:
        trade_started_ns = time.monotonic_ns()
        symbol = f"{ticker.upper()}USDT"
        result = self._build_buy_result(symbol=symbol, order_link_id=order_link_id)

        if not self.buy_enabled:
            result["reason"] = "buy_disabled"
            return self._annotate_trade_timing(result, trade_started_ns)

        if not self.is_configured():
            result["reason"] = "missing_api_config"
            return self._annotate_trade_timing(result, trade_started_ns)

        # Validate the quote amount before any market lookup: a bad/over-ceiling
        # amount is a config error, so fail fast and network-free.
        if self._market_buy_qty <= 0:
            result["reason"] = self._market_buy_reason
            return self._annotate_trade_timing(result, trade_started_ns)

        # On fast transports we let the execution channel or Bybit validate the
        # symbol instead of blocking on a synchronous market cache refresh here.
        if not self._transport_order and not self._spot_symbol_available(symbol):
            result["reason"] = "spot_symbol_unavailable"
            return self._annotate_trade_timing(result, trade_started_ns)

        for transport in self._transport_order:
            if transport == "cpp_ws":
                cpp_ws_result = self.cpp_ws_executor.buy_market(
                    symbol=symbol,
                    qty=self._market_buy_qty_str,
                    market_unit=self.buy_mode,
                    order_link_id=order_link_id,
                )
                result.update(cpp_ws_result)
                if result.get("executed"):
                    return self._annotate_trade_timing(result, trade_started_ns)
                result["reason"] = result.get("reason", "cpp_ws_trade_failed")
                if self._resolve_duplicate_order(result, order_link_id):
                    return self._annotate_trade_timing(result, trade_started_ns)
                if not self._should_continue_transport_fallback(result):
                    return self._annotate_trade_timing(result, trade_started_ns)
                continue

            if transport == "cpp":
                buy_market_quote_text = getattr(
                    self.fast_executor,
                    "buy_market_quote_text",
                    None,
                )
                if callable(buy_market_quote_text):
                    fast_result = buy_market_quote_text(
                        symbol=symbol,
                        quote_amount_text=self._market_buy_qty_str,
                        order_link_id=order_link_id,
                    )
                else:
                    fast_result = self.fast_executor.buy_market(
                        symbol=symbol,
                        quote_amount=self.buy_usdt_amount,
                        order_link_id=order_link_id,
                    )
                result.update(fast_result)
                if result.get("executed"):
                    return self._annotate_trade_timing(result, trade_started_ns)
                result["reason"] = result.get("reason", "cpp_fast_path_failed")
                if self._resolve_duplicate_order(result, order_link_id):
                    return self._annotate_trade_timing(result, trade_started_ns)
                if not self._should_continue_transport_fallback(result):
                    return self._annotate_trade_timing(result, trade_started_ns)
                continue

            if transport == "ws":
                ws_result = self.ws_executor.buy_market(
                    symbol=symbol,
                    qty=self._market_buy_qty_str,
                    market_unit=self.buy_mode,
                    order_link_id=order_link_id,
                )
                result.update(ws_result)
                if result.get("executed"):
                    return self._annotate_trade_timing(result, trade_started_ns)
                result["reason"] = result.get("reason", "ws_trade_failed")
                if self._resolve_duplicate_order(result, order_link_id):
                    return self._annotate_trade_timing(result, trade_started_ns)
                if not self._should_continue_transport_fallback(result):
                    return self._annotate_trade_timing(result, trade_started_ns)

        result["attempted"] = True
        result["transport"] = "rest_trade"
        body = json.dumps(
            {
                "category": "spot",
                "symbol": symbol,
                "side": "Buy",
                "orderType": "Market",
                "qty": self._market_buy_qty_str,
                "orderFilter": "Order",
                "marketUnit": self.buy_mode,
                "orderLinkId": order_link_id,
            },
            separators=(",", ":"),
        )
        response = self._request_json(
            method="POST",
            path="/v5/order/create",
            body=body,
            auth=True,
        )

        ret_code = int(response.get("retCode", -1))
        ret_msg = response.get("retMsg", "")
        result["ret_code"] = ret_code
        if ret_code != 0:
            result["reason"] = ret_msg or "order_create_failed"
            self._resolve_duplicate_order(result, order_link_id)
            return self._annotate_trade_timing(result, trade_started_ns)

        order_id = (
            response.get("result", {}).get("orderId")
            or ""
        )
        result["order_id"] = order_id
        result["executed"] = True

        if self.query_fill_after_buy:
            fill = self.query_order_fill(order_id)
            if fill:
                result.update(fill)
        return self._annotate_trade_timing(result, trade_started_ns)

    def _buy_market_cpp_only(
        self,
        *,
        ticker: str,
        order_link_id: str,
    ) -> dict:
        trade_started_ns = time.monotonic_ns()
        symbol = f"{ticker.upper()}USDT"

        def build_result() -> dict:
            return self._build_buy_result(
                symbol=symbol,
                order_link_id=order_link_id,
            )

        if not self.buy_enabled:
            result = build_result()
            result["reason"] = "buy_disabled"
            return self._annotate_trade_timing(result, trade_started_ns)

        if not self.is_configured():
            result = build_result()
            result["reason"] = "missing_api_config"
            return self._annotate_trade_timing(result, trade_started_ns)

        if self._market_buy_qty <= 0:
            result = build_result()
            result["reason"] = self._market_buy_reason
            return self._annotate_trade_timing(result, trade_started_ns)

        buy_market_quote_text = getattr(self.fast_executor, "buy_market_quote_text", None)
        if callable(buy_market_quote_text):
            fast_result = buy_market_quote_text(
                symbol=symbol,
                quote_amount_text=self._market_buy_qty_str,
                order_link_id=order_link_id,
            )
        else:
            fast_result = self.fast_executor.buy_market(
                symbol=symbol,
                quote_amount=self.buy_usdt_amount,
                order_link_id=order_link_id,
            )
        result = build_result()
        result.update(fast_result)
        if result.get("executed"):
            return self._annotate_trade_timing(result, trade_started_ns)
        result["reason"] = result.get("reason", "cpp_fast_path_failed")
        self._resolve_duplicate_order(result, order_link_id)
        return self._annotate_trade_timing(result, trade_started_ns)

    def _buy_markets_cpp_only(self, orders: list[dict]) -> list[dict]:
        trade_started_ns = time.monotonic_ns()
        symbols = [f"{str(order['ticker']).upper()}USDT" for order in orders]

        def build_result(index: int) -> dict:
            return self._build_buy_result(
                symbol=symbols[index],
                order_link_id=orders[index]["order_link_id"],
            )

        if not self.buy_enabled:
            trades = []
            for index in range(len(orders)):
                result = build_result(index)
                result["reason"] = "buy_disabled"
                trades.append(self._annotate_trade_timing(result, trade_started_ns))
            return trades

        if not self.is_configured():
            trades = []
            for index in range(len(orders)):
                result = build_result(index)
                result["reason"] = "missing_api_config"
                trades.append(self._annotate_trade_timing(result, trade_started_ns))
            return trades

        if self._market_buy_qty <= 0:
            trades = []
            for index in range(len(orders)):
                result = build_result(index)
                result["reason"] = self._market_buy_reason
                trades.append(self._annotate_trade_timing(result, trade_started_ns))
            return trades

        fast_results = self.fast_executor.buy_markets_quote_text(
            orders=[
                (symbols[index], orders[index]["order_link_id"])
                for index in range(len(orders))
            ],
            quote_amount_text=self._market_buy_qty_str,
        )
        trades: list[dict] = []
        for index, fast_result in enumerate(fast_results[:len(orders)]):
            result = build_result(index)
            result.update(fast_result)
            if not result.get("executed"):
                result["reason"] = result.get("reason", "cpp_fast_path_failed")
                self._resolve_duplicate_order(result, orders[index]["order_link_id"])
            trades.append(self._annotate_trade_timing(result, trade_started_ns))
        for index in range(len(trades), len(orders)):
            result = build_result(index)
            result["reason"] = "cpp_fast_path_bulk_missing_response"
            trades.append(self._annotate_trade_timing(result, trade_started_ns))
        return trades

    def _spot_symbol_available(self, symbol: str) -> bool:
        if self.prefer_cached_symbol_check and self.market_client.is_cache_ready():
            return self.market_client.has_symbol_cached("spot", symbol)
        return self.market_client.has_symbol("spot", symbol)

    def _build_buy_result(self, *, symbol: str, order_link_id: str) -> dict:
        return {
            "enabled": self.buy_enabled,
            "attempted": False,
            "executed": False,
            "symbol": symbol,
            "side": "Buy",
            "order_type": "Market",
            "market_unit": self.buy_mode,
            "requested_usdt": self.buy_usdt_amount,
            "qty": self._market_buy_qty,
            "order_link_id": order_link_id,
        }

    def _resolve_duplicate_order(self, result: dict, order_link_id: str) -> bool:
        if not self.resolve_duplicate_order_link_id:
            return False
        reason = str(result.get("reason", ""))
        if "duplicate" not in reason.lower():
            return False
        existing = self.query_order_by_link_id(order_link_id)
        if existing:
            result.update(existing)
            result["executed"] = True
            result["reason"] = "duplicate_order_link_id_existing_order"
            return True
        return False

    @staticmethod
    def _ret_code(result: dict) -> int:
        try:
            return int(result.get("ret_code", -1))
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _is_ambiguous_send_failure(reason: str) -> bool:
        # A timeout means the request likely reached Bybit but the response was
        # lost — the order may already be live. Re-sending the same order on the
        # next transport could double-fill in the window before Bybit registers
        # the (idempotent) orderLinkId, so a timeout must NOT trigger a fallback.
        text = reason.lower()
        return "timeout" in text or "timed out" in text or "time out" in text

    def _should_continue_transport_fallback(self, result: dict) -> bool:
        reason = str(result.get("reason", "")).lower()
        if "duplicate" in reason:
            return False
        if self._is_ambiguous_send_failure(reason):
            return False
        if reason in {
            "spot_symbol_unavailable",
            "missing_api_config",
            "quote_amount_invalid",
            "quote_amount_exceeds_max",
        }:
            return False
        return self._ret_code(result) == -1

    # Maps every documented BYBIT_ORDER_TRANSPORT_PREFERENCE token to one of the
    # internal transports. Accepts both the short names ("cpp", "cpp_ws", "ws")
    # and the README's explicit names ("cpp_rest", "python_ws", "python_rest").
    # "python_rest" is the always-last HTTP fallback and has no internal slot, so
    # it maps to None and only affects ordering of the others.
    _TRANSPORT_ALIASES = {
        "cpp": "cpp",
        "cpp_rest": "cpp",
        "cpp-rest": "cpp",
        "cpp_ws": "cpp_ws",
        "cpp-ws": "cpp_ws",
        "native_ws": "cpp_ws",
        "ws": "ws",
        "websocket": "ws",
        "python_ws": "ws",
        "python-ws": "ws",
        "python_rest": None,
        "python-rest": None,
        "rest": None,
    }

    def _parse_transport_preference(self) -> tuple[str, ...]:
        """Resolve the configured preference into an ordered transport list.

        Backwards compatible with the legacy single-token forms ("cpp_ws",
        "ws", or anything else -> cpp-first). Additionally supports the CSV form
        documented in .env.example (e.g.
        ``cpp_rest,cpp_ws,python_ws,python_rest``): tokens are mapped via
        ``_TRANSPORT_ALIASES``, unknown tokens are ignored, and any transport not
        named is appended in the default order so a partial preference still
        falls back to the others.
        """
        preference = self.order_transport_preference

        # Preserve the exact legacy ordering for the original single tokens.
        if "," not in preference:
            if preference in {"cpp_ws", "cpp-ws", "native_ws"}:
                return ("cpp_ws", "cpp", "ws")
            if preference in {"ws", "websocket"}:
                return ("ws", "cpp_ws", "cpp")
            if preference not in self._TRANSPORT_ALIASES or preference in {
                "cpp",
                "cpp_rest",
                "cpp-rest",
            }:
                return ("cpp", "cpp_ws", "ws")

        default_order = ("cpp", "cpp_ws", "ws")
        ordered: list[str] = []
        for raw_token in preference.split(","):
            token = raw_token.strip().lower()
            if not token or token not in self._TRANSPORT_ALIASES:
                continue
            transport = self._TRANSPORT_ALIASES[token]
            if transport is not None and transport not in ordered:
                ordered.append(transport)
        # Append any transports not named in the preference so a partial
        # preference still falls back to the others.
        for transport in default_order:
            if transport not in ordered:
                ordered.append(transport)
        return tuple(ordered) if ordered else default_order

    def _iter_order_transports(self) -> tuple[str, ...]:
        cpp_ws_enabled = self.cpp_ws_executor.is_enabled()
        fast_enabled = self.fast_executor.is_enabled()
        ws_enabled = self.ws_executor.is_enabled()

        ordered = self._parse_transport_preference()

        enabled: list[str] = []
        for transport in ordered:
            if transport == "cpp_ws" and cpp_ws_enabled:
                enabled.append("cpp_ws")
            elif transport == "cpp" and fast_enabled:
                enabled.append("cpp")
            elif transport == "ws" and ws_enabled:
                enabled.append("ws")
        return tuple(enabled)

    def _select_buy_market_impl(self):
        if self._transport_order == ("cpp",):
            return self._buy_market_cpp_only
        return self._buy_market_general

    def sell_market_base_qty(
        self,
        *,
        symbol: str,
        base_qty: str,
        order_link_id: str,
    ) -> dict:
        trade_started_ns = time.monotonic_ns()
        result = {
            "enabled": self.buy_enabled,
            "attempted": False,
            "executed": False,
            "symbol": symbol,
            "side": "Sell",
            "order_type": "Market",
            "market_unit": "baseCoin",
            "requested_qty": base_qty,
            "order_link_id": order_link_id,
        }

        if not self.buy_enabled:
            result["reason"] = "buy_disabled"
            return self._annotate_trade_timing(result, trade_started_ns)

        if not self.is_configured():
            result["reason"] = "missing_api_config"
            return self._annotate_trade_timing(result, trade_started_ns)

        if not self.market_client.has_symbol("spot", symbol):
            result["reason"] = "spot_symbol_unavailable"
            return self._annotate_trade_timing(result, trade_started_ns)

        qty_text = str(base_qty).strip()
        if not qty_text:
            result["reason"] = "base_qty_missing"
            return self._annotate_trade_timing(result, trade_started_ns)

        if self.cpp_ws_executor.is_enabled():
            cpp_ws_result = self.cpp_ws_executor.sell_market(
                symbol=symbol,
                qty=qty_text,
                order_link_id=order_link_id,
            )
            result.update(cpp_ws_result)
            if not result.get("executed"):
                result["reason"] = result.get("reason", "cpp_ws_trade_failed")
            return self._annotate_trade_timing(result, trade_started_ns)

        if self.ws_executor.is_enabled():
            ws_result = self.ws_executor.sell_market(
                symbol=symbol,
                qty=qty_text,
                order_link_id=order_link_id,
            )
            result.update(ws_result)
            if not result.get("executed"):
                result["reason"] = result.get("reason", "ws_trade_failed")
            return self._annotate_trade_timing(result, trade_started_ns)

        result["attempted"] = True
        result["transport"] = "rest_trade"
        body = json.dumps(
            {
                "category": "spot",
                "symbol": symbol,
                "side": "Sell",
                "orderType": "Market",
                "qty": qty_text,
                "orderFilter": "Order",
                "marketUnit": "baseCoin",
                "orderLinkId": order_link_id,
            },
            separators=(",", ":"),
        )
        response = self._request_json(
            method="POST",
            path="/v5/order/create",
            body=body,
            auth=True,
        )

        ret_code = int(response.get("retCode", -1))
        ret_msg = response.get("retMsg", "")
        result["ret_code"] = ret_code
        if ret_code != 0:
            result["reason"] = ret_msg or "order_create_failed"
            return self._annotate_trade_timing(result, trade_started_ns)

        result["order_id"] = response.get("result", {}).get("orderId") or ""
        result["executed"] = True
        return self._annotate_trade_timing(result, trade_started_ns)

    def _plan_market_buy(self, usdt_amount: float | None = None) -> dict:
        # Money-safety gate: a non-finite, non-positive, or above-ceiling quote
        # amount yields qty=0 so every buy path's `qty <= 0` guard refuses to
        # send an order instead of spending the bad value verbatim.
        invalid = {"qty": 0.0, "qty_str": "0", "reason": "quote_amount_invalid"}
        raw = self.buy_usdt_amount if usdt_amount is None else usdt_amount
        if raw is None or not math.isfinite(float(raw)) or float(raw) <= 0:
            return invalid
        try:
            qty = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return invalid
        qty_float = float(qty)
        if not math.isfinite(qty_float) or qty_float <= 0:
            return invalid
        if qty_float > self.max_usdt_amount:
            logger.error(
                "BYBIT_SPOT_BUY_USDT_AMOUNT=%s exceeds the safety ceiling %s — "
                "refusing to buy. Raise BYBIT_SPOT_BUY_MAX_USDT_AMOUNT only if "
                "this is intentional.",
                qty_float,
                self.max_usdt_amount,
            )
            return {
                "qty": 0.0,
                "qty_str": "0",
                "reason": "quote_amount_exceeds_max",
            }
        return {
            "qty": qty_float,
            "qty_str": _format_decimal(qty),
            "reason": "",
        }

    def query_spot_balance(self, coin: str) -> dict | None:
        """Read-only signed wallet-balance lookup for one coin (no order)."""
        query = urllib.parse.urlencode(
            {"accountType": "UNIFIED", "coin": coin.upper()}
        )
        response = self._request_json(
            method="GET",
            path="/v5/account/wallet-balance",
            query=query,
            auth=True,
        )
        ret_code = int(response.get("retCode", -1))
        if ret_code != 0:
            return {
                "ret_code": ret_code,
                "ret_msg": response.get("retMsg", ""),
                "available": None,
            }
        accounts = response.get("result", {}).get("list", [])
        for account in accounts:
            for entry in account.get("coin", []):
                if str(entry.get("coin", "")).upper() == coin.upper():
                    return {
                        "ret_code": 0,
                        "available": _to_float(
                            entry.get("availableToWithdraw")
                            or entry.get("walletBalance")
                        ),
                        "wallet": _to_float(entry.get("walletBalance")),
                    }
        return {"ret_code": 0, "available": 0.0, "wallet": 0.0}

    def query_order_fill(self, order_id: str) -> dict | None:
        if not order_id:
            return None
        query = urllib.parse.urlencode(
            {
                "category": "spot",
                "orderId": order_id,
            }
        )
        response = self._request_json(
            method="GET",
            path="/v5/order/realtime",
            query=query,
            auth=True,
        )
        items = response.get("result", {}).get("list", [])
        if not items:
            return None
        item = items[0]
        avg_price = _to_float(item.get("avgPrice"))
        filled_qty = _to_float(item.get("cumExecQty"))
        return {
            "avg_price": avg_price,
            "filled_qty": filled_qty,
        }

    def query_order_by_link_id(self, order_link_id: str) -> dict | None:
        if not order_link_id:
            return None
        query = urllib.parse.urlencode(
            {
                "category": "spot",
                "orderLinkId": order_link_id,
            }
        )
        response = self._request_json(
            method="GET",
            path="/v5/order/realtime",
            query=query,
            auth=True,
        )
        items = response.get("result", {}).get("list", [])
        if not items:
            return None
        item = items[0]
        avg_price = _to_float(item.get("avgPrice"))
        filled_qty = _to_float(item.get("cumExecQty"))
        return {
            "order_id": item.get("orderId", ""),
            "avg_price": avg_price,
            "filled_qty": filled_qty,
        }

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        query: str = "",
        body: str = "",
        auth: bool = False,
    ) -> dict:
        query_suffix = f"?{query}" if query else ""
        url = f"{path}{query_suffix}"
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        if auth:
            headers.update(self._build_auth_headers(query if method == "GET" else body))

        try:
            response = self._http.request(
                method,
                url,
                content=body.encode("utf-8") if method == "POST" else None,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            payload = exc.response.text
            logger.error("Bybit 요청 실패 [%s %s]: %s", method, path, payload)
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return {"retCode": exc.response.status_code, "retMsg": payload}
        except Exception as exc:
            logger.error("Bybit 요청 예외 [%s %s]: %s", method, path, exc)
            return {"retCode": -1, "retMsg": str(exc)}

    def _build_auth_headers(self, payload: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000) + self.timestamp_bias_ms)
        recv_window = str(self.recv_window)
        plain = f"{timestamp}{self.api_key}{recv_window}{payload}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            plain.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }

    def close(self):
        self._http.close()
        for executor in (
            self.cpp_ws_executor,
            self.ws_executor,
            self.fast_executor,
        ):
            close = getattr(executor, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _annotate_trade_timing(result: dict, trade_started_ns: int) -> dict:
        trade_finished_ns = time.monotonic_ns()
        result.setdefault("trade_started_monotonic_ns", int(trade_started_ns))
        result["trade_finished_monotonic_ns"] = int(trade_finished_ns)
        elapsed_ns = max(0, trade_finished_ns - trade_started_ns)
        result["trade_elapsed_ns"] = int(elapsed_ns)
        result["trade_elapsed_us"] = elapsed_ns / 1_000.0
        result["trade_elapsed_ms"] = elapsed_ns / 1_000_000.0
        return result
