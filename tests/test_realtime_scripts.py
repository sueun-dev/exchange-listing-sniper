from __future__ import annotations

from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]


def _script_text(name: str) -> str:
    return (MODULE_DIR / "bin" / name).read_text(encoding="utf-8")


def test_buy_realtime_scripts_run_classifier_fixture_gate_before_startup():
    for name in (
        "run_tdlib_native_buy_realtime.sh",
        "run_race_native_buy_realtime.sh",
        "run_fast_buy_realtime.sh",
        "run_fast_buy_cpp_realtime.sh",
    ):
        text = _script_text(name)
        assert "LISTING_CLASSIFIER_VERIFY" in text
        assert "verify_listing_classifiers.py\" --require-tdlib-relay" in text
        assert "refusing to start" in text


def test_readiness_script_runs_classifier_fixture_gate_before_preflight():
    text = _script_text("check_tdlib_native_buy_realtime.sh")

    assert "LISTING_CLASSIFIER_VERIFY" in text
    assert "verify_listing_classifiers.py\" --require-tdlib-relay" in text
    assert "refusing to run readiness gate" in text


def test_source_first_script_keeps_classifier_gate_out_of_source_only_path():
    text = _script_text("run_source_first_realtime.sh")

    assert "--source-only" in text
    assert "verify_listing_classifiers.py" not in text
    assert "LISTING_CLASSIFIER_VERIFY" not in text
