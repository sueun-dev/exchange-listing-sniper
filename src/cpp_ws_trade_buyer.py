from __future__ import annotations

"""Bridge to the latency-critical C++ Bybit trade WebSocket executor."""

import json
import os
import subprocess
import threading
from pathlib import Path

from .env_loader import MODULE_DIR, load_env_settings

DEFAULT_BINARY = MODULE_DIR / "bin" / "bybit_ws_trade_path"
DEFAULT_BUILD_SCRIPT = MODULE_DIR / "cpp" / "build_ws_trade_path.sh"


def _is_truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


class CppWsTradeBuyerBridge:
    """Persistent subprocess bridge for the C++ trade WebSocket executor."""

    def __init__(
        self,
        enabled: bool | None = None,
        binary_path: str | Path | None = None,
        build_script: str | Path | None = None,
        auto_build: bool | None = None,
    ):
        settings = load_env_settings(
            {
                "BYBIT_CPP_WS_EXECUTOR_ENABLED",
                "BYBIT_CPP_WS_EXECUTOR_PATH",
                "BYBIT_CPP_WS_EXECUTOR_BUILD_SCRIPT",
                "BYBIT_CPP_WS_EXECUTOR_AUTO_BUILD",
                "BYBIT_WS_TRADE_URL",
                "BYBIT_CPP_WS_INSECURE_SKIP_VERIFY",
            }
        )
        self.enabled = (
            _is_truthy(settings.get("BYBIT_CPP_WS_EXECUTOR_ENABLED"))
            if enabled is None
            else bool(enabled)
        )
        self.binary_path = Path(
            binary_path
            or settings.get("BYBIT_CPP_WS_EXECUTOR_PATH")
            or DEFAULT_BINARY
        )
        self.build_script = Path(
            build_script
            or settings.get("BYBIT_CPP_WS_EXECUTOR_BUILD_SCRIPT")
            or DEFAULT_BUILD_SCRIPT
        )
        self.auto_build = (
            _is_truthy(settings.get("BYBIT_CPP_WS_EXECUTOR_AUTO_BUILD", "1"))
            if auto_build is None
            else bool(auto_build)
        )
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None

    def is_enabled(self) -> bool:
        return self.enabled

    def warmup(self):
        if not self.enabled:
            return
        with self._lock:
            self._ensure_process()
            self._request_locked("WARMUP")

    def ping(self) -> dict:
        with self._lock:
            self._ensure_process()
            response = self._request_locked("PING")
        return json.loads(response)

    def buy_market(
        self,
        *,
        symbol: str,
        qty: str,
        market_unit: str,
        order_link_id: str,
    ) -> dict:
        with self._lock:
            self._ensure_process()
            response = self._request_locked(
                f"BUY\t{symbol}\t{qty}\t{market_unit}\t{order_link_id}"
            )
        payload = json.loads(response)
        payload.setdefault("symbol", symbol)
        payload.setdefault("attempted", False)
        payload.setdefault("executed", False)
        payload.setdefault("transport", "cpp_ws_trade")
        return payload

    def sell_market(
        self,
        *,
        symbol: str,
        qty: str,
        order_link_id: str,
    ) -> dict:
        with self._lock:
            self._ensure_process()
            response = self._request_locked(
                f"SELL\t{symbol}\t{qty}\t{order_link_id}"
            )
        payload = json.loads(response)
        payload.setdefault("symbol", symbol)
        payload.setdefault("attempted", False)
        payload.setdefault("executed", False)
        payload.setdefault("transport", "cpp_ws_trade")
        return payload

    def close(self):
        with self._lock:
            if self._proc is None:
                return
            proc = self._proc
            self._proc = None
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                proc.kill()

    def _ensure_process(self):
        if self._proc is not None and self._proc.poll() is None:
            return

        if not self.binary_path.exists():
            if not self.auto_build:
                raise RuntimeError(f"C++ WS executor not found: {self.binary_path}")
            self._build_binary()

        env = os.environ.copy()
        env.update(
            load_env_settings(
                {
                    "BYBIT_API_KEY",
                    "BYBIT_API_SECRET",
                    "BYBIT_API_BASE_URL",
                    "BYBIT_RECV_WINDOW",
                    "BYBIT_WS_TRADE_URL",
                    "BYBIT_CPP_WS_INSECURE_SKIP_VERIFY",
                    "BYBIT_TIMESTAMP_BIAS_MS",
                }
            )
        )
        self._proc = subprocess.Popen(
            [str(self.binary_path), "--server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(MODULE_DIR),
            env=env,
        )

    def _build_binary(self):
        if not self.build_script.exists():
            raise RuntimeError(f"Build script not found: {self.build_script}")
        subprocess.run(
            [str(self.build_script)],
            cwd=str(MODULE_DIR),
            check=True,
            text=True,
        )

    def _request_locked(self, line: str) -> str:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("C++ WS executor is not running")
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()
        response = self._proc.stdout.readline().strip()
        if response:
            return response
        stderr_text = ""
        if self._proc.stderr is not None:
            try:
                stderr_text = self._proc.stderr.read()
            except Exception:
                stderr_text = ""
        raise RuntimeError(f"C++ WS executor returned no response. stderr={stderr_text}")
