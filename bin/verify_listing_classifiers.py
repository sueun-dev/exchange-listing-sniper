#!/usr/bin/env python3
"""Verify listing classifiers against the shared golden title fixture."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parents[1]
CASES_PATH = MODULE_DIR / "tests" / "fixtures" / "listing_title_cases.json"
DEFAULT_RELAY_PATH = MODULE_DIR / "bin" / "tdlib_json_relay"
DEFAULT_ULTRA_DYLIB = MODULE_DIR / "bin" / "liblisting_ultra_engine.dylib"
# Must match cpp/listing_ultra_engine.cpp: MAX_ULTRA_TICKERS and the market flag bits.
MAX_ULTRA_TICKERS = 16
_MARKET_FLAG_BITS = (("KRW", 1), ("BTC", 2), ("USDT", 4), ("ETH", 8))

sys.path.insert(0, str(MODULE_DIR))

from src.announcement_filter import (  # noqa: E402
    classify_listing_title_python,
    make_listing_title_classifier,
)

logging.getLogger("src.native_classifier").setLevel(logging.ERROR)


def _case_id(case: dict[str, Any]) -> str:
    return str(case["id"])


def _expected_subset(expected: dict[str, Any] | None) -> dict[str, Any] | None:
    if expected is None:
        return None
    return {
        "signal_type": expected["signal_type"],
        "ticker": expected["ticker"],
        "tickers": expected["tickers"],
        "asset_name": expected["asset_name"],
        "markets": expected["markets"],
    }


def _actual_subset(actual: dict[str, Any] | None) -> dict[str, Any] | None:
    if actual is None:
        return None
    return {
        "signal_type": actual.get("signal_type"),
        "ticker": actual.get("ticker"),
        "tickers": actual.get("tickers"),
        "asset_name": actual.get("asset_name"),
        "markets": actual.get("markets"),
    }


def _record_mismatch(
    failures: list[dict[str, Any]],
    *,
    classifier: str,
    case: dict[str, Any],
    actual: dict[str, Any] | None,
):
    failures.append(
        {
            "classifier": classifier,
            "case_id": _case_id(case),
            "title": case["title"],
            "expected": _expected_subset(case["expected"]),
            "actual": _actual_subset(actual),
        }
    )


def _verify_python(cases: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for case in cases:
        actual = classify_listing_title_python(
            exchange=case["exchange"],
            title=case["title"],
            display_name=case["exchange"],
        )
        if _actual_subset(actual) != _expected_subset(case["expected"]):
            _record_mismatch(
                failures,
                classifier="python",
                case=case,
                actual=actual,
            )
    return {"name": "python_classifier_fixture", "ok": not failures, "failures": failures}


def _verify_default(cases: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    classifiers: dict[str, Any] = {}
    for case in cases:
        exchange = str(case["exchange"])
        if exchange not in classifiers:
            classifiers[exchange] = make_listing_title_classifier(
                exchange=exchange,
                display_name=exchange,
            )
        actual = classifiers[exchange](case["title"])
        if _actual_subset(actual) != _expected_subset(case["expected"]):
            _record_mismatch(
                failures,
                classifier="default",
                case=case,
                actual=actual,
            )
    return {"name": "default_classifier_fixture", "ok": not failures, "failures": failures}


def _relay_actual(
    *,
    relay_path: Path,
    exchange: str,
    title: str,
    timeout: float,
) -> dict[str, Any] | None:
    completed = subprocess.run(
        [str(relay_path), "--classify-title", exchange, title],
        cwd=str(MODULE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "signal_type": None,
            "ticker": None,
            "tickers": None,
            "asset_name": None,
            "markets": None,
            "error": completed.stdout.strip(),
            "returncode": completed.returncode,
        }
    payload = json.loads(completed.stdout)
    if payload == {"matched": False}:
        return None
    return payload


def _verify_tdlib_relay(
    cases: list[dict[str, Any]],
    *,
    relay_path: Path,
    timeout: float,
    required: bool,
) -> dict[str, Any]:
    if not relay_path.exists():
        return {
            "name": "tdlib_relay_cli_fixture",
            "ok": not required,
            "skipped": True,
            "required": required,
            "reason": f"relay_missing:{relay_path}",
        }

    failures: list[dict[str, Any]] = []
    for case in cases:
        actual = _relay_actual(
            relay_path=relay_path,
            exchange=case["exchange"],
            title=case["title"],
            timeout=timeout,
        )
        if _actual_subset(actual) != _expected_subset(case["expected"]):
            _record_mismatch(
                failures,
                classifier="tdlib_relay_cli",
                case=case,
                actual=actual,
            )
    return {
        "name": "tdlib_relay_cli_fixture",
        "ok": not failures,
        "required": required,
        "failures": failures,
    }


class _UltraClassifyResult(ctypes.Structure):
    # Mirrors UltraClassifyResult in cpp/listing_ultra_engine.cpp (field order/types).
    _fields_ = [
        ("matched", ctypes.c_int),
        ("market_flags", ctypes.c_uint32),
        ("ticker_count", ctypes.c_int),
        ("signal_type", ctypes.c_char * 16),
        ("ticker", ctypes.c_char * 16),
        ("asset_name", ctypes.c_char * 128),
        ("tickers", (ctypes.c_char * 16) * MAX_ULTRA_TICKERS),
    ]


def _load_ultra_classifier(dylib_path: Path) -> Any:
    lib = ctypes.CDLL(str(dylib_path))
    lib.ultra_classify_title.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.POINTER(_UltraClassifyResult),
    ]
    lib.ultra_classify_title.restype = ctypes.c_int
    return lib


def _ultra_actual(lib: Any, *, exchange: str, title: str) -> dict[str, Any] | None:
    res = _UltraClassifyResult()
    rc = lib.ultra_classify_title(
        exchange.encode("utf-8"), title.encode("utf-8"), ctypes.byref(res)
    )
    if rc != 1:
        return None
    tickers = [
        bytes(res.tickers[i]).split(b"\x00", 1)[0].decode("utf-8")
        for i in range(res.ticker_count)
    ]
    markets = [name for name, bit in _MARKET_FLAG_BITS if res.market_flags & bit]
    return {
        "signal_type": res.signal_type.decode("utf-8"),
        "ticker": res.ticker.decode("utf-8"),
        "tickers": tickers,
        "asset_name": res.asset_name.decode("utf-8"),
        "markets": markets,
    }


def _verify_ultra_engine(
    cases: list[dict[str, Any]],
    *,
    dylib_path: Path,
    required: bool,
) -> dict[str, Any]:
    if not dylib_path.exists():
        return {
            "name": "ultra_engine_fixture",
            "ok": not required,
            "skipped": True,
            "required": required,
            "reason": f"ultra_dylib_missing:{dylib_path}",
        }

    lib = _load_ultra_classifier(dylib_path)
    failures: list[dict[str, Any]] = []
    for case in cases:
        actual = _ultra_actual(lib, exchange=case["exchange"], title=case["title"])
        if _actual_subset(actual) != _expected_subset(case["expected"]):
            _record_mismatch(
                failures,
                classifier="ultra_engine",
                case=case,
                actual=actual,
            )
    return {
        "name": "ultra_engine_fixture",
        "ok": not failures,
        "required": required,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=CASES_PATH)
    parser.add_argument("--relay-path", type=Path, default=DEFAULT_RELAY_PATH)
    parser.add_argument("--require-tdlib-relay", action="store_true")
    parser.add_argument("--ultra-dylib-path", type=Path, default=DEFAULT_ULTRA_DYLIB)
    parser.add_argument("--require-ultra-engine", action="store_true")
    parser.add_argument("--skip-default", action="store_true")
    parser.add_argument("--skip-tdlib-relay", action="store_true")
    parser.add_argument("--skip-ultra-engine", action="store_true")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    cases = json.loads(args.fixture.read_text(encoding="utf-8"))
    steps = [_verify_python(cases)]
    if not args.skip_default:
        steps.append(_verify_default(cases))
    if not args.skip_tdlib_relay:
        steps.append(
            _verify_tdlib_relay(
                cases,
                relay_path=args.relay_path,
                timeout=args.timeout,
                required=args.require_tdlib_relay,
            )
        )
    if not args.skip_ultra_engine:
        steps.append(
            _verify_ultra_engine(
                cases,
                dylib_path=args.ultra_dylib_path,
                required=args.require_ultra_engine,
            )
        )

    ok = all(step["ok"] for step in steps)
    print(
        json.dumps(
            {
                "ok": ok,
                "mode": "verify_listing_classifiers",
                "fixture": str(args.fixture),
                "steps": steps,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
