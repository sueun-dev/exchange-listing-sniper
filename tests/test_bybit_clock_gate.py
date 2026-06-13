from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = MODULE_DIR / "bin" / "bybit_clock_gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("bybit_clock_gate", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_server_time_ms_prefers_time_nano():
    module = _load_module()
    payload = {
        "retCode": 0,
        "result": {
            "timeSecond": "1688639403",
            "timeNano": "1688639403423213947",
        },
        "time": 1688639403423,
    }

    assert module._server_time_ms_from_payload(payload) == 1688639403423.2139


def test_evaluate_clock_keeps_ntp_synced_clock_ok():
    module = _load_module()

    result = module._evaluate_clock(
        server_time_ms=100_000.0,
        local_midpoint_ms=100_020.0,
        timestamp_bias_ms=0,
        recv_window_ms=5000,
        max_clock_skew_ms=1000.0,
        ahead_margin_ms=100.0,
    )

    assert result["ok"] is True
    assert result["within_bybit_window"] is True
    assert result["within_max_skew"] is True


def test_evaluate_clock_fails_when_local_clock_too_far_ahead():
    module = _load_module()

    result = module._evaluate_clock(
        server_time_ms=100_000.0,
        local_midpoint_ms=101_000.0,
        timestamp_bias_ms=0,
        recv_window_ms=5000,
        max_clock_skew_ms=1000.0,
        ahead_margin_ms=100.0,
    )

    assert result["ok"] is False
    assert result["within_bybit_window"] is False


def test_evaluate_clock_fails_when_local_clock_too_far_behind():
    module = _load_module()

    result = module._evaluate_clock(
        server_time_ms=100_000.0,
        local_midpoint_ms=98_900.0,
        timestamp_bias_ms=0,
        recv_window_ms=5000,
        max_clock_skew_ms=1000.0,
        ahead_margin_ms=100.0,
    )

    assert result["ok"] is False
    assert result["within_bybit_window"] is True
    assert result["within_max_skew"] is False


def test_check_clock_uses_midpoint_and_reports_ready(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "load_env_settings",
        lambda _keys: {
            "BYBIT_API_BASE_URL": "https://api.bybit.com",
            "BYBIT_RECV_WINDOW": "5000",
            "BYBIT_TIMESTAMP_BIAS_MS": "-50",
            "BYBIT_MAX_CLOCK_SKEW_MS": "1000",
            "BYBIT_CLOCK_AHEAD_MARGIN_MS": "100",
            "BYBIT_CLOCK_MAX_RTT_MS": "1000",
            "BYBIT_CLOCK_GATE_TIMEOUT_SEC": "2",
        },
    )

    def fake_fetch(**_kwargs):
        return {
            "endpoint": "https://api.bybit.com/v5/market/time",
            "http_status": 200,
            "payload": {
                "retCode": 0,
                "result": {
                    "timeNano": "100000000000",
                },
                "time": 100_000,
            },
            "local_start_ms": 100_010.0,
            "local_end_ms": 100_030.0,
        }

    result = module.check_clock(fetch_fn=fake_fetch)

    assert result["ok"] is True
    assert result["request_rtt_ms"] == 20.0
    assert result["rtt_ok"] is True
    assert result["raw_skew_ms"] == 20.0
    assert result["skew_ms"] == -30.0
    assert result["timestamp_bias_ms"] == -50


def test_check_clock_fails_ret_code_error(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "load_env_settings", lambda _keys: {})

    def fake_fetch(**_kwargs):
        return {
            "endpoint": "https://api.bybit.com/v5/market/time",
            "http_status": 200,
            "payload": {
                "retCode": 10001,
                "retMsg": "error",
                "time": 100_000,
            },
            "local_start_ms": 100_010.0,
            "local_end_ms": 100_030.0,
        }

    result = module.check_clock(fetch_fn=fake_fetch)

    assert result["ok"] is False
    assert result["reason"] == "bybit_time_ret_code_not_ok"


def test_check_clock_fails_slow_time_probe(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "load_env_settings",
        lambda _keys: {
            "BYBIT_CLOCK_MAX_RTT_MS": "100",
        },
    )

    def fake_fetch(**_kwargs):
        return {
            "endpoint": "https://api.bybit.com/v5/market/time",
            "http_status": 200,
            "payload": {
                "retCode": 0,
                "time": 100_000,
            },
            "local_start_ms": 99_900.0,
            "local_end_ms": 100_100.0,
        }

    result = module.check_clock(fetch_fn=fake_fetch)

    assert result["ok"] is False
    assert result["rtt_ok"] is False
    assert result["reason"] == "bybit_time_rtt_threshold_exceeded"


def test_check_clock_fails_unparseable_response(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "load_env_settings", lambda _keys: {})

    def fake_fetch(**_kwargs):
        return {
            "endpoint": "https://api.bybit.com/v5/market/time",
            "http_status": 200,
            "payload": {"retCode": 0, "result": {}},
            "local_start_ms": 100_010.0,
            "local_end_ms": 100_030.0,
        }

    result = module.check_clock(fetch_fn=fake_fetch)

    assert result["ok"] is False
    assert result["reason"] == "bybit_time_response_unparseable"
