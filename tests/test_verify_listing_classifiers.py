from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = MODULE_DIR / "bin" / "verify_listing_classifiers.py"


def test_verify_listing_classifiers_passes_current_fixture_and_relay():
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--require-tdlib-relay",
        ],
        cwd=str(MODULE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    payload = json.loads(completed.stdout[completed.stdout.find("{") :])
    assert payload["ok"] is True
    assert [step["name"] for step in payload["steps"]] == [
        "python_classifier_fixture",
        "default_classifier_fixture",
        "tdlib_relay_cli_fixture",
    ]


def test_verify_listing_classifiers_fails_when_required_relay_is_missing(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--require-tdlib-relay",
            "--relay-path",
            str(tmp_path / "missing-relay"),
            "--skip-default",
        ],
        cwd=str(MODULE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2, completed.stdout
    payload = json.loads(completed.stdout[completed.stdout.find("{") :])
    assert payload["ok"] is False
    assert payload["steps"][-1]["name"] == "tdlib_relay_cli_fixture"
    assert payload["steps"][-1]["required"] is True
