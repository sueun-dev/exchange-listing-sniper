"""Small normalization helpers for external listing payloads."""

from __future__ import annotations


def string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple)):
        return []

    items = []
    for item in value:
        if item is None:
            continue
        text = str(item)
        if text:
            items.append(text)
    return items
