#!/usr/bin/env python3
from __future__ import annotations

"""Run the fastest TDLib native-buy readiness checks as one JSON gate."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MAX_NATIVE_BUY_P50_US = 5.0
DEFAULT_MAX_TDLIB_MESSAGE_P50_US = 10.0
DEFAULT_MAX_TDLIB_MESSAGE_LONG_BODY_P50_US = 10.0
DEFAULT_MAX_NATIVE_BUY_MULTI_P50_US = 10.0
DEFAULT_MAX_TDLIB_MESSAGE_MULTI_P50_US = 15.0
DEFAULT_MAX_TDLIB_FIRE_RECEIVE_RETURN_P50_US = 5.0
DEFAULT_MAX_TDLIB_FIRE_ORDER_SEND_P50_US = 15.0
DEFAULT_MAX_TDLIB_TYPE_FILTER_FAST_P50_NS = 100.0
DEFAULT_MIN_TDLIB_TYPE_FILTER_SPEEDUP = 3.0
DEFAULT_MAX_LIVE_ORDER_SEND_US = 1_000.0
DEFAULT_MAX_LIVE_TRADE_FINISHED_US = 1_000.0

BENCH_RE = re.compile(
    r"^(?P<name>[A-Z0-9_]+)\s+iterations=(?P<iterations>\d+)"
    r".*?\bp50_us=(?P<p50_us>[0-9.]+)"
    r".*?\bp95_us=(?P<p95_us>[0-9.]+)"
    r".*?\bavg_us=(?P<avg_us>[0-9.]+)"
)


def _truthy_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
TYPE_FILTER_RE = re.compile(
    r"^BENCHMARK_TDLIB_TYPE_FILTER\s+iterations=(?P<iterations>\d+)"
    r"\s+ops_per_sample=(?P<ops_per_sample>\d+)"
    r"\s+legacy_p50_ns=(?P<legacy_p50_ns>[0-9.]+)"
    r"\s+legacy_p95_ns=(?P<legacy_p95_ns>[0-9.]+)"
    r"\s+legacy_avg_ns=(?P<legacy_avg_ns>[0-9.]+)"
    r"\s+fast_p50_ns=(?P<fast_p50_ns>[0-9.]+)"
    r"\s+fast_p95_ns=(?P<fast_p95_ns>[0-9.]+)"
    r"\s+fast_avg_ns=(?P<fast_avg_ns>[0-9.]+)"
    r"(?:\s+live_cstr_p50_ns=(?P<live_cstr_p50_ns>[0-9.]+)"
    r"\s+live_cstr_p95_ns=(?P<live_cstr_p95_ns>[0-9.]+)"
    r"\s+live_cstr_avg_ns=(?P<live_cstr_avg_ns>[0-9.]+))?"
)

TDLIB_FIRE_RE = re.compile(
    r"^(?P<name>BENCHMARK_TDLIB_MESSAGE_FIRE_AND_FORGET(?:_MULTI)?)"
    r"\s+iterations=(?P<iterations>\d+)"
    r"\s+receive_return_p50_us=(?P<receive_return_p50_us>[0-9.]+)"
    r"\s+receive_return_p95_us=(?P<receive_return_p95_us>[0-9.]+)"
    r"\s+receive_return_avg_us=(?P<receive_return_avg_us>[0-9.]+)"
    r"\s+order_send_started_p50_us=(?P<order_send_started_p50_us>[0-9.]+)"
    r"\s+order_send_started_p95_us=(?P<order_send_started_p95_us>[0-9.]+)"
    r"\s+order_send_started_avg_us=(?P<order_send_started_avg_us>[0-9.]+)"
)


def _run(cmd: list[str], timeout: float) -> dict:
    completed = subprocess.run(
        cmd,
        cwd=str(MODULE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    return {
        "cmd": cmd,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
    }


def _json_from_stdout(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            return None
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            return None


def _benchmark_from_stdout(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        match = BENCH_RE.search(line.strip())
        if match is None:
            continue
        return {
            "name": match.group("name"),
            "iterations": int(match.group("iterations")),
            "p50_us": float(match.group("p50_us")),
            "p95_us": float(match.group("p95_us")),
            "avg_us": float(match.group("avg_us")),
        }
    return None


def _type_filter_benchmark_from_stdout(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        match = TYPE_FILTER_RE.search(line.strip())
        if match is None:
            continue
        benchmark = {
            "name": "BENCHMARK_TDLIB_TYPE_FILTER",
            "iterations": int(match.group("iterations")),
            "ops_per_sample": int(match.group("ops_per_sample")),
            "legacy_p50_ns": float(match.group("legacy_p50_ns")),
            "legacy_p95_ns": float(match.group("legacy_p95_ns")),
            "legacy_avg_ns": float(match.group("legacy_avg_ns")),
            "fast_p50_ns": float(match.group("fast_p50_ns")),
            "fast_p95_ns": float(match.group("fast_p95_ns")),
            "fast_avg_ns": float(match.group("fast_avg_ns")),
        }
        if match.group("live_cstr_p50_ns") is not None:
            benchmark.update(
                {
                    "live_cstr_p50_ns": float(match.group("live_cstr_p50_ns")),
                    "live_cstr_p95_ns": float(match.group("live_cstr_p95_ns")),
                    "live_cstr_avg_ns": float(match.group("live_cstr_avg_ns")),
                }
            )
        return benchmark
    return None


def _tdlib_fire_benchmark_from_stdout(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        match = TDLIB_FIRE_RE.search(line.strip())
        if match is None:
            continue
        return {
            "name": match.group("name"),
            "iterations": int(match.group("iterations")),
            "receive_return_p50_us": float(match.group("receive_return_p50_us")),
            "receive_return_p95_us": float(match.group("receive_return_p95_us")),
            "receive_return_avg_us": float(match.group("receive_return_avg_us")),
            "order_send_started_p50_us": float(match.group("order_send_started_p50_us")),
            "order_send_started_p95_us": float(match.group("order_send_started_p95_us")),
            "order_send_started_avg_us": float(match.group("order_send_started_avg_us")),
        }
    return None


def _trim_stdout(stdout: str, limit: int = 2000) -> str:
    text = stdout.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _step(name: str, cmd: list[str], timeout: float, required: bool = True) -> dict:
    try:
        raw = _run(cmd, timeout)
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "required": required,
            "ok": False,
            "returncode": None,
            "reason": "timeout",
            "cmd": cmd,
            "stdout": _trim_stdout(exc.stdout or ""),
        }
    parsed_json = _json_from_stdout(raw["stdout"])
    benchmark = _benchmark_from_stdout(raw["stdout"])
    type_filter_benchmark = _type_filter_benchmark_from_stdout(raw["stdout"])
    tdlib_fire_benchmark = _tdlib_fire_benchmark_from_stdout(raw["stdout"])
    ok = raw["returncode"] == 0
    result = {
        "name": name,
        "required": required,
        "ok": ok,
        "returncode": raw["returncode"],
        "cmd": cmd,
    }
    if parsed_json is not None:
        result["json"] = parsed_json
    if benchmark is not None:
        result["benchmark"] = benchmark
    if type_filter_benchmark is not None:
        result["type_filter_benchmark"] = type_filter_benchmark
    if tdlib_fire_benchmark is not None:
        result["tdlib_fire_benchmark"] = tdlib_fire_benchmark
    if not ok or (
        parsed_json is None
        and benchmark is None
        and type_filter_benchmark is None
        and tdlib_fire_benchmark is None
    ):
        result["stdout"] = _trim_stdout(raw["stdout"])
    return result


def _apply_benchmark_threshold(step: dict, *, max_p50_us: float) -> dict:
    benchmark = step.get("benchmark")
    if not step.get("ok") or not isinstance(benchmark, dict):
        return step
    p50_us = float(benchmark.get("p50_us", float("inf")))
    step["max_p50_us"] = max_p50_us
    if p50_us > max_p50_us:
        step["ok"] = False
        step["reason"] = "benchmark_p50_threshold_exceeded"
    return step


def _apply_type_filter_threshold(
    step: dict,
    *,
    max_fast_p50_ns: float,
    min_speedup: float,
) -> dict:
    benchmark = step.get("type_filter_benchmark")
    if not step.get("ok") or not isinstance(benchmark, dict):
        return step
    fast_p50_ns = float(benchmark.get("fast_p50_ns", float("inf")))
    legacy_p50_ns = float(benchmark.get("legacy_p50_ns", 0.0))
    speedup = legacy_p50_ns / fast_p50_ns if fast_p50_ns > 0 else float("inf")
    step["max_fast_p50_ns"] = max_fast_p50_ns
    step["min_speedup"] = min_speedup
    step["observed_speedup"] = speedup
    if fast_p50_ns > max_fast_p50_ns:
        step["ok"] = False
        step["reason"] = "tdlib_type_filter_fast_p50_threshold_exceeded"
    elif speedup < min_speedup:
        step["ok"] = False
        step["reason"] = "tdlib_type_filter_speedup_threshold_missed"
    return step


def _apply_tdlib_fire_threshold(
    step: dict,
    *,
    max_receive_return_p50_us: float,
    max_order_send_p50_us: float,
) -> dict:
    benchmark = step.get("tdlib_fire_benchmark")
    if not step.get("ok") or not isinstance(benchmark, dict):
        return step
    receive_return_p50_us = float(benchmark.get("receive_return_p50_us", float("inf")))
    order_send_p50_us = float(benchmark.get("order_send_started_p50_us", float("inf")))
    step["max_receive_return_p50_us"] = max_receive_return_p50_us
    step["max_order_send_started_p50_us"] = max_order_send_p50_us
    if receive_return_p50_us > max_receive_return_p50_us:
        step["ok"] = False
        step["reason"] = "tdlib_fire_receive_return_p50_threshold_exceeded"
    elif order_send_p50_us > max_order_send_p50_us:
        step["ok"] = False
        step["reason"] = "tdlib_fire_order_send_p50_threshold_exceeded"
    return step


def _max_receive_to_order_send_us(payload: dict) -> float | None:
    summary = payload.get("receive_to_last_order_send_started_us_summary")
    if isinstance(summary, dict):
        try:
            return float(summary["max_us"])
        except (KeyError, TypeError, ValueError):
            pass

    events = payload.get("events")
    if not isinstance(events, list):
        return None
    values = []
    for event in events:
        if not isinstance(event, dict):
            continue
        value = event.get("receive_to_last_order_send_started_us")
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return max(values) if values else None


def _max_receive_to_trade_finished_us(payload: dict) -> float | None:
    summary = payload.get("receive_to_last_trade_finished_us_summary")
    if isinstance(summary, dict):
        try:
            return float(summary["max_us"])
        except (KeyError, TypeError, ValueError):
            pass

    events = payload.get("events")
    if not isinstance(events, list):
        return None
    values = []
    for event in events:
        if not isinstance(event, dict):
            continue
        value = event.get("receive_to_last_trade_finished_us")
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return max(values) if values else None


def _apply_live_inject_threshold(step: dict, *, max_order_send_us: float) -> dict:
    payload = step.get("json")
    if not step.get("ok") or not isinstance(payload, dict):
        return step
    if payload.get("mode_detail") == "emit_off_fire_and_forget_dispatch":
        step["live_inject_order_send_threshold_skipped"] = (
            "emit-off fire-and-forget returns before order_send_started; "
            "tdlib_message_fire_and_forget_benchmark covers order send timing"
        )
        return step
    summary = payload.get("receive_to_last_order_send_started_us_summary")
    if isinstance(summary, dict):
        step["observed_receive_to_order_send_summary_us"] = summary
    max_seen_us = _max_receive_to_order_send_us(payload)
    step["max_receive_to_order_send_us"] = max_order_send_us
    step["observed_max_receive_to_order_send_us"] = max_seen_us
    if max_seen_us is None:
        step["ok"] = False
        step["reason"] = "live_inject_order_send_timing_missing"
    elif max_seen_us > max_order_send_us:
        step["ok"] = False
        step["reason"] = "live_inject_order_send_threshold_exceeded"
    return step


def _apply_live_trade_finished_threshold(
    step: dict,
    *,
    max_trade_finished_us: float,
) -> dict:
    payload = step.get("json")
    if not step.get("ok") or not isinstance(payload, dict):
        return step
    summary = payload.get("receive_to_last_trade_finished_us_summary")
    if isinstance(summary, dict):
        step["observed_receive_to_trade_finished_summary_us"] = summary
    max_seen_us = _max_receive_to_trade_finished_us(payload)
    step["max_receive_to_trade_finished_us"] = max_trade_finished_us
    step["observed_max_receive_to_trade_finished_us"] = max_seen_us
    if max_seen_us is None:
        step["ok"] = False
        step["reason"] = "live_inject_trade_finished_timing_missing"
    elif max_seen_us > max_trade_finished_us:
        step["ok"] = False
        step["reason"] = "live_inject_trade_finished_threshold_exceeded"
    return step


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100_000)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-native-buy-p50-us", type=float, default=DEFAULT_MAX_NATIVE_BUY_P50_US)
    parser.add_argument(
        "--max-tdlib-message-p50-us",
        type=float,
        default=DEFAULT_MAX_TDLIB_MESSAGE_P50_US,
    )
    parser.add_argument(
        "--max-tdlib-message-long-body-p50-us",
        type=float,
        default=DEFAULT_MAX_TDLIB_MESSAGE_LONG_BODY_P50_US,
    )
    parser.add_argument(
        "--max-native-buy-multi-p50-us",
        type=float,
        default=DEFAULT_MAX_NATIVE_BUY_MULTI_P50_US,
    )
    parser.add_argument(
        "--max-tdlib-message-multi-p50-us",
        type=float,
        default=DEFAULT_MAX_TDLIB_MESSAGE_MULTI_P50_US,
    )
    parser.add_argument(
        "--max-tdlib-fire-receive-return-p50-us",
        type=float,
        default=DEFAULT_MAX_TDLIB_FIRE_RECEIVE_RETURN_P50_US,
    )
    parser.add_argument(
        "--max-tdlib-fire-order-send-p50-us",
        type=float,
        default=DEFAULT_MAX_TDLIB_FIRE_ORDER_SEND_P50_US,
    )
    parser.add_argument(
        "--max-tdlib-type-filter-fast-p50-ns",
        type=float,
        default=DEFAULT_MAX_TDLIB_TYPE_FILTER_FAST_P50_NS,
    )
    parser.add_argument(
        "--min-tdlib-type-filter-speedup",
        type=float,
        default=DEFAULT_MIN_TDLIB_TYPE_FILTER_SPEEDUP,
    )
    parser.add_argument("--max-live-order-send-us", type=float, default=DEFAULT_MAX_LIVE_ORDER_SEND_US)
    parser.add_argument(
        "--max-live-trade-finished-us",
        type=float,
        default=DEFAULT_MAX_LIVE_TRADE_FINISHED_US,
    )
    parser.add_argument(
        "--live-inject",
        action="store_true",
        help="Also run live TDLib auth + official chat-id file:// order inject.",
    )
    parser.add_argument(
        "--live-inject-iterations",
        type=int,
        default=9,
        help="Official-sample TDLib inject iterations when --live-inject is enabled.",
    )
    parser.add_argument(
        "--require-symbol-cache",
        action="store_true",
        help="Fail if strict TDLib spot-symbol cache is missing or stale.",
    )
    parser.add_argument(
        "--refresh-symbol-cache",
        action="store_true",
        help="Refresh the TDLib spot-symbol cache before checking it.",
    )
    parser.add_argument(
        "--require-trading-config",
        action="store_true",
        help="Fail if live Bybit spot trading env cannot place an order.",
    )
    parser.add_argument(
        "--bybit-clock",
        action="store_true",
        help="Also check Bybit server-time skew as a non-required readiness step.",
    )
    parser.add_argument(
        "--require-bybit-clock",
        action="store_true",
        help="Fail if Bybit server-time skew check fails.",
    )
    parser.add_argument(
        "--race-fallback-warmup",
        action="store_true",
        help="Also require C++ ultra + C++ fast executor warmup for race fallback.",
    )
    parser.add_argument(
        "--strict-live-tdlib",
        action="store_true",
        help=(
            "Shortcut for the TDLib-native live gate: require trading config, "
            "spot-symbol cache, Bybit clock, and live-safe file-order inject."
        ),
    )
    parser.add_argument(
        "--strict-live-race",
        action="store_true",
        help=(
            "Shortcut for the race live gate: strict TDLib-native checks plus "
            "C++ ultra/C++ fast race-fallback warmup."
        ),
    )
    args = parser.parse_args()
    if args.live_inject_iterations <= 0:
        parser.error("--live-inject-iterations must be positive")
    if args.strict_live_tdlib and args.strict_live_race:
        parser.error("--strict-live-tdlib and --strict-live-race are mutually exclusive")
    strict_live_mode = None
    if args.strict_live_tdlib or args.strict_live_race:
        strict_live_mode = "race" if args.strict_live_race else "tdlib"
        args.require_trading_config = True
        args.require_symbol_cache = True
        args.refresh_symbol_cache = True
        args.require_bybit_clock = True
        args.live_inject = True
        if args.strict_live_race:
            args.race_fallback_warmup = True

    python = sys.executable
    native_buy_step = _apply_benchmark_threshold(
        _step(
            "native_buy_preflight_benchmark",
            ["./bin/tdlib_json_relay", "--benchmark-native-buy-preflight", str(args.iterations)],
            args.timeout,
            required=True,
        ),
        max_p50_us=args.max_native_buy_p50_us,
    )
    tdlib_message_step = _apply_benchmark_threshold(
        _step(
            "tdlib_message_buy_preflight_benchmark",
            [
                "./bin/tdlib_json_relay",
                "--benchmark-tdlib-message-buy-preflight",
                str(args.iterations),
            ],
            args.timeout,
            required=True,
        ),
        max_p50_us=args.max_tdlib_message_p50_us,
    )
    tdlib_message_long_body_step = _apply_benchmark_threshold(
        _step(
            "tdlib_message_buy_preflight_long_body_benchmark",
            [
                "./bin/tdlib_json_relay",
                "--benchmark-tdlib-message-buy-preflight-long-body",
                str(args.iterations),
            ],
            args.timeout,
            required=True,
        ),
        max_p50_us=args.max_tdlib_message_long_body_p50_us,
    )
    native_buy_multi_step = _apply_benchmark_threshold(
        _step(
            "native_buy_preflight_multi_benchmark",
            ["./bin/tdlib_json_relay", "--benchmark-native-buy-preflight-multi", str(args.iterations)],
            args.timeout,
            required=True,
        ),
        max_p50_us=args.max_native_buy_multi_p50_us,
    )
    tdlib_message_upbit_step = _apply_benchmark_threshold(
        _step(
            "tdlib_message_buy_preflight_upbit_benchmark",
            [
                "./bin/tdlib_json_relay",
                "--benchmark-tdlib-message-buy-preflight-upbit",
                str(args.iterations),
            ],
            args.timeout,
            required=True,
        ),
        max_p50_us=args.max_tdlib_message_p50_us,
    )
    tdlib_message_multi_step = _apply_benchmark_threshold(
        _step(
            "tdlib_message_buy_preflight_multi_benchmark",
            [
                "./bin/tdlib_json_relay",
                "--benchmark-tdlib-message-buy-preflight-multi",
                str(args.iterations),
            ],
            args.timeout,
            required=True,
        ),
        max_p50_us=args.max_tdlib_message_multi_p50_us,
    )
    tdlib_message_fire_step = _apply_tdlib_fire_threshold(
        _step(
            "tdlib_message_fire_and_forget_benchmark",
            [
                "./bin/tdlib_json_relay",
                "--benchmark-tdlib-message-fire-and-forget",
                str(args.iterations),
            ],
            args.timeout,
            required=True,
        ),
        max_receive_return_p50_us=args.max_tdlib_fire_receive_return_p50_us,
        max_order_send_p50_us=args.max_tdlib_fire_order_send_p50_us,
    )
    tdlib_message_fire_multi_step = _apply_tdlib_fire_threshold(
        _step(
            "tdlib_message_fire_and_forget_multi_benchmark",
            [
                "./bin/tdlib_json_relay",
                "--benchmark-tdlib-message-fire-and-forget-multi",
                str(args.iterations),
            ],
            args.timeout,
            required=True,
        ),
        max_receive_return_p50_us=args.max_tdlib_fire_receive_return_p50_us,
        max_order_send_p50_us=args.max_tdlib_fire_order_send_p50_us,
    )
    tdlib_type_filter_step = _apply_type_filter_threshold(
        _step(
            "tdlib_type_filter_benchmark",
            [
                "./bin/tdlib_json_relay",
                "--benchmark-tdlib-type-filter",
                str(args.iterations),
            ],
            args.timeout,
            required=True,
        ),
        max_fast_p50_ns=args.max_tdlib_type_filter_fast_p50_ns,
        min_speedup=args.min_tdlib_type_filter_speedup,
    )
    steps = [
        _step(
            "trading_config_check",
            [python, "bin/trading_config_gate.py", "check"],
            args.timeout,
            required=args.require_trading_config,
        ),
        _step(
            "listing_classifier_fixture_check",
            [python, "bin/verify_listing_classifiers.py", "--require-tdlib-relay"],
            args.timeout,
            required=True,
        ),
    ]
    if args.refresh_symbol_cache:
        steps.append(
            _step(
                "tdlib_symbol_cache_refresh",
                [python, "bin/tdlib_symbol_cache.py", "refresh"],
                args.timeout,
                required=args.require_symbol_cache,
            )
        )
    steps.extend(
        [
            _step(
                "tdlib_symbol_cache_check",
                [python, "bin/tdlib_symbol_cache.py", "check"],
                args.timeout,
                required=args.require_symbol_cache,
            ),
            _step(
                "native_order_file_scheme_selftest",
                ["./bin/tdlib_json_relay", "--self-test-native-order-file-scheme"],
                args.timeout,
                required=True,
            ),
            _step(
                "native_async_fire_and_forget_reclaim_selftest",
                [
                    "./bin/tdlib_json_relay",
                    "--self-test-native-async-fire-and-forget-reclaim",
                ],
                args.timeout,
                required=True,
            ),
            _step(
                "native_async_ticker_copy_selftest",
                ["./bin/tdlib_json_relay", "--self-test-native-async-ticker-copy"],
                args.timeout,
                required=True,
            ),
            native_buy_step,
            tdlib_message_step,
            tdlib_message_long_body_step,
            native_buy_multi_step,
            tdlib_message_upbit_step,
            tdlib_message_multi_step,
            tdlib_message_fire_step,
            tdlib_message_fire_multi_step,
            tdlib_type_filter_step,
        ]
    )
    if args.bybit_clock or args.require_bybit_clock:
        steps.append(
            _step(
                "bybit_clock_check",
                [python, "bin/bybit_clock_gate.py", "check"],
                args.timeout,
                required=args.require_bybit_clock,
            )
        )
    if args.live_inject:
        async_order_dispatch = _truthy_env(
            "LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH",
            default=False,
        )
        live_inject_step = _apply_live_inject_threshold(
            _step(
                "native_file_order_inject",
                [
                    python,
                    "bin/benchmark_live_ingest.py",
                    "bench",
                    "--backend",
                    "tdlib",
                    "--native-file-order-inject",
                    "--iterations",
                    str(args.live_inject_iterations),
                    "--timeout",
                    str(args.timeout),
                ],
                args.timeout + 5.0,
                required=True,
            ),
            max_order_send_us=args.max_live_order_send_us,
        )
        if async_order_dispatch:
            live_inject_step["async_order_dispatch"] = True
            live_inject_step["trade_finished_threshold_skipped"] = (
                "async dispatch returns after order send starts; Bybit response is handled off the TDLib receive loop"
            )
            steps.append(live_inject_step)
        else:
            steps.append(
                _apply_live_trade_finished_threshold(
                    live_inject_step,
                    max_trade_finished_us=args.max_live_trade_finished_us,
                )
            )

    if args.race_fallback_warmup:
        steps.append(
            _step(
                "race_fallback_warmup",
                [python, "bin/race_fallback_readiness.py", "check"],
                args.timeout + 10.0,
                required=True,
            )
        )

    required_ok = all(step["ok"] for step in steps if step["required"])
    output = {
        "ok": required_ok,
        "mode": "fast_readiness_gate",
        "require_trading_config": args.require_trading_config,
        "require_symbol_cache": args.require_symbol_cache,
        "refresh_symbol_cache": args.refresh_symbol_cache,
        "bybit_clock": args.bybit_clock or args.require_bybit_clock,
        "require_bybit_clock": args.require_bybit_clock,
        "race_fallback_warmup": args.race_fallback_warmup,
        "live_inject": args.live_inject,
        "strict_live_mode": strict_live_mode,
        "thresholds": {
            "max_native_buy_p50_us": args.max_native_buy_p50_us,
            "max_tdlib_message_p50_us": args.max_tdlib_message_p50_us,
            "max_tdlib_message_long_body_p50_us": args.max_tdlib_message_long_body_p50_us,
            "max_native_buy_multi_p50_us": args.max_native_buy_multi_p50_us,
            "max_tdlib_message_multi_p50_us": args.max_tdlib_message_multi_p50_us,
            "max_tdlib_fire_receive_return_p50_us": args.max_tdlib_fire_receive_return_p50_us,
            "max_tdlib_fire_order_send_p50_us": args.max_tdlib_fire_order_send_p50_us,
            "max_tdlib_type_filter_fast_p50_ns": args.max_tdlib_type_filter_fast_p50_ns,
            "min_tdlib_type_filter_speedup": args.min_tdlib_type_filter_speedup,
            "max_live_order_send_us": args.max_live_order_send_us,
            "max_live_trade_finished_us": args.max_live_trade_finished_us,
        },
        "steps": steps,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if required_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
