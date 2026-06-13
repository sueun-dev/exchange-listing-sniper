"""Lightweight latency tracing for the listing sniper hot path."""

from __future__ import annotations

import time


class LatencyTrace:
    """Collect monotonic timestamps and expose stage deltas in ns/us/ms."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._marks: list[tuple[str, int]] = []
        if enabled:
            self.mark("start")

    def mark(self, name: str):
        if not self.enabled:
            return
        self._marks.append((name, time.monotonic_ns()))

    def start_ns(self) -> int | None:
        if not self.enabled or not self._marks:
            return None
        return self._marks[0][1]

    def last_ns(self) -> int | None:
        if not self.enabled or not self._marks:
            return None
        return self._marks[-1][1]

    def as_dict(self) -> dict:
        if not self.enabled or not self._marks:
            return {}

        stages_ns: dict[str, int] = {}
        previous_name, previous_ns = self._marks[0]
        start_ns = previous_ns

        for current_name, current_ns in self._marks[1:]:
            stages_ns[f"{previous_name}_to_{current_name}"] = current_ns - previous_ns
            previous_name, previous_ns = current_name, current_ns

        total_ns = self._marks[-1][1] - start_ns
        return {
            "total_ns": total_ns,
            "total_us": total_ns / 1_000.0,
            "total_ms": total_ns / 1_000_000.0,
            "stages_ns": stages_ns,
            "marks": [name for name, _ in self._marks],
        }


class _DisabledLatencyTrace:
    enabled = False

    def mark(self, name: str):
        return None

    def start_ns(self) -> int | None:
        return None

    def last_ns(self) -> int | None:
        return None

    def as_dict(self) -> dict:
        return {}


NOOP_LATENCY_TRACE = _DisabledLatencyTrace()
