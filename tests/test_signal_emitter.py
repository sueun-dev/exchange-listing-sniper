from __future__ import annotations

import json
import sys
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
for module_name in list(sys.modules):
    if module_name == "src" or module_name.startswith("src."):
        sys.modules.pop(module_name, None)

from src.signal_emitter import SignalEmitter  # noqa: E402


def test_persist_trade_proof_writes_minimal_jsonl(tmp_path):
    emitter = SignalEmitter(
        signal_dir=tmp_path / "signals",
        trade_proof_dir=tmp_path / "trade_proofs",
    )

    out_path = emitter.persist_trade_proof(
        post={
            "channel_handle": "BithumbExchange",
            "message_id": 921989,
            "title": "[마켓 추가] 센티언트(SENT), 헤이엘사(ELSA) 원화 마켓 추가",
            "published_at": "2026-06-01T00:00:00+00:00",
            "received_monotonic_ns": 1000,
        },
        listing={
            "exchange": "bithumb",
            "ticker": "SENT",
            "asset_name": "센티언트",
            "markets": ["KRW"],
        },
        trade={
            "attempted": True,
            "executed": False,
            "symbol": "SENTUSDT",
            "order_link_id": "ls-b-921989-SENT",
            "transport": "tdlib_native_rest",
            "reason": "tdlib_native_rest_preflight",
            "order_send_started_monotonic_ns": 1800,
            "trade_finished_monotonic_ns": 2500,
        },
    )

    assert out_path.parent == tmp_path / "trade_proofs"
    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["exchange"] == "bithumb"
    assert rows[0]["ticker"] == "SENT"
    assert rows[0]["trade"]["symbol"] == "SENTUSDT"
    assert rows[0]["receive_to_order_send_started_ns"] == 800
    assert rows[0]["receive_to_trade_finished_ns"] == 1500
