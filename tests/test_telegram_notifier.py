from __future__ import annotations

import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
for module_name in list(sys.modules):
    if module_name == "src" or module_name.startswith("src."):
        sys.modules.pop(module_name, None)

from src.telegram_notifier import format_listing_signal  # noqa: E402


def _signal(trade: dict) -> dict:
    return {
        "exchange_name": "빗썸",
        "asset_name": "스타크넷",
        "ticker": "STRK",
        "markets": ["KRW"],
        "title": "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가",
        "trade": trade,
    }


def test_executed_trade_renders_executed():
    text = format_listing_signal(_signal({"attempted": True, "executed": True}))
    assert "자동매수: <b>EXECUTED" in text


def test_async_dispatched_trade_is_not_reported_as_failed():
    # The dangerous [2] case: an order that was SENT (and likely filled) via async
    # native dispatch must not read as FAILED, or the operator re-buys.
    text = format_listing_signal(
        _signal(
            {
                "attempted": True,
                "executed": False,
                "reason": "tdlib_native_rest_dispatched",
            }
        )
    )
    assert "FAILED" not in text
    assert "DISPATCHED" in text
    assert "체결 확인" in text


def test_genuinely_failed_trade_still_reports_failed():
    text = format_listing_signal(
        _signal(
            {
                "attempted": True,
                "executed": False,
                "reason": "insufficient_balance",
            }
        )
    )
    assert "FAILED (insufficient_balance)" in text


def test_disabled_trade_renders_skip():
    text = format_listing_signal(
        _signal({"attempted": False, "executed": False, "reason": "cli_disabled"})
    )
    assert "SKIP (cli_disabled)" in text
