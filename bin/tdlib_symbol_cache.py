#!/usr/bin/env python3
from __future__ import annotations

"""Check or refresh the TDLib native Bybit spot-symbol cache."""

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

MODULE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_PATH = MODULE_DIR / "data" / "tdlib_bybit_spot_symbols.txt"
DEFAULT_BASE_URL = "https://api.bybit.com"
DEFAULT_MAX_AGE_SEC = 300
DEFAULT_MIN_SYMBOL_COUNT = 100


def _cache_path() -> Path:
    value = os.environ.get("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH")
    if not value:
        return DEFAULT_CACHE_PATH
    path = Path(value)
    return path if path.is_absolute() else MODULE_DIR / path


def _max_age_sec() -> int:
    value = os.environ.get("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MAX_AGE_SEC")
    if not value:
        return DEFAULT_MAX_AGE_SEC
    try:
        return int(value)
    except ValueError:
        return DEFAULT_MAX_AGE_SEC


def _min_symbol_count() -> int:
    value = os.environ.get("LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MIN_COUNT")
    if not value:
        return DEFAULT_MIN_SYMBOL_COUNT
    try:
        return max(1, int(value))
    except ValueError:
        return DEFAULT_MIN_SYMBOL_COUNT


def _read_cache(path: Path, max_age_sec: int, min_symbol_count: int = DEFAULT_MIN_SYMBOL_COUNT) -> dict:
    if not path.exists():
        return {
            "ok": False,
            "path": str(path),
            "reason": "cache_missing",
            "symbol_count": 0,
            "max_age_sec": max_age_sec,
            "min_symbol_count": min_symbol_count,
        }
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("# saved_unix_sec="):
        return {
            "ok": False,
            "path": str(path),
            "reason": "invalid_header",
            "symbol_count": 0,
            "max_age_sec": max_age_sec,
            "min_symbol_count": min_symbol_count,
        }
    try:
        saved_sec = int(lines[0].split("=", 1)[1])
    except ValueError:
        return {
            "ok": False,
            "path": str(path),
            "reason": "invalid_saved_unix_sec",
            "symbol_count": 0,
            "max_age_sec": max_age_sec,
            "min_symbol_count": min_symbol_count,
        }
    symbols = [line.strip() for line in lines[1:] if line.strip() and not line.startswith("#")]
    age_sec = max(0, int(time.time()) - saved_sec)
    if age_sec > max_age_sec:
        reason = "cache_stale"
    elif not symbols:
        reason = "cache_empty"
    elif len(symbols) < min_symbol_count:
        reason = "cache_too_small"
    else:
        reason = "ready"
    return {
        "ok": reason == "ready",
        "path": str(path),
        "reason": reason,
        "symbol_count": len(symbols),
        "age_sec": age_sec,
        "max_age_sec": max_age_sec,
        "min_symbol_count": min_symbol_count,
        "sample": symbols[:5],
    }


def _fetch_spot_symbols(base_url: str, timeout: float) -> list[str]:
    symbols: list[str] = []
    cursor = ""
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        while True:
            params = {"category": "spot", "limit": "1000"}
            if cursor:
                params["cursor"] = cursor
            path = "/v5/market/instruments-info?" + urllib.parse.urlencode(params)
            response = client.get(path)
            response.raise_for_status()
            body = response.json()
            if body.get("retCode") != 0:
                raise RuntimeError(f"Bybit retCode={body.get('retCode')} retMsg={body.get('retMsg')}")
            result = body.get("result") or {}
            for item in result.get("list") or []:
                symbol = item.get("symbol")
                if symbol:
                    symbols.append(str(symbol))
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
    return sorted(set(symbols))


def _write_cache(path: Path, symbols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        "# saved_unix_sec=" + str(int(time.time())) + "\n" + "\n".join(symbols) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "refresh"])
    parser.add_argument("--path", type=Path, default=None)
    parser.add_argument("--max-age-sec", type=int, default=None)
    parser.add_argument("--min-symbol-count", type=int, default=None)
    parser.add_argument("--base-url", default=os.environ.get("BYBIT_API_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    path = args.path or _cache_path()
    max_age_sec = args.max_age_sec if args.max_age_sec is not None else _max_age_sec()
    min_symbol_count = (
        max(1, args.min_symbol_count)
        if args.min_symbol_count is not None
        else _min_symbol_count()
    )

    if args.command == "check":
        result = _read_cache(path, max_age_sec, min_symbol_count)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2

    try:
        symbols = _fetch_spot_symbols(args.base_url, args.timeout)
        if not symbols:
            raise RuntimeError("empty symbol list")
        _write_cache(path, symbols)
        result = _read_cache(path, max_age_sec, min_symbol_count)
        result["refreshed"] = True
        result["base_url"] = args.base_url
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "path": str(path),
                    "reason": "refresh_failed",
                    "error": str(exc),
                    "base_url": args.base_url,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
