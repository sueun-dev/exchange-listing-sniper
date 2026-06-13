from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = MODULE_DIR / "bin" / "trading_config_gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("trading_config_gate", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_config_passes_with_live_buy_settings(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "load_env_settings",
        lambda _keys: {
            "BYBIT_SPOT_BUY_ENABLED": "1",
            "BYBIT_API_KEY": "key",
            "BYBIT_API_SECRET": "secret",
            "BYBIT_SPOT_BUY_USDT_AMOUNT": "5",
            "BYBIT_SPOT_BUY_MODE": "quote",
        },
    )

    result = module.check_config()

    assert result["ok"] is True
    assert result["api_key_present"] is True
    assert result["api_secret_present"] is True
    assert result["amount_positive"] is True
    assert result["buy_mode"] == "quoteCoin"
    assert result["buy_mode_valid"] is True
    assert result["missing_or_invalid"] == []


def test_check_config_fails_without_secrets_or_amount(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "load_env_settings",
        lambda _keys: {
            "BYBIT_SPOT_BUY_ENABLED": "1",
            "BYBIT_SPOT_BUY_USDT_AMOUNT": "0",
        },
    )

    result = module.check_config()

    assert result["ok"] is False
    assert "BYBIT_API_KEY" in result["missing_or_invalid"]
    assert "BYBIT_API_SECRET" in result["missing_or_invalid"]
    assert "BYBIT_SPOT_BUY_USDT_AMOUNT" in result["missing_or_invalid"]


def test_check_config_fails_base_coin_buy_mode_for_usdt_amount(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "load_env_settings",
        lambda _keys: {
            "BYBIT_SPOT_BUY_ENABLED": "1",
            "BYBIT_API_KEY": "key",
            "BYBIT_API_SECRET": "secret",
            "BYBIT_SPOT_BUY_USDT_AMOUNT": "5",
            "BYBIT_SPOT_BUY_MODE": "baseCoin",
        },
    )

    result = module.check_config()

    assert result["ok"] is False
    assert result["buy_mode_valid"] is False
    assert "BYBIT_SPOT_BUY_MODE" in result["missing_or_invalid"]
