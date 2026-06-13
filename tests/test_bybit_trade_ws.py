from __future__ import annotations

import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
for module_name in list(sys.modules):
    if module_name == "src" or module_name.startswith("src."):
        sys.modules.pop(module_name, None)

from src.bybit_trade_ws import BybitTradeWebSocketExecutor  # noqa: E402


def test_ws_order_timestamp_applies_configured_bias():
    captured = {}
    executor = BybitTradeWebSocketExecutor(
        api_key="key",
        api_secret="secret",
        ws_url="wss://example.test/v5/trade",
        enabled=True,
        ws_factory=lambda *_args, **_kwargs: object(),
        time_fn=lambda: 1000.0,
        timestamp_bias_ms=-50,
    )
    executor._ensure_ready_locked = lambda: None

    def fake_send_request_locked(*, payload, matcher):
        captured["payload"] = payload
        return {
            "retCode": 0,
            "retMsg": "OK",
            "data": {
                "orderId": "order-1",
                "orderLinkId": "ls-test",
            },
        }

    executor._send_request_locked = fake_send_request_locked

    result = executor.create_market_order(
        symbol="VVVUSDT",
        side="Buy",
        qty="5",
        market_unit="quoteCoin",
        order_link_id="ls-test",
    )

    assert result["executed"] is True
    assert captured["payload"]["header"]["X-BAPI-TIMESTAMP"] == "999950"
