from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = MODULE_DIR / "bin" / "fast_readiness_gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("fast_readiness_gate", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_from_stdout_parses_native_buy_preflight():
    module = _load_module()
    parsed = module._benchmark_from_stdout(
        "BENCHMARK_NATIVE_BUY_PREFLIGHT iterations=100000 p50_us=0.25 "
        "p95_us=0.459 avg_us=0.302576\n"
    )

    assert parsed == {
        "name": "BENCHMARK_NATIVE_BUY_PREFLIGHT",
        "iterations": 100000,
        "p50_us": 0.25,
        "p95_us": 0.459,
        "avg_us": 0.302576,
    }


def test_json_from_stdout_parses_gate_payload():
    module = _load_module()
    parsed = module._json_from_stdout('{"ok": true, "reason": "ready"}\n')

    assert parsed == {"ok": True, "reason": "ready"}


def test_json_from_stdout_parses_payload_after_warning_prefix():
    module = _load_module()
    parsed = module._json_from_stdout(
        "Native classifier rust failed semantic canary checks; ignoring old.dylib\n"
        '{"ok": true, "reason": "ready"}\n'
    )

    assert parsed == {"ok": True, "reason": "ready"}


def test_type_filter_benchmark_from_stdout_parses_fast_and_legacy_metrics():
    module = _load_module()
    parsed = module._type_filter_benchmark_from_stdout(
        "BENCHMARK_TDLIB_TYPE_FILTER iterations=100000 ops_per_sample=128 "
        "legacy_p50_ns=211 legacy_p95_ns=228 legacy_avg_ns=245.536 "
        "fast_p50_ns=14 fast_p95_ns=27 fast_avg_ns=26.8746 "
        "live_cstr_p50_ns=4 live_cstr_p95_ns=5 live_cstr_avg_ns=4.27426\n"
    )

    assert parsed == {
        "name": "BENCHMARK_TDLIB_TYPE_FILTER",
        "iterations": 100000,
        "ops_per_sample": 128,
        "legacy_p50_ns": 211.0,
        "legacy_p95_ns": 228.0,
        "legacy_avg_ns": 245.536,
        "fast_p50_ns": 14.0,
        "fast_p95_ns": 27.0,
        "fast_avg_ns": 26.8746,
        "live_cstr_p50_ns": 4.0,
        "live_cstr_p95_ns": 5.0,
        "live_cstr_avg_ns": 4.27426,
    }


def test_tdlib_fire_benchmark_from_stdout_parses_emit_off_metrics():
    module = _load_module()
    parsed = module._tdlib_fire_benchmark_from_stdout(
        "BENCHMARK_TDLIB_MESSAGE_FIRE_AND_FORGET iterations=50000 "
        "receive_return_p50_us=0.792 receive_return_p95_us=2.875 "
        "receive_return_avg_us=1.2389 order_send_started_p50_us=2.459 "
        "order_send_started_p95_us=14.375 order_send_started_avg_us=5.27899\n"
    )

    assert parsed == {
        "name": "BENCHMARK_TDLIB_MESSAGE_FIRE_AND_FORGET",
        "iterations": 50000,
        "receive_return_p50_us": 0.792,
        "receive_return_p95_us": 2.875,
        "receive_return_avg_us": 1.2389,
        "order_send_started_p50_us": 2.459,
        "order_send_started_p95_us": 14.375,
        "order_send_started_avg_us": 5.27899,
    }


def test_tdlib_fire_benchmark_from_stdout_parses_multi_emit_off_metrics():
    module = _load_module()
    parsed = module._tdlib_fire_benchmark_from_stdout(
        "BENCHMARK_TDLIB_MESSAGE_FIRE_AND_FORGET_MULTI iterations=50000 "
        "receive_return_p50_us=1.25 receive_return_p95_us=2.0 "
        "receive_return_avg_us=1.5 order_send_started_p50_us=3.0 "
        "order_send_started_p95_us=5.0 order_send_started_avg_us=3.5\n"
    )

    assert parsed == {
        "name": "BENCHMARK_TDLIB_MESSAGE_FIRE_AND_FORGET_MULTI",
        "iterations": 50000,
        "receive_return_p50_us": 1.25,
        "receive_return_p95_us": 2.0,
        "receive_return_avg_us": 1.5,
        "order_send_started_p50_us": 3.0,
        "order_send_started_p95_us": 5.0,
        "order_send_started_avg_us": 3.5,
    }


def test_apply_benchmark_threshold_keeps_fast_step_ok():
    module = _load_module()
    step = {
        "ok": True,
        "benchmark": {
            "p50_us": 0.25,
        },
    }

    assert module._apply_benchmark_threshold(step, max_p50_us=5.0)["ok"] is True


def test_apply_benchmark_threshold_fails_slow_step():
    module = _load_module()
    step = {
        "ok": True,
        "benchmark": {
            "p50_us": 10.5,
        },
    }

    result = module._apply_benchmark_threshold(step, max_p50_us=5.0)

    assert result["ok"] is False
    assert result["reason"] == "benchmark_p50_threshold_exceeded"


def test_apply_type_filter_threshold_keeps_fast_reject_ok():
    module = _load_module()
    step = {
        "ok": True,
        "type_filter_benchmark": {
            "legacy_p50_ns": 210.0,
            "fast_p50_ns": 14.0,
        },
    }

    result = module._apply_type_filter_threshold(
        step,
        max_fast_p50_ns=100.0,
        min_speedup=3.0,
    )

    assert result["ok"] is True
    assert result["observed_speedup"] == 15.0


def test_apply_type_filter_threshold_fails_slow_fast_reject():
    module = _load_module()
    step = {
        "ok": True,
        "type_filter_benchmark": {
            "legacy_p50_ns": 210.0,
            "fast_p50_ns": 150.0,
        },
    }

    result = module._apply_type_filter_threshold(
        step,
        max_fast_p50_ns=100.0,
        min_speedup=3.0,
    )

    assert result["ok"] is False
    assert result["reason"] == "tdlib_type_filter_fast_p50_threshold_exceeded"


def test_apply_type_filter_threshold_fails_weak_speedup():
    module = _load_module()
    step = {
        "ok": True,
        "type_filter_benchmark": {
            "legacy_p50_ns": 40.0,
            "fast_p50_ns": 20.0,
        },
    }

    result = module._apply_type_filter_threshold(
        step,
        max_fast_p50_ns=100.0,
        min_speedup=3.0,
    )

    assert result["ok"] is False
    assert result["reason"] == "tdlib_type_filter_speedup_threshold_missed"


def test_apply_tdlib_fire_threshold_keeps_fast_emit_off_path_ok():
    module = _load_module()
    step = {
        "ok": True,
        "tdlib_fire_benchmark": {
            "receive_return_p50_us": 0.792,
            "order_send_started_p50_us": 2.459,
        },
    }

    result = module._apply_tdlib_fire_threshold(
        step,
        max_receive_return_p50_us=5.0,
        max_order_send_p50_us=15.0,
    )

    assert result["ok"] is True
    assert result["max_receive_return_p50_us"] == 5.0
    assert result["max_order_send_started_p50_us"] == 15.0


def test_apply_tdlib_fire_threshold_fails_slow_receive_return():
    module = _load_module()
    step = {
        "ok": True,
        "tdlib_fire_benchmark": {
            "receive_return_p50_us": 6.0,
            "order_send_started_p50_us": 2.0,
        },
    }

    result = module._apply_tdlib_fire_threshold(
        step,
        max_receive_return_p50_us=5.0,
        max_order_send_p50_us=15.0,
    )

    assert result["ok"] is False
    assert result["reason"] == "tdlib_fire_receive_return_p50_threshold_exceeded"


def test_apply_tdlib_fire_threshold_fails_slow_order_send():
    module = _load_module()
    step = {
        "ok": True,
        "tdlib_fire_benchmark": {
            "receive_return_p50_us": 1.0,
            "order_send_started_p50_us": 16.0,
        },
    }

    result = module._apply_tdlib_fire_threshold(
        step,
        max_receive_return_p50_us=5.0,
        max_order_send_p50_us=15.0,
    )

    assert result["ok"] is False
    assert result["reason"] == "tdlib_fire_order_send_p50_threshold_exceeded"


def test_apply_live_inject_threshold_keeps_fast_events_ok():
    module = _load_module()
    step = {
        "ok": True,
        "json": {
            "events": [
                {"receive_to_last_order_send_started_us": 4.0},
                {"receive_to_last_order_send_started_us": 11.0},
            ],
        },
    }

    result = module._apply_live_inject_threshold(step, max_order_send_us=1_000.0)

    assert result["ok"] is True
    assert result["observed_max_receive_to_order_send_us"] == 11.0


def test_apply_live_inject_threshold_prefers_summary_max_and_records_distribution():
    module = _load_module()
    step = {
        "ok": True,
        "json": {
            "receive_to_last_order_send_started_us_summary": {
                "count": 9,
                "p50_us": 3.0,
                "p95_us": 7.0,
                "max_us": 7.0,
            },
            "events": [
                {"receive_to_last_order_send_started_us": 99.0},
            ],
        },
    }

    result = module._apply_live_inject_threshold(step, max_order_send_us=10.0)

    assert result["ok"] is True
    assert result["observed_max_receive_to_order_send_us"] == 7.0
    assert result["observed_receive_to_order_send_summary_us"]["count"] == 9


def test_apply_live_inject_threshold_fails_missing_timing():
    module = _load_module()
    step = {
        "ok": True,
        "json": {
            "events": [
                {"receive_to_last_trade_finished_us": 40.0},
            ],
        },
    }

    result = module._apply_live_inject_threshold(step, max_order_send_us=1_000.0)

    assert result["ok"] is False
    assert result["reason"] == "live_inject_order_send_timing_missing"


def test_apply_live_inject_threshold_skips_emit_off_fire_and_forget_dispatch():
    module = _load_module()
    step = {
        "ok": True,
        "json": {
            "mode_detail": "emit_off_fire_and_forget_dispatch",
            "events": [
                {"native_dispatch_attempt_count": 2},
            ],
        },
    }

    result = module._apply_live_inject_threshold(step, max_order_send_us=1_000.0)

    assert result["ok"] is True
    assert "live_inject_order_send_threshold_skipped" in result
    assert "observed_max_receive_to_order_send_us" not in result


def test_apply_live_inject_threshold_fails_slow_event():
    module = _load_module()
    step = {
        "ok": True,
        "json": {
            "events": [
                {"receive_to_last_order_send_started_us": 1_250.0},
            ],
        },
    }

    result = module._apply_live_inject_threshold(step, max_order_send_us=1_000.0)

    assert result["ok"] is False
    assert result["reason"] == "live_inject_order_send_threshold_exceeded"


def test_apply_live_trade_finished_threshold_prefers_summary_max():
    module = _load_module()
    step = {
        "ok": True,
        "json": {
            "receive_to_last_trade_finished_us_summary": {
                "count": 9,
                "p50_us": 30.0,
                "p95_us": 80.0,
                "max_us": 80.0,
            },
            "events": [
                {"receive_to_last_trade_finished_us": 999.0},
            ],
        },
    }

    result = module._apply_live_trade_finished_threshold(
        step,
        max_trade_finished_us=100.0,
    )

    assert result["ok"] is True
    assert result["observed_max_receive_to_trade_finished_us"] == 80.0
    assert result["observed_receive_to_trade_finished_summary_us"]["count"] == 9


def test_apply_live_trade_finished_threshold_fails_missing_timing():
    module = _load_module()
    step = {
        "ok": True,
        "json": {
            "events": [
                {"receive_to_last_order_send_started_us": 4.0},
            ],
        },
    }

    result = module._apply_live_trade_finished_threshold(
        step,
        max_trade_finished_us=1_000.0,
    )

    assert result["ok"] is False
    assert result["reason"] == "live_inject_trade_finished_timing_missing"


def test_apply_live_trade_finished_threshold_fails_slow_event():
    module = _load_module()
    step = {
        "ok": True,
        "json": {
            "events": [
                {"receive_to_last_trade_finished_us": 1_250.0},
            ],
        },
    }

    result = module._apply_live_trade_finished_threshold(
        step,
        max_trade_finished_us=1_000.0,
    )

    assert result["ok"] is False
    assert result["reason"] == "live_inject_trade_finished_threshold_exceeded"


def test_bybit_clock_step_is_only_added_when_requested(monkeypatch):
    module = _load_module()
    calls = []

    monkeypatch.setattr(
        module,
        "_step",
        lambda name, cmd, timeout, required=True: calls.append(name) or {
            "name": name,
            "ok": True,
            "required": required,
        },
    )
    monkeypatch.setattr(module, "_apply_benchmark_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module, "_apply_type_filter_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module, "_apply_tdlib_fire_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module.sys, "argv", ["fast_readiness_gate.py", "--iterations", "1"])

    assert module.main() == 0
    assert "bybit_clock_check" not in calls
    assert "tdlib_message_buy_preflight_long_body_benchmark" in calls
    assert "tdlib_message_fire_and_forget_benchmark" in calls


def test_require_bybit_clock_adds_required_clock_step(monkeypatch):
    module = _load_module()
    captured = {}

    def fake_step(name, cmd, timeout, required=True):
        captured[name] = {
            "cmd": cmd,
            "required": required,
        }
        return {
            "name": name,
            "ok": True,
            "required": required,
        }

    monkeypatch.setattr(module, "_step", fake_step)
    monkeypatch.setattr(module, "_apply_benchmark_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module, "_apply_type_filter_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module, "_apply_tdlib_fire_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(
        module.sys,
        "argv",
        ["fast_readiness_gate.py", "--iterations", "1", "--require-bybit-clock"],
    )

    assert module.main() == 0
    assert captured["bybit_clock_check"]["required"] is True
    assert captured["bybit_clock_check"]["cmd"][-2:] == ["bin/bybit_clock_gate.py", "check"]


def test_strict_live_tdlib_enables_required_live_startup_gates(monkeypatch):
    module = _load_module()
    captured = {}

    def fake_step(name, cmd, timeout, required=True):
        captured[name] = {
            "cmd": cmd,
            "required": required,
        }
        return {
            "name": name,
            "ok": True,
            "required": required,
        }

    monkeypatch.setattr(module, "_step", fake_step)
    monkeypatch.setattr(module, "_apply_benchmark_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module, "_apply_type_filter_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module, "_apply_tdlib_fire_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module, "_apply_live_inject_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(
        module.sys,
        "argv",
        ["fast_readiness_gate.py", "--iterations", "1", "--strict-live-tdlib"],
    )

    assert module.main() == 0
    assert captured["trading_config_check"]["required"] is True
    assert captured["listing_classifier_fixture_check"]["required"] is True
    assert captured["listing_classifier_fixture_check"]["cmd"][-1] == "--require-tdlib-relay"
    assert captured["tdlib_symbol_cache_refresh"]["required"] is True
    assert captured["tdlib_symbol_cache_refresh"]["cmd"][-2:] == [
        "bin/tdlib_symbol_cache.py",
        "refresh",
    ]
    assert captured["tdlib_symbol_cache_check"]["required"] is True
    assert captured["native_async_fire_and_forget_reclaim_selftest"]["required"] is True
    assert captured["native_async_fire_and_forget_reclaim_selftest"]["cmd"] == [
        "./bin/tdlib_json_relay",
        "--self-test-native-async-fire-and-forget-reclaim",
    ]
    assert captured["native_async_ticker_copy_selftest"]["required"] is True
    assert captured["native_async_ticker_copy_selftest"]["cmd"] == [
        "./bin/tdlib_json_relay",
        "--self-test-native-async-ticker-copy",
    ]
    assert captured["tdlib_message_fire_and_forget_multi_benchmark"]["required"] is True
    assert captured["tdlib_message_fire_and_forget_multi_benchmark"]["cmd"][:2] == [
        "./bin/tdlib_json_relay",
        "--benchmark-tdlib-message-fire-and-forget-multi",
    ]
    assert captured["bybit_clock_check"]["required"] is True
    assert captured["native_file_order_inject"]["required"] is True
    assert "9" in captured["native_file_order_inject"]["cmd"]
    assert "race_fallback_warmup" not in captured


def test_strict_live_race_also_requires_race_fallback_warmup(monkeypatch):
    module = _load_module()
    captured = {}

    def fake_step(name, cmd, timeout, required=True):
        captured[name] = {
            "cmd": cmd,
            "required": required,
        }
        return {
            "name": name,
            "ok": True,
            "required": required,
        }

    monkeypatch.setattr(module, "_step", fake_step)
    monkeypatch.setattr(module, "_apply_benchmark_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module, "_apply_type_filter_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module, "_apply_tdlib_fire_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(module, "_apply_live_inject_threshold", lambda step, **_kwargs: step)
    monkeypatch.setattr(
        module.sys,
        "argv",
        ["fast_readiness_gate.py", "--iterations", "1", "--strict-live-race"],
    )

    assert module.main() == 0
    assert captured["trading_config_check"]["required"] is True
    assert captured["listing_classifier_fixture_check"]["required"] is True
    assert captured["tdlib_symbol_cache_refresh"]["required"] is True
    assert captured["tdlib_symbol_cache_check"]["required"] is True
    assert captured["bybit_clock_check"]["required"] is True
    assert captured["native_file_order_inject"]["required"] is True
    assert captured["race_fallback_warmup"]["required"] is True
