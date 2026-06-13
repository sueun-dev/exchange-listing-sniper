"""Emit and optionally persist raw Telegram source events."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

RAW_SOURCE_DIR = Path(__file__).parent.parent / "data" / "raw_source"


class SourceEventEmitter:
    """Build and persist raw source-ingest events."""

    def __init__(self, raw_source_dir: Path | str = RAW_SOURCE_DIR):
        self.raw_source_dir = Path(raw_source_dir)
        self.raw_source_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _to_iso8601(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        return str(value)

    def build(
        self,
        *,
        channel: dict,
        post: dict,
        latency: dict | None = None,
    ) -> dict:
        event = {
            "event_type": "telegram_source_post",
            "exchange": channel["exchange"],
            "exchange_name": channel["display_name"],
            "channel_id": channel["id"],
            "channel_handle": post["channel_handle"],
            "message_id": int(post["message_id"]),
            "title": post.get("title", ""),
            "text": post.get("text", ""),
            "post_url": post.get("post_url", ""),
            "published_at": self._to_iso8601(post.get("published_at")),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        received_at = self._to_iso8601(post.get("received_at"))
        if received_at:
            event["received_at"] = received_at
        received_monotonic_ns = post.get("received_monotonic_ns")
        if received_monotonic_ns is not None:
            event["received_monotonic_ns"] = int(received_monotonic_ns)
        if latency:
            event["latency"] = latency
        return event

    def persist(self, event: dict) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = (
            f"{timestamp}_{event['exchange']}_{event['message_id']}.json"
        )
        out_path = self.raw_source_dir / filename
        with open(out_path, "w") as handle:
            json.dump(event, handle, indent=2, ensure_ascii=False)
        return out_path
