from __future__ import annotations

import importlib.util
import time
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = MODULE_DIR / "bin" / "tdlib_symbol_cache.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("tdlib_symbol_cache", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_read_cache_reports_missing(tmp_path):
    module = _load_module()
    result = module._read_cache(tmp_path / "missing.txt", 300)

    assert result["ok"] is False
    assert result["reason"] == "cache_missing"
    assert result["symbol_count"] == 0


def test_write_and_read_cache_ready(tmp_path):
    module = _load_module()
    path = tmp_path / "spot_symbols.txt"

    module._write_cache(path, ["STRKUSDT", "VVVUSDT"])
    result = module._read_cache(path, 300, min_symbol_count=2)

    assert result["ok"] is True
    assert result["reason"] == "ready"
    assert result["symbol_count"] == 2
    assert result["sample"] == ["STRKUSDT", "VVVUSDT"]


def test_read_cache_rejects_tiny_cache_by_default(tmp_path):
    module = _load_module()
    path = tmp_path / "spot_symbols.txt"

    module._write_cache(path, ["STRKUSDT", "VVVUSDT"])
    result = module._read_cache(path, 300)

    assert result["ok"] is False
    assert result["reason"] == "cache_too_small"
    assert result["symbol_count"] == 2
    assert result["min_symbol_count"] == 100


def test_read_cache_reports_stale(tmp_path):
    module = _load_module()
    path = tmp_path / "spot_symbols.txt"
    path.write_text(
        f"# saved_unix_sec={int(time.time()) - 999}\nSTRKUSDT\n",
        encoding="utf-8",
    )

    result = module._read_cache(path, 300)

    assert result["ok"] is False
    assert result["reason"] == "cache_stale"
    assert result["symbol_count"] == 1
