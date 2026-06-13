#!/usr/bin/env python3
from __future__ import annotations

"""Benchmark Python, C++, and Rust listing-title classifiers."""

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from src.announcement_filter import classify_listing_title_python
from src.native_classifier import (  # noqa: E402
    BENCHMARK_CACHE_PATH,
    NativeClassifierBackend,
    _native_library_paths,
)

POSITIVE_TITLE = "[거래] 베니스토큰(VVV) 신규 거래지원 안내 (KRW, BTC 마켓)"
NEGATIVE_TITLE = "[거래] 유통량 계획표 변경 안내 : 오르카(ORCA)"


def _percentiles(samples_ns: list[int]) -> dict[str, float]:
    ordered = sorted(samples_ns)
    return {
        "p50_us": round(ordered[len(ordered) // 2] / 1_000.0, 3),
        "p95_us": round(ordered[int(len(ordered) * 0.95)] / 1_000.0, 3),
        "avg_us": round(statistics.fmean(ordered) / 1_000.0, 3),
    }


def _benchmark(callable_, iterations: int) -> dict[str, float]:
    samples_ns: list[int] = []
    for index in range(iterations):
        title = POSITIVE_TITLE if index % 3 else NEGATIVE_TITLE
        exchange = "upbit" if index % 2 == 0 else "bithumb"
        start_ns = time.perf_counter_ns()
        callable_(exchange, title)
        samples_ns.append(time.perf_counter_ns() - start_ns)
    return _percentiles(samples_ns)


def _build_native_classifiers() -> None:
    script = MODULE_DIR / "bin" / "build_native_classifiers.sh"
    if script.exists():
        subprocess.run(["bash", str(script)], cwd=MODULE_DIR, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100_000)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--write-cache", action="store_true", help="Persist native winner to data/native_classifier_benchmark.json")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only")
    args = parser.parse_args()

    if args.iterations <= 0:
        parser.error("--iterations must be positive")

    if not args.skip_build:
        _build_native_classifiers()

    metrics: dict[str, dict[str, float]] = {}
    metrics["python_classifier"] = _benchmark(
        lambda exchange, title: classify_listing_title_python(
            exchange=exchange,
            title=title,
            display_name=exchange,
        ),
        args.iterations,
    )

    native_backends: dict[str, NativeClassifierBackend] = {}
    for name, path in _native_library_paths().items():
        if not path.exists():
            continue
        try:
            native_backends[name] = NativeClassifierBackend(name=name, library_path=path)
        except Exception as exc:
            metrics[f"{name}_classifier"] = {"load_error": str(exc)}

    for name, backend in native_backends.items():
        metrics[f"{name}_classifier"] = _benchmark(
            lambda exchange, title, backend=backend: backend.classify(exchange, title),
            args.iterations,
        )

    comparable = {
        name: value
        for name, value in metrics.items()
        if isinstance(value.get("p50_us"), (int, float))
    }
    winner = min(comparable, key=lambda name: comparable[name]["p50_us"]) if comparable else None
    native_winner = None
    native_comparable = {
        name: value for name, value in comparable.items() if name != "python_classifier"
    }
    if native_comparable:
        native_winner = min(native_comparable, key=lambda name: native_comparable[name]["p50_us"])

    payload = {
        "iterations": args.iterations,
        "metrics": metrics,
        "native_winner": native_winner.replace("_classifier", "") if native_winner else None,
        "overall_winner": winner.replace("_classifier", "") if winner else None,
        "benchmark_positive_title": POSITIVE_TITLE,
        "benchmark_negative_title": NEGATIVE_TITLE,
    }

    if native_winner and args.write_cache:
        BENCHMARK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BENCHMARK_CACHE_PATH.write_text(
            json.dumps(
                {
                    "winner": native_winner.replace("_classifier", ""),
                    "iterations": args.iterations,
                    "metrics": metrics,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name, metric in metrics.items():
            if "load_error" in metric:
                print(f"{name}: load_error={metric['load_error']}")
            else:
                print(
                    f"{name}: p50={metric['p50_us']}us "
                    f"p95={metric['p95_us']}us avg={metric['avg_us']}us"
                )
        print(f"native winner: {payload['native_winner']}")
        print(f"overall winner: {payload['overall_winner']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
