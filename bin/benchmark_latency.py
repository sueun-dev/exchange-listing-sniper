#!/usr/bin/env python3
from __future__ import annotations

"""Synthetic latency benchmark for the exchange-listing hot path helpers."""

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from src.announcement_filter import classify_listing_title_python  # noqa: E402
from src.cpp_ultra_engine import CppUltraListingEngineBridge  # noqa: E402
from src.poller import ExchangeListingPoller  # noqa: E402
from src.race_realtime_client import _FirstArrivalGate  # noqa: E402
from src.telegram_realtime_client import RealtimeTelegramChannelClient  # noqa: E402

TITLE = "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내"
TEXT = TITLE + "\n상장 안내 본문입니다."
PUBLISHED_AT = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)


class _BenchmarkMarketClient:
    def refresh_market_cache(self):
        return None


class _BenchmarkSpotBuyer:
    pass


class _BenchmarkStateStore:
    def snapshot_last_seen(self):
        return {}

    def mark_seen(self, channel_id: str, message_id: int, persist: bool = True):
        return True


class _BenchmarkRawUltraResult:
    duplicate = False
    matched = True


class _BenchmarkUltraEngine:
    def is_enabled(self):
        return True

    def warmup(self):
        return {"ok": True}

    def handle_post_raw(self, *, exchange: str, message_id: int, title: str):
        return _BenchmarkRawUltraResult()

    def payload_from_raw(self, raw_result, **_kwargs):
        return None


def _make_benchmark_cpp_ultra_poller() -> ExchangeListingPoller:
    poller = ExchangeListingPoller(
        bybit_client=_BenchmarkMarketClient(),
        spot_buyer=_BenchmarkSpotBuyer(),
        state_store=_BenchmarkStateStore(),
        cpp_ultra_engine=_BenchmarkUltraEngine(),
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=True,
        defer_persistence=True,
        defer_post_trade_work=True,
        hot_state_enabled=True,
        state_flush_interval_sec=0,
        emit_ultra_ack=False,
    )
    poller._submit_background = lambda *_args, **_kwargs: None
    return poller


def _make_native_cpp_ultra_poller() -> ExchangeListingPoller | None:
    os.environ["BYBIT_SPOT_BUY_ENABLED"] = "0"
    os.environ["LISTING_CPP_ULTRA_ENGINE_ENABLED"] = "1"
    ultra_engine = CppUltraListingEngineBridge(enabled=True)
    if not ultra_engine.is_enabled():
        return None
    poller = ExchangeListingPoller(
        bybit_client=_BenchmarkMarketClient(),
        spot_buyer=_BenchmarkSpotBuyer(),
        state_store=_BenchmarkStateStore(),
        cpp_ultra_engine=ultra_engine,
        enable_trading=True,
        enable_bybit_warmup=False,
        enable_cpp_ultra_warmup=True,
        defer_persistence=True,
        defer_post_trade_work=True,
        hot_state_enabled=True,
        state_flush_interval_sec=0,
        emit_ultra_ack=False,
    )
    poller._submit_background = lambda *_args, **_kwargs: None
    return poller


def _percentiles(samples_ns: list[int]) -> dict[str, float]:
    ordered = sorted(samples_ns)
    return {
        "p50_us": round(ordered[len(ordered) // 2] / 1_000.0, 3),
        "p95_us": round(ordered[int(len(ordered) * 0.95)] / 1_000.0, 3),
        "avg_us": round(statistics.fmean(ordered) / 1_000.0, 3),
    }


def _bench(name: str, iterations: int, func) -> tuple[str, dict[str, float]]:
    samples_ns: list[int] = []
    for index in range(iterations):
        start_ns = time.perf_counter_ns()
        func(index)
        samples_ns.append(time.perf_counter_ns() - start_ns)
    return name, _percentiles(samples_ns)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=2_000)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only")
    args = parser.parse_args()

    if args.iterations <= 0:
        parser.error("--iterations must be positive")

    race_gate_unique = _FirstArrivalGate()
    race_gate_duplicate = _FirstArrivalGate()
    race_gate_duplicate.claim("BithumbExchange", 1)
    cpp_ultra_poller = _make_benchmark_cpp_ultra_poller()
    native_cpp_ultra_poller = _make_native_cpp_ultra_poller()

    benchmarks = [
        _bench(
            "build_post_full",
            args.iterations,
            lambda index: RealtimeTelegramChannelClient.build_post(
                channel_handle="BithumbExchange",
                message_id=index + 1,
                text=TEXT,
                published_at=PUBLISHED_AT,
                received_monotonic_ns=time.monotonic_ns(),
            ),
        ),
        _bench(
            "build_post_trade",
            args.iterations,
            lambda index: RealtimeTelegramChannelClient.build_trade_post(
                channel_handle="BithumbExchange",
                message_id=index + 1,
                text=TEXT,
                title=TITLE,
                published_at=PUBLISHED_AT,
                received_monotonic_ns=time.monotonic_ns(),
            ),
        ),
        _bench(
            "build_post_minimal",
            args.iterations,
            lambda index: RealtimeTelegramChannelClient.build_minimal_post(
                channel_handle="BithumbExchange",
                message_id=index + 1,
                text=TEXT,
                published_at=PUBLISHED_AT,
                received_monotonic_ns=time.monotonic_ns(),
            ),
        ),
        _bench(
            "python_classifier",
            args.iterations,
            lambda index: classify_listing_title_python(
                exchange="bithumb",
                title=TITLE,
                display_name="Bithumb",
            ),
        ),
        _bench(
            "race_gate_unique",
            args.iterations,
            lambda index: race_gate_unique.claim(
                "BithumbExchange",
                index + 1,
            ),
        ),
        _bench(
            "race_gate_duplicate",
            args.iterations,
            lambda index: race_gate_duplicate.claim("BithumbExchange", 1),
        ),
        _bench(
            "process_post_cpp_ultra_fire",
            args.iterations,
            lambda index: cpp_ultra_poller.process_post(
                "bithumb",
                {
                    "channel_handle": "BithumbExchange",
                    "message_id": index + 1,
                    "published_at": PUBLISHED_AT,
                    "received_monotonic_ns": 0,
                    "title": TITLE,
                },
            ),
        ),
        _bench(
            "process_post_tdlib_native_trade_skip",
            args.iterations,
            lambda index: cpp_ultra_poller.process_post(
                "bithumb",
                {
                    "channel_handle": "BithumbExchange",
                    "message_id": args.iterations + index + 1,
                    "published_at": PUBLISHED_AT,
                    "received_monotonic_ns": 0,
                    "title": TITLE,
                    "native_listing": {
                        "signal_type": "market_add",
                        "ticker": "STRK",
                        "tickers": ["STRK"],
                        "asset_name": "스타크넷",
                        "markets": ["KRW"],
                    },
                    "native_trade": {
                        "enabled": True,
                        "attempted": True,
                        "executed": False,
                        "reason": "tdlib_native_rest_preflight",
                        "symbol": "STRKUSDT",
                        "order_link_id": f"ls-b-{args.iterations + index + 1}-STRK",
                    },
                },
            ),
        ),
    ]
    if native_cpp_ultra_poller is not None:
        benchmarks.append(
            _bench(
                "process_post_cpp_ultra_native_disabled",
                args.iterations,
                lambda index: native_cpp_ultra_poller.process_post(
                    "bithumb",
                    {
                        "channel_handle": "BithumbExchange",
                        "message_id": index + 1,
                        "published_at": PUBLISHED_AT,
                        "received_monotonic_ns": 0,
                        "title": TITLE,
                    },
                ),
            )
        )

    payload = {"iterations": args.iterations, "metrics": dict(benchmarks)}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name, metric in benchmarks:
            print(
                f"{name}: p50={metric['p50_us']}us "
                f"p95={metric['p95_us']}us avg={metric['avg_us']}us"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
