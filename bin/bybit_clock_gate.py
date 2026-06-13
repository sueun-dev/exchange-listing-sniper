#!/usr/bin/env python3
from __future__ import annotations

"""Fail fast when local clock skew can make Bybit signed orders invalid."""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable

import httpx

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from src.env_loader import load_env_settings  # noqa: E402

DEFAULT_BASE_URL = "https://api.bybit.com"
DEFAULT_RECV_WINDOW_MS = 5000
DEFAULT_TIMESTAMP_BIAS_MS = -50
DEFAULT_MAX_CLOCK_SKEW_MS = 1000.0
DEFAULT_AHEAD_MARGIN_MS = 100.0
DEFAULT_MAX_RTT_MS = 1000.0
DEFAULT_TIMEOUT_SEC = 2.0


def _to_int(value: str | int | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_float(value: str | float | int | None, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _server_time_ms_from_payload(payload: dict) -> float | None:
    result = payload.get("result")
    if isinstance(result, dict):
        time_nano = result.get("timeNano")
        if time_nano is not None:
            try:
                return int(str(time_nano)) / 1_000_000.0
            except ValueError:
                pass

        time_second = result.get("timeSecond")
        if time_second is not None:
            try:
                return int(str(time_second)) * 1000.0
            except ValueError:
                pass

    top_level_time = payload.get("time")
    if top_level_time is not None:
        try:
            return float(top_level_time)
        except (TypeError, ValueError):
            return None
    return None


def _evaluate_clock(
    *,
    server_time_ms: float,
    local_midpoint_ms: float,
    timestamp_bias_ms: int,
    recv_window_ms: int,
    max_clock_skew_ms: float,
    ahead_margin_ms: float,
) -> dict:
    raw_skew_ms = local_midpoint_ms - server_time_ms
    signed_timestamp_ms = local_midpoint_ms + timestamp_bias_ms
    skew_ms = signed_timestamp_ms - server_time_ms
    max_ahead_ms = 1000.0 - ahead_margin_ms
    within_bybit_window = -float(recv_window_ms) <= skew_ms < max_ahead_ms
    within_max_skew = abs(skew_ms) <= max_clock_skew_ms
    return {
        "raw_skew_ms": raw_skew_ms,
        "signed_timestamp_ms": signed_timestamp_ms,
        "skew_ms": skew_ms,
        "abs_skew_ms": abs(skew_ms),
        "max_ahead_ms": max_ahead_ms,
        "within_bybit_window": within_bybit_window,
        "within_max_skew": within_max_skew,
        "ok": within_bybit_window and within_max_skew,
    }


def _fetch_bybit_time(
    *,
    base_url: str,
    timeout_sec: float,
    time_fn: Callable[[], float] = time.time,
) -> dict:
    endpoint = base_url.rstrip("/") + "/v5/market/time"
    start_ms = time_fn() * 1000.0
    with httpx.Client(timeout=timeout_sec) as client:
        response = client.get(endpoint)
    end_ms = time_fn() * 1000.0
    response.raise_for_status()
    return {
        "endpoint": endpoint,
        "http_status": response.status_code,
        "payload": response.json(),
        "local_start_ms": start_ms,
        "local_end_ms": end_ms,
    }


def check_clock(
    fetch_fn: Callable[..., dict] | None = None,
    *,
    time_fn: Callable[[], float] = time.time,
) -> dict:
    settings = load_env_settings(
        {
            "BYBIT_API_BASE_URL",
            "BYBIT_RECV_WINDOW",
            "BYBIT_TIMESTAMP_BIAS_MS",
            "BYBIT_MAX_CLOCK_SKEW_MS",
            "BYBIT_CLOCK_AHEAD_MARGIN_MS",
            "BYBIT_CLOCK_MAX_RTT_MS",
            "BYBIT_CLOCK_GATE_TIMEOUT_SEC",
        }
    )
    base_url = settings.get("BYBIT_API_BASE_URL") or DEFAULT_BASE_URL
    recv_window_ms = _to_int(settings.get("BYBIT_RECV_WINDOW"), DEFAULT_RECV_WINDOW_MS)
    timestamp_bias_ms = _to_int(
        settings.get("BYBIT_TIMESTAMP_BIAS_MS"),
        DEFAULT_TIMESTAMP_BIAS_MS,
    )
    max_clock_skew_ms = _to_float(
        settings.get("BYBIT_MAX_CLOCK_SKEW_MS"),
        DEFAULT_MAX_CLOCK_SKEW_MS,
    )
    ahead_margin_ms = _to_float(
        settings.get("BYBIT_CLOCK_AHEAD_MARGIN_MS"),
        DEFAULT_AHEAD_MARGIN_MS,
    )
    max_rtt_ms = _to_float(
        settings.get("BYBIT_CLOCK_MAX_RTT_MS"),
        DEFAULT_MAX_RTT_MS,
    )
    timeout_sec = _to_float(
        settings.get("BYBIT_CLOCK_GATE_TIMEOUT_SEC"),
        DEFAULT_TIMEOUT_SEC,
    )

    fetch = fetch_fn or _fetch_bybit_time
    try:
        fetched = fetch(
            base_url=base_url,
            timeout_sec=timeout_sec,
            time_fn=time_fn,
        )
    except Exception as exc:
        return {
            "ok": False,
            "mode": "bybit_clock_gate",
            "base_url": base_url.rstrip("/"),
            "recv_window_ms": recv_window_ms,
            "timestamp_bias_ms": timestamp_bias_ms,
            "max_clock_skew_ms": max_clock_skew_ms,
            "ahead_margin_ms": ahead_margin_ms,
            "max_rtt_ms": max_rtt_ms,
            "reason": "bybit_time_request_failed",
            "error": str(exc),
        }

    payload = fetched.get("payload")
    if isinstance(payload, dict) and payload.get("retCode") not in (None, 0):
        return {
            "ok": False,
            "mode": "bybit_clock_gate",
            "base_url": base_url.rstrip("/"),
            "recv_window_ms": recv_window_ms,
            "timestamp_bias_ms": timestamp_bias_ms,
            "max_clock_skew_ms": max_clock_skew_ms,
            "ahead_margin_ms": ahead_margin_ms,
            "max_rtt_ms": max_rtt_ms,
            "endpoint": fetched.get("endpoint"),
            "http_status": fetched.get("http_status"),
            "ret_code": payload.get("retCode"),
            "ret_msg": payload.get("retMsg"),
            "reason": "bybit_time_ret_code_not_ok",
        }

    server_time_ms = (
        _server_time_ms_from_payload(payload)
        if isinstance(payload, dict)
        else None
    )
    if server_time_ms is None:
        return {
            "ok": False,
            "mode": "bybit_clock_gate",
            "base_url": base_url.rstrip("/"),
            "recv_window_ms": recv_window_ms,
            "timestamp_bias_ms": timestamp_bias_ms,
            "max_clock_skew_ms": max_clock_skew_ms,
            "ahead_margin_ms": ahead_margin_ms,
            "max_rtt_ms": max_rtt_ms,
            "endpoint": fetched.get("endpoint"),
            "http_status": fetched.get("http_status"),
            "reason": "bybit_time_response_unparseable",
        }

    local_start_ms = float(fetched["local_start_ms"])
    local_end_ms = float(fetched["local_end_ms"])
    local_midpoint_ms = (local_start_ms + local_end_ms) / 2.0
    evaluation = _evaluate_clock(
        server_time_ms=server_time_ms,
        local_midpoint_ms=local_midpoint_ms,
        timestamp_bias_ms=timestamp_bias_ms,
        recv_window_ms=recv_window_ms,
        max_clock_skew_ms=max_clock_skew_ms,
        ahead_margin_ms=ahead_margin_ms,
    )
    request_rtt_ms = max(0.0, local_end_ms - local_start_ms)
    rtt_ok = max_rtt_ms <= 0 or request_rtt_ms <= max_rtt_ms
    ok = evaluation["ok"] and rtt_ok
    if not evaluation["ok"]:
        reason = "clock_skew_out_of_bounds"
    elif not rtt_ok:
        reason = "bybit_time_rtt_threshold_exceeded"
    else:
        reason = "ready"
    return {
        **evaluation,
        "ok": ok,
        "mode": "bybit_clock_gate",
        "base_url": base_url.rstrip("/"),
        "endpoint": fetched.get("endpoint"),
        "http_status": fetched.get("http_status"),
        "recv_window_ms": recv_window_ms,
        "timestamp_bias_ms": timestamp_bias_ms,
        "max_clock_skew_ms": max_clock_skew_ms,
        "ahead_margin_ms": ahead_margin_ms,
        "max_rtt_ms": max_rtt_ms,
        "server_time_ms": server_time_ms,
        "local_midpoint_ms": local_midpoint_ms,
        "request_rtt_ms": request_rtt_ms,
        "rtt_ok": rtt_ok,
        "reason": reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check"])
    parser.parse_args()

    result = check_clock()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
