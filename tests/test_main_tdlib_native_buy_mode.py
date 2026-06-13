from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from main import (  # noqa: E402
    _create_realtime_client,
    _env_int,
    _python_bybit_order_path_enabled,
    _tdlib_native_buy_exclusive,
    _tdlib_native_buy_parallel_race,
    _tdlib_native_buy_relay_active,
)


def _args(
    *,
    backend: str,
    ultra_buy: bool = True,
    no_trade: bool = False,
    source_only: bool = False,
):
    return SimpleNamespace(
        realtime_backend=backend,
        ultra_buy=ultra_buy,
        no_trade=no_trade,
        source_only=source_only,
    )


def test_tdlib_backend_is_exclusive_native_buy(monkeypatch):
    monkeypatch.delenv("LISTING_RACE_TDLIB_NATIVE_BUY_ENABLED", raising=False)
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ENABLED", "1")
    args = _args(backend="tdlib")

    assert _tdlib_native_buy_exclusive(args, realtime_mode=True)
    assert _tdlib_native_buy_relay_active(args, realtime_mode=True)
    assert not _tdlib_native_buy_parallel_race(args, realtime_mode=True)


def test_race_backend_can_opt_into_parallel_tdlib_native_buy(monkeypatch):
    monkeypatch.setenv("LISTING_RACE_TDLIB_NATIVE_BUY_ENABLED", "1")
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ENABLED", "1")
    args = _args(backend="race")

    assert not _tdlib_native_buy_exclusive(args, realtime_mode=True)
    assert _tdlib_native_buy_parallel_race(args, realtime_mode=True)
    assert _tdlib_native_buy_relay_active(args, realtime_mode=True)


def test_parallel_tdlib_native_buy_owns_order_path(monkeypatch):
    monkeypatch.setenv("LISTING_RACE_TDLIB_NATIVE_BUY_ENABLED", "1")
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ENABLED", "1")
    args = _args(backend="race")

    assert not _python_bybit_order_path_enabled(args, realtime_mode=True)


def test_race_without_tdlib_native_buy_keeps_python_order_path(monkeypatch):
    monkeypatch.delenv("LISTING_RACE_TDLIB_NATIVE_BUY_ENABLED", raising=False)
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ENABLED", "1")
    args = _args(backend="race")

    assert _python_bybit_order_path_enabled(args, realtime_mode=True)


def test_race_backend_keeps_parallel_tdlib_native_buy_off_by_default(monkeypatch):
    monkeypatch.delenv("LISTING_RACE_TDLIB_NATIVE_BUY_ENABLED", raising=False)
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ENABLED", "1")
    args = _args(backend="race")

    assert not _tdlib_native_buy_parallel_race(args, realtime_mode=True)
    assert not _tdlib_native_buy_relay_active(args, realtime_mode=True)


def test_tdlib_native_buy_disabled_for_no_trade(monkeypatch):
    monkeypatch.setenv("LISTING_RACE_TDLIB_NATIVE_BUY_ENABLED", "1")
    monkeypatch.setenv("LISTING_TDLIB_NATIVE_BUY_ENABLED", "1")

    assert not _tdlib_native_buy_relay_active(
        _args(backend="tdlib", no_trade=True),
        realtime_mode=True,
    )
    assert not _tdlib_native_buy_relay_active(
        _args(backend="race", no_trade=True),
        realtime_mode=True,
    )


def test_env_int_reads_valid_value(monkeypatch):
    monkeypatch.setenv("LISTING_RACE_MIN_READY_BACKENDS", "3")

    assert _env_int("LISTING_RACE_MIN_READY_BACKENDS", 2) == 3


def test_env_int_falls_back_on_invalid_value(monkeypatch):
    monkeypatch.setenv("LISTING_RACE_MIN_READY_BACKENDS", "bad")

    assert _env_int("LISTING_RACE_MIN_READY_BACKENDS", 2) == 2


def test_tdlib_client_creation_skips_race_and_telethon_imports():
    for module_name in list(sys.modules):
        if (
            module_name == "telethon"
            or module_name.startswith("telethon.")
            or module_name in {
                "src.race_realtime_client",
                "src.telegram_realtime_client",
                "src.pyrogram_realtime_client",
            }
        ):
            sys.modules.pop(module_name, None)

    client = _create_realtime_client("tdlib")

    assert client.__class__.__name__ == "TdlibRealtimeChannelClient"
    assert "src.race_realtime_client" not in sys.modules
    assert "src.telegram_realtime_client" not in sys.modules
    assert "src.pyrogram_realtime_client" not in sys.modules
    assert not any(
        module_name == "telethon" or module_name.startswith("telethon.")
        for module_name in sys.modules
    )
