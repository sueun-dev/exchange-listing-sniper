"""Shared .env loader for module-local runtime settings."""

from __future__ import annotations

import os
from pathlib import Path

MODULE_DIR = Path(__file__).parent.parent
REPO_ROOT = MODULE_DIR.parent.parent
ENV_FILES = [
    REPO_ROOT / ".env",
    MODULE_DIR / ".env",
]


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_env_settings(keys: set[str] | None = None) -> dict[str, str]:
    settings: dict[str, str] = {}
    for env_file in ENV_FILES:
        settings.update(parse_env_file(env_file))

    target_keys = keys or set(settings)
    for key in target_keys:
        value = os.getenv(key)
        if value:
            settings[key] = value
    return settings
