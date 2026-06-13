#!/usr/bin/env python3
"""Benchmark C++ ultra-engine classify -> order-create preflight."""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

TITLE = "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내"
MULTI_TITLE = "[마켓 추가] 센티언트(SENT), 헤이엘사(ELSA) 원화 마켓 추가"


def _set_default_env() -> None:
    os.environ.setdefault("BYBIT_API_KEY", "benchmark-key")
    os.environ.setdefault("BYBIT_API_SECRET", "benchmark-secret")
    os.environ.setdefault("BYBIT_SPOT_BUY_ENABLED", "1")
    os.environ.setdefault("BYBIT_SPOT_BUY_USDT_AMOUNT", "10")
    os.environ.setdefault("LISTING_CPP_ULTRA_ENGINE_ENABLED", "1")
    os.environ.setdefault("LISTING_CPP_ULTRA_ORDER_ON_CACHE_MISS", "1")
    os.environ.setdefault("LISTING_CPP_ULTRA_ORDER_PREFLIGHT_ONLY", "1")


def _percentiles(samples_ns: list[int]) -> dict[str, float]:
    ordered = sorted(samples_ns)
    return {
        "p50_us": ordered[len(ordered) // 2] / 1_000.0,
        "p95_us": ordered[int(len(ordered) * 0.95)] / 1_000.0,
        "avg_us": statistics.fmean(ordered) / 1_000.0,
    }


def _bench_case(
    *,
    engine,
    name: str,
    title: str,
    iterations: int,
    message_offset: int,
    expected_ticker: bytes,
    expected_trade_count: int,
) -> dict[str, float]:
    # Keep measurements on steady-state order prep, not first-use worker startup.
    engine.handle_post_raw(
        exchange="bithumb",
        message_id=10_000_000_000 + message_offset,
        title=title,
    )
    samples_ns: list[int] = []
    for index in range(iterations):
        start_ns = time.perf_counter_ns()
        result = engine.handle_post_raw(
            exchange="bithumb",
            message_id=message_offset + index + 1,
            title=title,
        )
        samples_ns.append(time.perf_counter_ns() - start_ns)
        reason = bytes(result.reason).split(b"\0", 1)[0]
        ticker = bytes(result.ticker).split(b"\0", 1)[0]
        if (
            not result.matched
            or not result.attempted
            or result.ret_code != 0
            or reason != b"cpp_ultra_rest_preflight"
            or ticker != expected_ticker
            or int(result.trade_count) != expected_trade_count
            or int(result.attempted_count) != expected_trade_count
        ):
            raise RuntimeError(
                f"{name}_failed index={index} reason={reason!r} "
                f"ticker={ticker!r} trade_count={int(result.trade_count)} "
                f"attempted_count={int(result.attempted_count)}"
            )
    return _percentiles(samples_ns)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100_000)
    args = parser.parse_args()
    if args.iterations <= 0:
        parser.error("--iterations must be positive")

    _set_default_env()
    from src.cpp_ultra_engine import CppUltraListingEngineBridge  # noqa: WPS433

    engine = CppUltraListingEngineBridge(enabled=True)
    if not engine.is_enabled():
        raise RuntimeError("C++ ultra engine is not enabled")

    single_metric = _bench_case(
        engine=engine,
        name="cpp_ultra_order_preflight",
        title=TITLE,
        iterations=args.iterations,
        message_offset=0,
        expected_ticker=b"STRK",
        expected_trade_count=1,
    )
    multi_metric = _bench_case(
        engine=engine,
        name="cpp_ultra_multi_order_preflight",
        title=MULTI_TITLE,
        iterations=args.iterations,
        message_offset=args.iterations,
        expected_ticker=b"SENT",
        expected_trade_count=2,
    )
    multi_result = engine.handle_post_raw(
        exchange="bithumb",
        message_id=20_000_000_000,
        title=MULTI_TITLE,
    )
    multi_payload = engine.payload_from_raw(
        multi_result,
        exchange="bithumb",
        message_id=20_000_000_000,
    )
    if (
        multi_payload is None
        or [trade["ticker"] for trade in multi_payload["trades"]] != ["SENT", "ELSA"]
        or [trade["symbol"] for trade in multi_payload["trades"]] != ["SENTUSDT", "ELSAUSDT"]
    ):
        raise RuntimeError(f"cpp_ultra_multi_payload_failed: {multi_payload!r}")
    for name, metric in (
        ("cpp_ultra_order_preflight", single_metric),
        ("cpp_ultra_multi_order_preflight", multi_metric),
    ):
        print(
            f"{name}: "
            f"p50={metric['p50_us']:.3f}us "
            f"p95={metric['p95_us']:.3f}us "
            f"avg={metric['avg_us']:.3f}us"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
