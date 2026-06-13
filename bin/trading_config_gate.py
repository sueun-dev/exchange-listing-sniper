#!/usr/bin/env python3
from __future__ import annotations

"""Fail fast when live trading config cannot actually place a Bybit spot buy."""

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from src.env_loader import load_env_settings  # noqa: E402

VALID_QUOTE_BUY_MODES = {"", "quote", "quotecoin"}
CANONICAL_QUOTE_BUY_MODE = "quoteCoin"


def _is_truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_decimal(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return Decimal(value.strip()) > 0
    except (AttributeError, InvalidOperation):
        return False


def _normalize_buy_mode(value: str | None) -> str:
    return (value or "").strip().lower()


def check_config() -> dict:
    settings = load_env_settings(
        {
            "BYBIT_API_KEY",
            "BYBIT_API_SECRET",
            "BYBIT_SPOT_BUY_ENABLED",
            "BYBIT_SPOT_BUY_USDT_AMOUNT",
            "BYBIT_SPOT_BUY_MODE",
        }
    )
    buy_enabled = _is_truthy(settings.get("BYBIT_SPOT_BUY_ENABLED"))
    api_key_present = bool(settings.get("BYBIT_API_KEY"))
    api_secret_present = bool(settings.get("BYBIT_API_SECRET"))
    amount_positive = _positive_decimal(settings.get("BYBIT_SPOT_BUY_USDT_AMOUNT"))
    buy_mode = _normalize_buy_mode(settings.get("BYBIT_SPOT_BUY_MODE"))
    buy_mode_valid = buy_mode in VALID_QUOTE_BUY_MODES
    missing = []
    if not buy_enabled:
        missing.append("BYBIT_SPOT_BUY_ENABLED")
    if not api_key_present:
        missing.append("BYBIT_API_KEY")
    if not api_secret_present:
        missing.append("BYBIT_API_SECRET")
    if not amount_positive:
        missing.append("BYBIT_SPOT_BUY_USDT_AMOUNT")
    if not buy_mode_valid:
        missing.append("BYBIT_SPOT_BUY_MODE")
    return {
        "ok": not missing,
        "mode": "trading_config_gate",
        "buy_enabled": buy_enabled,
        "api_key_present": api_key_present,
        "api_secret_present": api_secret_present,
        "amount_positive": amount_positive,
        "buy_mode": CANONICAL_QUOTE_BUY_MODE if buy_mode_valid else buy_mode,
        "buy_mode_valid": buy_mode_valid,
        "missing_or_invalid": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check"])
    parser.parse_args()

    result = check_config()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
