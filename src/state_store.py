from __future__ import annotations

"""State file for latest seen Telegram post ids."""

import json
import os
import tempfile
import threading
from pathlib import Path

STATE_FILE = Path(__file__).parent.parent / "data" / "detected_listing_posts.json"
MAX_SEEN_MESSAGE_IDS = 512


class StateStore:
    """Track the latest processed Telegram message id per channel."""

    def __init__(self, state_file: Path | str = STATE_FILE):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._state = self._load()
        self._replay_floor = {
            channel_id: self._payload_last_seen(payload)
            for channel_id, payload in self._state.items()
        }

    def _load(self) -> dict:
        if not self.state_file.exists():
            return {}
        try:
            with open(self.state_file, "r") as handle:
                payload = json.load(handle)
                return payload if isinstance(payload, dict) else {}
        except (json.JSONDecodeError, IOError):
            return {}

    def _save(self):
        with tempfile.NamedTemporaryFile(
            "w",
            dir=self.state_file.parent,
            prefix=f"{self.state_file.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(self._state, handle, indent=2, ensure_ascii=False)
            tmp_path = Path(handle.name)
        os.replace(tmp_path, self.state_file)

    def get_last_seen(self, channel_id: str) -> int:
        with self._lock:
            return self._payload_last_seen(self._state.get(channel_id, {}))

    def snapshot_last_seen(self) -> dict[str, int]:
        with self._lock:
            return {
                channel_id: self._payload_last_seen(payload)
                for channel_id, payload in self._state.items()
            }

    def snapshot_seen_message_ids(self) -> dict[str, list[int]]:
        with self._lock:
            return {
                channel_id: self._payload_seen_ids(payload)
                for channel_id, payload in self._state.items()
            }

    def can_mark_seen(self, channel_id: str, message_id: int) -> bool:
        with self._lock:
            payload = self._state.get(channel_id, {})
            seen_ids = set(self._payload_seen_ids(payload))
            message_id = int(message_id)
            return (
                message_id not in seen_ids
                and message_id > int(self._replay_floor.get(channel_id, 0))
            )

    def has_seen_listing(self, channel_id: str, ticker: str) -> bool:
        key = ticker.upper()
        with self._lock:
            payload = self._state.get(channel_id, {})
            seen = payload.get("seen_listing_tickers", {}) if isinstance(payload, dict) else {}
            return key in seen

    def mark_listing_seen(
        self,
        channel_id: str,
        ticker: str,
        message_id: int,
        persist: bool = True,
    ) -> bool:
        key = ticker.upper()
        with self._lock:
            existing = self._payload_dict(self._state.get(channel_id, {}))
            seen = dict(existing.get("seen_listing_tickers", {}))
            if key in seen:
                return False
            seen[key] = int(message_id)
            existing["seen_listing_tickers"] = seen
            self._state[channel_id] = existing
            if persist:
                self._save()
            return True

    @staticmethod
    def _bounded_seen_ids(values) -> list[int]:
        seen_ids: set[int] = set()
        for value in values:
            try:
                seen_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        return sorted(seen_ids)[-MAX_SEEN_MESSAGE_IDS:]

    def mark_seen(self, channel_id: str, message_id: int, persist: bool = True) -> bool:
        with self._lock:
            existing = self._payload_dict(self._state.get(channel_id, {}))
            message_id = int(message_id)
            seen_ids = set(self._payload_seen_ids(existing))
            if message_id in seen_ids:
                return False
            if message_id <= int(self._replay_floor.get(channel_id, 0)):
                return False
            last_seen = self._payload_last_seen(existing)
            seen_ids.add(message_id)
            existing["last_seen_message_id"] = max(last_seen, message_id)
            existing["seen_message_ids"] = self._bounded_seen_ids(seen_ids)
            self._state[channel_id] = {
                **existing,
            }
            if persist:
                self._save()
            return True

    def replace_last_seen_snapshot(
        self,
        snapshot: dict[str, int],
        persist: bool = True,
    ):
        with self._lock:
            for channel_id, message_id in snapshot.items():
                existing = self._payload_dict(self._state.get(channel_id, {}))
                existing["last_seen_message_id"] = max(
                    self._payload_last_seen(existing),
                    int(message_id),
                )
                self._state[channel_id] = existing
            if persist:
                self._save()

    def replace_message_state_snapshot(
        self,
        last_seen: dict[str, int],
        seen_message_ids: dict[str, list[int] | set[int]],
        persist: bool = True,
    ):
        with self._lock:
            channel_ids = set(last_seen) | set(seen_message_ids)
            for channel_id in channel_ids:
                existing = self._payload_dict(self._state.get(channel_id, {}))
                existing["last_seen_message_id"] = max(
                    self._payload_last_seen(existing),
                    int(last_seen.get(channel_id, 0)),
                )
                existing["seen_message_ids"] = self._bounded_seen_ids(
                    seen_message_ids.get(channel_id, [])
                )
                self._state[channel_id] = existing
            if persist:
                self._save()

    def replace_hot_state_snapshot(
        self,
        last_seen: dict[str, int],
        seen_message_ids: dict[str, list[int]],
        persist: bool = True,
    ):
        self.replace_message_state_snapshot(last_seen, seen_message_ids, persist=persist)

    def flush(self):
        with self._lock:
            self._save()

    def clear(self):
        with self._lock:
            self._state = {}
            self._replay_floor = {}
            self._save()

    @staticmethod
    def _payload_last_seen(payload) -> int:
        if isinstance(payload, dict):
            try:
                return int(payload.get("last_seen_message_id", 0))
            except (TypeError, ValueError):
                return 0
        try:
            return int(payload)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _payload_dict(payload) -> dict:
        if isinstance(payload, dict):
            return dict(payload)
        last_seen = StateStore._payload_last_seen(payload)
        return {"last_seen_message_id": last_seen} if last_seen else {}

    @staticmethod
    def _payload_seen_ids(payload) -> list[int]:
        if not isinstance(payload, dict):
            return []
        raw_ids = payload.get("seen_message_ids", [])
        if not isinstance(raw_ids, list):
            return []
        seen_ids = []
        for value in raw_ids:
            try:
                seen_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        return seen_ids
