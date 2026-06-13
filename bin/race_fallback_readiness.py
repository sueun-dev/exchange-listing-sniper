#!/usr/bin/env python3
"""Check the Telethon/Pyrogram race fallback C++ buy path before live watch."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))


def _set_race_defaults() -> None:
    os.environ.setdefault("BYBIT_FAST_EXECUTOR_ENABLED", "1")
    os.environ.setdefault("BYBIT_FAST_EXECUTOR_AUTO_BUILD", "1")
    os.environ.setdefault("BYBIT_REQUIRE_FAST_EXECUTOR_WARMUP", "1")
    os.environ.setdefault("BYBIT_WS_ORDER_ENABLED", "0")
    os.environ.setdefault("BYBIT_CPP_WS_EXECUTOR_ENABLED", "0")
    os.environ.setdefault("BYBIT_ORDER_TRANSPORT_PREFERENCE", "cpp")
    os.environ.setdefault("BYBIT_PREFER_CACHED_SYMBOL_CHECK", "1")
    os.environ.setdefault("BYBIT_FAST_ORDER_ON_CACHE_MISS", "0")
    os.environ.setdefault("LISTING_CPP_ULTRA_ENGINE_ENABLED", "1")
    os.environ.setdefault("LISTING_CPP_ULTRA_REQUIRE_WARMUP", "1")
    os.environ.setdefault("LISTING_CPP_ULTRA_ORDER_ON_CACHE_MISS", "0")


def check_readiness() -> dict:
    _set_race_defaults()
    steps = []

    from src.cpp_ultra_engine import CppUltraListingEngineBridge  # noqa: WPS433

    ultra = CppUltraListingEngineBridge(enabled=True)
    ultra_step = {
        "name": "cpp_ultra_warmup",
        "ok": False,
        "enabled": ultra.is_enabled(),
    }
    if ultra.is_enabled():
        try:
            warmup_result = ultra.warmup()
            ultra_step["result"] = warmup_result
            ultra_step["ok"] = bool(
                isinstance(warmup_result, dict)
                and warmup_result.get("ok")
            )
            if not ultra_step["ok"]:
                ultra_step["reason"] = "cpp_ultra_warmup_not_ready"
        except Exception as exc:
            ultra_step["reason"] = "cpp_ultra_warmup_failed"
            ultra_step["error"] = str(exc)
    else:
        ultra_step["reason"] = "cpp_ultra_disabled_or_missing"
    steps.append(ultra_step)

    from src.bybit_spot_buyer import BybitSpotBuyer  # noqa: WPS433

    buyer = None
    fast_step = {
        "name": "cpp_fast_executor_warmup",
        "ok": False,
    }
    try:
        buyer = BybitSpotBuyer(require_fast_executor_warmup=True)
        transports = tuple(getattr(buyer, "_transport_order", ()))
        fast_step["transport_order"] = list(transports)
        fast_step["ok"] = transports == ("cpp",)
        if not fast_step["ok"]:
            fast_step["reason"] = "cpp_fast_transport_not_exclusive"
    except Exception as exc:
        fast_step["reason"] = "cpp_fast_executor_warmup_failed"
        fast_step["error"] = str(exc)
    finally:
        if buyer is not None:
            try:
                buyer.close()
            except Exception:
                pass
    steps.append(fast_step)

    ok = all(step.get("ok") for step in steps)
    return {
        "ok": ok,
        "mode": "race_fallback_readiness",
        "steps": steps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check"])
    parser.parse_args()

    result = check_readiness()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
