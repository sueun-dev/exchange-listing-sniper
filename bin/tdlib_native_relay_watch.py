#!/usr/bin/env python3
"""Run the minimal TDLib C++ native-buy relay without main.py/poller."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from src.tdlib_realtime_client import TdlibRealtimeChannelClient  # noqa: E402


def _split_channels(value: str) -> list[str]:
    channels = [
        item.strip().lstrip("@")
        for item in value.replace(";", ",").split(",")
        if item.strip()
    ]
    if not channels:
        raise argparse.ArgumentTypeError("at least one Telegram channel is required")
    return channels


async def _run(args) -> int:
    client = TdlibRealtimeChannelClient(
        relay_path=args.relay_path,
        database_dir=args.database_dir,
    )

    def _print_ready():
        if args.ready_json:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "mode": "tdlib_native_relay_only",
                        "channels": args.channels,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    await client.run_native_buy_relay_only(
        channel_handles=args.channels,
        on_ready=_print_ready,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channels",
        type=_split_channels,
        default=["upbit_news", "BithumbExchange"],
        help="Comma-separated Telegram channels to watch.",
    )
    parser.add_argument("--relay-path", default=None)
    parser.add_argument("--database-dir", default=None)
    parser.add_argument(
        "--ready-json",
        action="store_true",
        help="Print one JSON line after native-buy readiness is confirmed.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
