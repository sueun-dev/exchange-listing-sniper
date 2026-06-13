"""Emit and persist listing signals."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SIGNAL_DIR = Path(__file__).parent.parent / "data" / "signals"
TRADE_PROOF_DIR = Path(__file__).parent.parent / "data" / "trade_proofs"


class SignalEmitter:
    """Persist listing signals as JSON files."""

    def __init__(
        self,
        signal_dir: Path | str = SIGNAL_DIR,
        trade_proof_dir: Path | str | None = None,
    ):
        self.signal_dir = Path(signal_dir)
        self.signal_dir.mkdir(parents=True, exist_ok=True)
        self.trade_proof_dir = (
            Path(trade_proof_dir)
            if trade_proof_dir is not None
            else TRADE_PROOF_DIR
        )
        self.trade_proof_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _to_iso8601(value) -> str:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    def build(
        self,
        *,
        post: dict,
        listing: dict,
        bybit: dict,
        trade: dict | None = None,
        latency: dict | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        post_url = post.get("post_url")
        if not post_url:
            post_url = (
                f"https://t.me/{post.get('channel_handle', '')}/{post.get('message_id', '')}"
            )
        text = post.get("text") or post.get("title", "")
        signal = {
            "exchange": listing["exchange"],
            "exchange_name": listing["display_name"],
            "signal_type": listing["signal_type"],
            "ticker": listing["ticker"],
            "asset_name": listing["asset_name"],
            "markets": listing["markets"],
            "channel_handle": post["channel_handle"],
            "message_id": post["message_id"],
            "title": post.get("title", text),
            "text": text,
            "post_url": post_url,
            "published_at": self._to_iso8601(post["published_at"]),
            "bybit_symbol": bybit["symbol"],
            "bybit_spot": bybit["spot"],
            "bybit_perp": bybit["perp"],
            "bybit_any": bybit["any"],
            "trade": trade or {},
            "detected_at": now,
        }
        if "cache_ready" in bybit:
            signal["bybit_cache_ready"] = bybit["cache_ready"]
        if "cache_age_ms" in bybit:
            signal["bybit_cache_age_ms"] = bybit["cache_age_ms"]
        if latency:
            signal["latency"] = latency
        return signal

    def persist(self, signal: dict) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = (
            f"{timestamp}_{signal['exchange']}_{signal['ticker']}_{signal['message_id']}.json"
        )
        out_path = self.signal_dir / filename
        with open(out_path, "w") as handle:
            json.dump(signal, handle, indent=2, ensure_ascii=False)
        return out_path

    def persist_trade_proof(self, *, post: dict, listing: dict, trade: dict) -> Path:
        """Append minimal post-order evidence without blocking the buy path."""
        now = datetime.now(timezone.utc)
        proof = {
            "recorded_at": now.isoformat(),
            "exchange": listing.get("exchange"),
            "ticker": listing.get("ticker"),
            "asset_name": listing.get("asset_name"),
            "markets": listing.get("markets"),
            "channel_handle": post.get("channel_handle"),
            "message_id": post.get("message_id"),
            "title": post.get("title", post.get("text", "")),
            "post_url": post.get(
                "post_url",
                f"https://t.me/{post.get('channel_handle', '')}/{post.get('message_id', '')}",
            ),
            "published_at": self._to_iso8601(post.get("published_at")),
            "received_monotonic_ns": post.get("received_monotonic_ns"),
            "received_python_monotonic_ns": post.get("received_python_monotonic_ns"),
            "relay_received_monotonic_ns": post.get("relay_received_monotonic_ns"),
            "trade": trade or {},
        }
        if trade and trade.get("trade_finished_monotonic_ns") is not None:
            try:
                received_ns = int(post.get("received_monotonic_ns"))
                finished_ns = int(trade["trade_finished_monotonic_ns"])
            except (TypeError, ValueError):
                pass
            else:
                elapsed_ns = max(0, finished_ns - received_ns)
                proof["receive_to_trade_finished_ns"] = elapsed_ns
                proof["receive_to_trade_finished_ms"] = elapsed_ns / 1_000_000.0
        if trade and trade.get("order_send_started_monotonic_ns") is not None:
            try:
                received_ns = int(post.get("received_monotonic_ns"))
                send_started_ns = int(trade["order_send_started_monotonic_ns"])
            except (TypeError, ValueError):
                pass
            else:
                elapsed_ns = max(0, send_started_ns - received_ns)
                proof["receive_to_order_send_started_ns"] = elapsed_ns
                proof["receive_to_order_send_started_ms"] = elapsed_ns / 1_000_000.0
        out_path = self.trade_proof_dir / f"{now:%Y%m%d}_native_trades.jsonl"
        with open(out_path, "a", encoding="utf-8") as handle:
            json.dump(proof, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        return out_path

    def emit(
        self,
        *,
        post: dict,
        listing: dict,
        bybit: dict,
        trade: dict | None = None,
    ) -> dict:
        signal = self.build(
            post=post,
            listing=listing,
            bybit=bybit,
            trade=trade,
            latency=None,
        )
        self.persist(signal)
        return signal
