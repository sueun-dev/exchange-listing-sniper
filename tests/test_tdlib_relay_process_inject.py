from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest


MODULE_DIR = Path(__file__).resolve().parents[1]
RELAY_PATH = MODULE_DIR / "bin" / "tdlib_json_relay"
CASES_PATH = MODULE_DIR / "tests" / "fixtures" / "listing_title_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _case_id(case: dict) -> str:
    return case["id"]


def _read_until(proc: subprocess.Popen[str], marker: str, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line)
        if marker in line:
            return line
    raise AssertionError(f"marker {marker!r} not seen; output={''.join(lines)!r}")


def test_relay_process_native_order_file_scheme_selftest_executes_order_path():
    if not RELAY_PATH.exists():
        pytest.skip(f"TDLib relay binary missing: {RELAY_PATH}")

    completed = subprocess.run(
        [str(RELAY_PATH), "--self-test-native-order-file-scheme"],
        cwd=str(MODULE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    assert "SELFTEST_NATIVE_ORDER_FILE_SCHEME_OK" in completed.stdout
    _, raw_json = completed.stdout.strip().split(" ", 1)
    trade = json.loads(raw_json)
    assert trade["attempted"] is True
    assert trade["executed"] is True
    assert trade["order_id"] == "file-order-1"
    assert trade["trade_started_monotonic_ns"] > 0
    assert trade["order_send_started_monotonic_ns"] > 0
    assert trade["trade_finished_monotonic_ns"] > 0
    assert (
        trade["trade_started_monotonic_ns"]
        <= trade["order_send_started_monotonic_ns"]
        <= trade["trade_finished_monotonic_ns"]
    )
    assert trade["order_prepare_elapsed_ns"] == (
        trade["order_send_started_monotonic_ns"]
        - trade["trade_started_monotonic_ns"]
    )


def test_relay_process_native_invalid_quote_amount_selftest_blocks_order_path():
    if not RELAY_PATH.exists():
        pytest.skip(f"TDLib relay binary missing: {RELAY_PATH}")

    completed = subprocess.run(
        [str(RELAY_PATH), "--self-test-native-invalid-quote-amount"],
        cwd=str(MODULE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    assert "SELFTEST_NATIVE_INVALID_QUOTE_AMOUNT_OK" in completed.stdout
    _, raw_json = completed.stdout.strip().split(" ", 1)
    trade = json.loads(raw_json)
    assert trade["attempted"] is False
    assert trade["executed"] is False
    assert trade["reason"] == "quote_amount_invalid"
    assert trade["symbol"] == "STRKUSDT"


def test_relay_process_native_message_dedup_selftest_blocks_second_order():
    if not RELAY_PATH.exists():
        pytest.skip(f"TDLib relay binary missing: {RELAY_PATH}")

    completed = subprocess.run(
        [str(RELAY_PATH), "--self-test-native-message-dedup"],
        cwd=str(MODULE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    assert "SELFTEST_NATIVE_MESSAGE_DEDUP_OK" in completed.stdout


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_relay_process_cli_classifier_matches_golden_cases(case):
    if not RELAY_PATH.exists():
        pytest.skip(f"TDLib relay binary missing: {RELAY_PATH}")

    completed = subprocess.run(
        [
            str(RELAY_PATH),
            "--classify-title",
            case["exchange"],
            case["title"],
        ],
        cwd=str(MODULE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    payload = json.loads(completed.stdout)
    expected = case["expected"]
    if expected is None:
        assert payload == {"matched": False}
        return

    assert payload["matched"] is True
    assert payload["signal_type"] == expected["signal_type"]
    assert payload["ticker"] == expected["ticker"]
    assert payload["tickers"] == expected["tickers"]
    assert payload["asset_name"] == expected["asset_name"]
    assert payload["markets"] == expected["markets"]


def test_relay_process_selftest_update_emits_native_listing_match():
    if not RELAY_PATH.exists():
        pytest.skip(f"TDLib relay binary missing: {RELAY_PATH}")

    proc = subprocess.Popen(
        [str(RELAY_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(MODULE_DIR),
    )
    try:
        assert proc.stdin is not None
        ready = proc.stdout.readline().strip() if proc.stdout is not None else ""
        assert ready == "__relay_ready__"

        proc.stdin.write("__watch_chats__\t777001:BithumbExchange\n")
        proc.stdin.write("__native_listing_on__\n")
        payload = (
            '{"@type":"updateNewMessage","message":{"@type":"message",'
            '"id":321987,"chat_id":777001,"date":1778680000,'
            '"content":{"@type":"messageText","text":{"@type":"formattedText",'
            '"text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내\\n본문은 주문 전 판정에 필요 없음",'
            '"entities":[]}}}}'
        )
        proc.stdin.write(f"__selftest_update__\t{payload}\n")
        proc.stdin.flush()

        line = _read_until(proc, '"@type":"listingMatched"')
        prefix_ns, raw_json = line.split("\t", 1)
        event = json.loads(raw_json)
        assert int(prefix_ns) == event["relay_received_monotonic_ns"]
        assert event["relay_received_monotonic_ns"] > 0
        assert event["channel_handle"] == "BithumbExchange"
        assert "exchange" not in event
        assert "signal_type" not in event
        assert event["message_id"] == 321987
        assert event["title"] == "[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내"
        assert event["ticker"] == "STRK"
        assert event["tickers"] == ["STRK"]
        assert "markets" not in event
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.write("__quit__\n")
                proc.stdin.flush()
            except BrokenPipeError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_relay_process_selftest_update_reaches_native_buy_preflight():
    if not RELAY_PATH.exists():
        pytest.skip(f"TDLib relay binary missing: {RELAY_PATH}")

    env = {
        **os.environ,
        "BYBIT_SPOT_BUY_ENABLED": "1",
        "BYBIT_API_KEY": "test-key",
        "BYBIT_API_SECRET": "test-secret",
        "BYBIT_SPOT_BUY_USDT_AMOUNT": "5",
        "LISTING_TDLIB_NATIVE_TIMING_ENABLED": "1",
    }
    proc = subprocess.Popen(
        [str(RELAY_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(MODULE_DIR),
        env=env,
    )
    try:
        assert proc.stdin is not None
        ready = proc.stdout.readline().strip() if proc.stdout is not None else ""
        assert ready == "__relay_ready__"

        proc.stdin.write("__watch_chats__\t777001:BithumbExchange\n")
        proc.stdin.write("__selftest_native_preflight_on__\tSTRKUSDT\n")
        proc.stdin.flush()
        _read_until(proc, "__selftest_native_preflight_status__")

        payload = (
            '{"@type":"updateNewMessage","message":{"@type":"message",'
            '"id":321987,"chat_id":777001,"date":1778680000,'
            '"content":{"@type":"messageText","text":{"@type":"formattedText",'
            '"text":"[마켓 추가] 스타크넷(STRK) 원화 마켓 추가 및 재단 에어드랍 안내",'
            '"entities":[]}}}}'
        )
        proc.stdin.write(f"__selftest_update__\t{payload}\n")
        proc.stdin.flush()

        line = _read_until(proc, '"@type":"listingMatched"')
        prefix_ns, raw_json = line.split("\t", 1)
        event = json.loads(raw_json)
        assert "native_trade" not in event
        assert len(event["native_trades"]) == 1
        trade = event["native_trades"][0]
        assert int(prefix_ns) == event["relay_received_monotonic_ns"]
        assert event["relay_received_monotonic_ns"] > 0
        assert event["ticker"] == "STRK"
        assert event["tickers"] == ["STRK"]
        assert trade["enabled"] is True
        assert trade["attempted"] is True
        assert trade["executed"] is False
        assert trade["ret_code"] == 0
        assert trade["reason"] == "tdlib_native_rest_preflight"
        assert trade["symbol"] == "STRKUSDT"
        assert trade["order_link_id"] == "ls-b-321987-STRK"
        assert event["relay_received_monotonic_ns"] <= trade["trade_started_monotonic_ns"]
        assert trade["trade_started_monotonic_ns"] <= trade["trade_finished_monotonic_ns"]
        assert "trade_elapsed_ns" not in trade
        assert "order_prepare_elapsed_ns" not in trade
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.write("__quit__\n")
                proc.stdin.flush()
            except BrokenPipeError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
