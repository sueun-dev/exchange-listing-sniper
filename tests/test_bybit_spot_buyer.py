from __future__ import annotations

import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
for module_name in list(sys.modules):
    if module_name == "src" or module_name.startswith("src."):
        sys.modules.pop(module_name, None)

from src import bybit_spot_buyer as bybit_spot_buyer_module  # noqa: E402
from src.bybit_spot_buyer import BybitSpotBuyer, _normalize_buy_mode  # noqa: E402


class _DisabledExecutor:
    def is_enabled(self):
        return False


class _DuplicateFastExecutor:
    def is_enabled(self):
        return True

    def warmup(self):
        return None

    def buy_market_quote_text(self, *, symbol, quote_amount_text, order_link_id):
        return {
            "attempted": True,
            "executed": False,
            "ret_code": 10014,
            "symbol": symbol,
            "reason": "duplicate orderLinkId",
            "order_link_id": order_link_id,
            "transport": "cpp_fast_path",
        }


class _FailingWarmupFastExecutor:
    def is_enabled(self):
        return True

    def warmup(self):
        return {"ok": False, "symbol_count": 0}


class _ShortBulkFastExecutor:
    def is_enabled(self):
        return True

    def warmup(self):
        return {"ok": True}

    def buy_market_quote_text(self, *, symbol, quote_amount_text, order_link_id):
        raise AssertionError("bulk path should use buy_markets_quote_text")

    def buy_markets_quote_text(self, *, orders, quote_amount_text):
        symbol, order_link_id = orders[0]
        return [
            {
                "attempted": True,
                "executed": True,
                "ret_code": 0,
                "symbol": symbol,
                "order_id": "first-order",
                "order_link_id": order_link_id,
                "transport": "cpp_fast_path",
            }
        ]


class _TimeoutThenAssertFastExecutor:
    """cpp transport that times out; any further transport use is a test failure."""

    def __init__(self):
        self.calls = 0

    def is_enabled(self):
        return True

    def warmup(self):
        return {"ok": True}

    def buy_market_quote_text(self, *, symbol, quote_amount_text, order_link_id):
        self.calls += 1
        return {
            "attempted": True,
            "executed": False,
            "ret_code": -1,
            "symbol": symbol,
            "reason": "Timeout was reached",
            "order_link_id": order_link_id,
            "transport": "cpp_fast_path",
        }


class _NoNetworkMarketClient:
    def is_cache_ready(self):
        raise AssertionError("market cache should not be checked")

    def has_symbol_cached(self, category, symbol):
        raise AssertionError("market cache should not be checked")

    def has_symbol(self, category, symbol):
        raise AssertionError("market API should not be called")


def _buyer(**kwargs) -> BybitSpotBuyer:
    fast_executor = kwargs.pop("fast_executor", _DisabledExecutor())
    cpp_ws_executor = kwargs.pop("cpp_ws_executor", _DisabledExecutor())
    ws_executor = kwargs.pop("ws_executor", _DisabledExecutor())
    return BybitSpotBuyer(
        fast_executor=fast_executor,
        cpp_ws_executor=cpp_ws_executor,
        ws_executor=ws_executor,
        market_client=_NoNetworkMarketClient(),
        **kwargs,
    )


class _AssertNotCalledWsExecutor:
    def is_enabled(self):
        return True

    def warmup(self):
        return None

    def buy_market(self, **kwargs):
        raise AssertionError("ws transport must not run after an ambiguous timeout")


def test_ambiguous_timeout_does_not_fall_back_to_next_transport():
    fast = _TimeoutThenAssertFastExecutor()
    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=True,
        buy_usdt_amount=10,
        fast_executor=fast,
        ws_executor=_AssertNotCalledWsExecutor(),
        order_transport_preference="cpp,ws",
    )

    result = buyer.buy_market(ticker="STRK", order_link_id="ls-test-timeout")

    assert fast.calls == 1
    assert result["executed"] is False
    assert result["reason"] == "Timeout was reached"
    assert result["transport"] == "cpp_fast_path"


def test_buy_market_disabled_never_attempts_network_or_executor():
    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=False,
        buy_usdt_amount=10,
    )

    result = buyer.buy_market(ticker="STRK", order_link_id="ls-test-1")

    assert result["enabled"] is False
    assert result["attempted"] is False
    assert result["executed"] is False
    assert result["reason"] == "buy_disabled"
    assert result["symbol"] == "STRKUSDT"


def test_buy_market_amount_above_ceiling_refuses_to_send_order():
    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=True,
        buy_usdt_amount=5000,
        max_usdt_amount=300,
    )

    result = buyer.buy_market(ticker="STRK", order_link_id="ls-test-cap")

    assert result["attempted"] is False
    assert result["executed"] is False
    assert result["reason"] == "quote_amount_exceeds_max"
    assert result["qty"] == 0.0


def test_buy_market_non_finite_amount_refuses_to_send_order():
    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=True,
        buy_usdt_amount=float("inf"),
        max_usdt_amount=300,
    )

    result = buyer.buy_market(ticker="STRK", order_link_id="ls-test-inf")

    assert result["attempted"] is False
    assert result["executed"] is False
    assert result["reason"] == "quote_amount_invalid"
    assert result["qty"] == 0.0


def test_buy_market_amount_at_or_below_ceiling_is_planned():
    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=True,
        buy_usdt_amount=300,
        max_usdt_amount=300,
    )

    assert buyer._market_buy_qty == 300.0
    assert buyer._market_buy_qty_str == "300"
    assert buyer._market_buy_reason == ""


def test_buy_market_enabled_without_credentials_stops_before_market_lookup():
    buyer = _buyer(
        api_key="",
        api_secret="",
        buy_enabled=True,
        buy_usdt_amount=10,
    )

    result = buyer.buy_market(ticker="STRK", order_link_id="ls-test-2")

    assert result["attempted"] is False
    assert result["executed"] is False
    assert result["reason"] == "missing_api_config"


def test_bulk_buy_disabled_returns_one_disabled_result_per_order():
    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=False,
        buy_usdt_amount=10,
    )

    results = buyer.buy_markets(
        [
            {"ticker": "SENT", "order_link_id": "ls-test-sent"},
            {"ticker": "ELSA", "order_link_id": "ls-test-elsa"},
        ]
    )

    assert [item["symbol"] for item in results] == ["SENTUSDT", "ELSAUSDT"]
    assert [item["reason"] for item in results] == ["buy_disabled", "buy_disabled"]
    assert all(item["attempted"] is False for item in results)


def test_cpp_only_bulk_buy_pads_missing_fast_executor_responses():
    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=True,
        buy_usdt_amount=10,
        fast_executor=_ShortBulkFastExecutor(),
        order_transport_preference="cpp",
    )

    results = buyer.buy_markets(
        [
            {"ticker": "SENT", "order_link_id": "ls-test-sent"},
            {"ticker": "ELSA", "order_link_id": "ls-test-elsa"},
        ]
    )

    assert [item["symbol"] for item in results] == ["SENTUSDT", "ELSAUSDT"]
    assert results[0]["executed"] is True
    assert results[0]["order_id"] == "first-order"
    assert results[1]["executed"] is False
    assert results[1]["reason"] == "cpp_fast_path_bulk_missing_response"


def test_cpp_only_duplicate_resolution_can_be_disabled_for_hot_path():
    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=True,
        buy_usdt_amount=10,
        fast_executor=_DuplicateFastExecutor(),
        order_transport_preference="cpp",
        resolve_duplicate_order_link_id=False,
    )
    buyer.query_order_by_link_id = lambda _order_link_id: (_ for _ in ()).throw(
        AssertionError("duplicate lookup should be skipped")
    )

    result = buyer.buy_market(ticker="STRK", order_link_id="ls-test-dup")

    assert result["attempted"] is True
    assert result["executed"] is False
    assert result["reason"] == "duplicate orderLinkId"


def test_query_spot_balance_parses_available_amount():
    buyer = _buyer(api_key="key", api_secret="secret", buy_enabled=False, buy_usdt_amount=10)
    buyer._request_json = lambda **kwargs: {
        "retCode": 0,
        "result": {"list": [{"coin": [
            {"coin": "USDT", "walletBalance": "50.5", "availableToWithdraw": "48.2"},
        ]}]},
    }

    result = buyer.query_spot_balance("USDT")

    assert result["ret_code"] == 0
    assert result["available"] == 48.2
    assert result["wallet"] == 50.5


def test_query_spot_balance_surfaces_auth_failure():
    buyer = _buyer(api_key="key", api_secret="secret", buy_enabled=False, buy_usdt_amount=10)
    buyer._request_json = lambda **kwargs: {"retCode": 10003, "retMsg": "api key invalid"}

    result = buyer.query_spot_balance("USDT")

    assert result["ret_code"] == 10003
    assert result["available"] is None


def test_close_tolerates_injected_executors_without_close_method():
    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=False,
        buy_usdt_amount=10,
    )

    buyer.close()


def test_required_fast_executor_warmup_fails_startup_when_not_ready():
    try:
        _buyer(
            api_key="key",
            api_secret="secret",
            buy_enabled=True,
            buy_usdt_amount=10,
            fast_executor=_FailingWarmupFastExecutor(),
            order_transport_preference="cpp",
            require_fast_executor_warmup=True,
        )
    except RuntimeError as exc:
        assert "C++ fast executor warmup is required" in str(exc)
    else:
        raise AssertionError("required C++ fast executor warmup should fail startup")


def test_rest_auth_timestamp_applies_configured_bias(monkeypatch):
    monkeypatch.setattr(bybit_spot_buyer_module.time, "time", lambda: 1000.0)
    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=True,
        buy_usdt_amount=10,
        timestamp_bias_ms=-50,
    )

    headers = buyer._build_auth_headers("{}")

    assert headers["X-BAPI-TIMESTAMP"] == "999950"


def test_buy_mode_quote_alias_normalizes_to_bybit_market_unit():
    assert _normalize_buy_mode("quote") == "quoteCoin"
    assert _normalize_buy_mode("quoteCoin") == "quoteCoin"
    assert _normalize_buy_mode("base") == "baseCoin"
    assert _normalize_buy_mode("baseCoin") == "baseCoin"

    buyer = _buyer(
        api_key="key",
        api_secret="secret",
        buy_enabled=False,
        buy_usdt_amount=10,
        buy_mode="quote",
    )

    assert buyer.buy_mode == "quoteCoin"


def _buyer_with_preference(pref: str) -> BybitSpotBuyer:
    buyer = BybitSpotBuyer.__new__(BybitSpotBuyer)
    buyer.order_transport_preference = pref
    return buyer


def test_documented_transport_preference_csv_is_honored():
    buyer = _buyer_with_preference("python_ws,cpp_ws,cpp_rest,python_rest")

    assert buyer._parse_transport_preference() == ("ws", "cpp_ws", "cpp")


def test_transport_preference_partial_csv_appends_missing_transports():
    buyer = _buyer_with_preference("python_ws,")

    assert buyer._parse_transport_preference() == ("ws", "cpp", "cpp_ws")
