from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


MODULE_DIR = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = MODULE_DIR / "bin" / "benchmark_live_ingest.py"


def _load_benchmark_module():
    spec = importlib.util.spec_from_file_location("benchmark_live_ingest", BENCHMARK_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expanded_test_cases_repeats_with_unique_message_ids():
    module = _load_benchmark_module()

    cases = [
        {"message_id": 100, "title": "a"},
        {"message_id": 101, "title": "b"},
        {"message_id": 102, "title": "c"},
    ]

    expanded = module._expanded_test_cases(cases, 8)

    assert [item["message_id"] for item in expanded] == [
        100,
        101,
        102,
        1100,
        1101,
        1102,
        2100,
        2101,
    ]
    assert [item["title"] for item in expanded] == ["a", "b", "c", "a", "b", "c", "a", "b"]
    assert cases[0]["message_id"] == 100


def test_event_latency_summaries_report_distribution_fields():
    module = _load_benchmark_module()

    events = [
        {
            "receive_to_last_order_send_started_us": 2.0,
            "receive_to_last_trade_finished_us": 8.0,
            "inject_to_listing_matched_us": 40.0,
        },
        {
            "receive_to_last_order_send_started_us": 5.0,
            "receive_to_last_trade_finished_us": 12.0,
            "inject_to_listing_matched_us": 30.0,
        },
        {
            "receive_to_last_order_send_started_us": 3.0,
            "receive_to_last_trade_finished_us": 9.0,
            "inject_to_listing_matched_us": 35.0,
        },
    ]

    summaries = module._event_latency_summaries(events)

    assert summaries["receive_to_last_order_send_started_us_summary"] == {
        "count": 3,
        "min_us": 2.0,
        "p50_us": 3.0,
        "p95_us": 5.0,
        "max_us": 5.0,
        "avg_us": 3.333,
    }
    assert summaries["receive_to_last_trade_finished_us_summary"]["max_us"] == 12.0
    assert summaries["inject_to_listing_matched_us_summary"]["p50_us"] == 35.0


def test_native_local_order_inject_reports_mock_server_start_failure(monkeypatch):
    module = _load_benchmark_module()

    class FakeTdlibClient:
        relay_path = MODULE_DIR / "bin" / "tdlib_json_relay"

        def is_configured(self):
            return True

        def has_session_file(self):
            return True

    def fail_to_start_server(_symbols):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(module, "TdlibRealtimeChannelClient", FakeTdlibClient)
    monkeypatch.setattr(module, "_start_mock_bybit_server", fail_to_start_server)

    payload = asyncio.run(
        module._run_native_local_order_inject(
            SimpleNamespace(timeout=10.0, iterations=3, channel=["upbit_news", "BithumbExchange"])
        )
    )

    assert payload == {
        "ok": False,
        "backend": "tdlib",
        "mode": "tdlib_native_local_order_inject",
        "reason": "mock_bybit_server_start_failed",
        "error": "[Errno 1] Operation not permitted",
    }
