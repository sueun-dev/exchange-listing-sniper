"""Bridge to the latency-critical C++ Bybit fast path process."""

from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import threading
from pathlib import Path

from .env_loader import MODULE_DIR, load_env_settings

logger = logging.getLogger(__name__)

DEFAULT_BINARY = MODULE_DIR / "bin" / "bybit_fast_path"
DEFAULT_BUILD_SCRIPT = MODULE_DIR / "cpp" / "build_fast_path.sh"


def _encode_frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def _is_truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


class CppFastBuyerBridge:
    """Persistent subprocess bridge for the C++ fast order executor."""

    def __init__(
        self,
        enabled: bool | None = None,
        binary_path: str | Path | None = None,
        build_script: str | Path | None = None,
        auto_build: bool | None = None,
    ):
        settings = load_env_settings(
            {
                "BYBIT_FAST_EXECUTOR_ENABLED",
                "BYBIT_FAST_EXECUTOR_PATH",
                "BYBIT_FAST_EXECUTOR_BUILD_SCRIPT",
                "BYBIT_FAST_EXECUTOR_AUTO_BUILD",
            }
        )
        self.enabled = (
            _is_truthy(settings.get("BYBIT_FAST_EXECUTOR_ENABLED"))
            if enabled is None
            else bool(enabled)
        )
        self.binary_path = Path(
            binary_path
            or settings.get("BYBIT_FAST_EXECUTOR_PATH")
            or DEFAULT_BINARY
        )
        self.build_script = Path(
            build_script
            or settings.get("BYBIT_FAST_EXECUTOR_BUILD_SCRIPT")
            or DEFAULT_BUILD_SCRIPT
        )
        self.auto_build = (
            _is_truthy(settings.get("BYBIT_FAST_EXECUTOR_AUTO_BUILD"))
            if auto_build is None
            else bool(auto_build)
        )
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._ping_frame = _encode_frame(b"PING")
        self._refresh_frame = _encode_frame(b"REFRESH")
        self._keepwarm_frame = _encode_frame(b"KEEPWARM")
        self._warmed_once = False

    def is_enabled(self) -> bool:
        return self.enabled

    def warmup(self):
        if not self.enabled:
            return
        with self._lock:
            self._ensure_process()
            if self._warmed_once:
                response = self._request_locked(self._keepwarm_frame)
            else:
                response = self._request_locked(self._refresh_frame)
                self._warmed_once = True
        return self._parse_warmup_response(response)

    def ping(self) -> dict:
        with self._lock:
            self._ensure_process()
            response = self._request_locked(self._ping_frame)
        return self._parse_ping_response(response)

    def buy_market(self, *, symbol: str, quote_amount: float, order_link_id: str) -> dict:
        return self.buy_market_quote_text(
            symbol=symbol,
            quote_amount_text=f"{quote_amount:g}",
            order_link_id=order_link_id,
        )

    def buy_market_quote_text(
        self,
        *,
        symbol: str,
        quote_amount_text: str,
        order_link_id: str,
    ) -> dict:
        with self._lock:
            self._ensure_process()
            response = self._request_locked(
                _encode_frame(
                    f"BUY\t{symbol}\t{quote_amount_text}\t{order_link_id}".encode()
                )
            )
        payload = self._parse_buy_response(response)
        payload.setdefault("symbol", symbol)
        payload.setdefault("attempted", False)
        payload.setdefault("executed", False)
        try:
            payload["requested_usdt"] = float(quote_amount_text)
        except ValueError:
            payload["requested_usdt"] = 0.0
        payload["transport"] = payload.get("transport", "cpp_fast_path")
        return payload

    def buy_markets_quote_text(
        self,
        *,
        orders: list[tuple[str, str]],
        quote_amount_text: str,
    ) -> list[dict]:
        if not orders:
            return []
        if len(orders) == 1:
            symbol, order_link_id = orders[0]
            return [
                self.buy_market_quote_text(
                    symbol=symbol,
                    quote_amount_text=quote_amount_text,
                    order_link_id=order_link_id,
                )
            ]
        payload_parts = ["BUYBULK", quote_amount_text]
        for symbol, order_link_id in orders:
            payload_parts.extend((symbol, order_link_id))
        with self._lock:
            self._ensure_process()
            response = self._request_locked(
                _encode_frame("\t".join(payload_parts).encode("utf-8"))
            )
        return self._parse_bulk_buy_response(response, orders, quote_amount_text)

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
                raise RuntimeError(
                    f"C++ fast executor not found: {self.binary_path}"
                )
            self._build_binary()

        env = os.environ.copy()
        env.update(
            load_env_settings(
                {
                    "BYBIT_API_KEY",
                    "BYBIT_API_SECRET",
                    "BYBIT_API_BASE_URL",
                    "BYBIT_RECV_WINDOW",
                    "BYBIT_SPOT_BUY_ENABLED",
                    "BYBIT_SPOT_BUY_USDT_AMOUNT",
                    "BYBIT_FAST_ORDER_ON_CACHE_MISS",
                    "BYBIT_TIMESTAMP_BIAS_MS",
                }
            )
        )
        self._proc = subprocess.Popen(
            [str(self.binary_path), "--server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=str(MODULE_DIR),
            env=env,
        )
        logger.info("C++ fast executor started: %s", self.binary_path)

    def _build_binary(self):
        if not self.build_script.exists():
            raise RuntimeError(f"Build script not found: {self.build_script}")
        subprocess.run(
            [str(self.build_script)],
            cwd=str(MODULE_DIR),
            check=True,
            text=True,
        )

    def _request_locked(self, frame: bytes) -> bytes:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("C++ fast executor is not running")
        self._proc.stdin.write(frame)
        self._proc.stdin.flush()
        header = self._read_exact_locked(4)
        if not header:
            stderr_text = ""
            if self._proc.poll() is not None and self._proc.stderr is not None:
                try:
                    stderr_text = self._proc.stderr.read().decode("utf-8", errors="ignore")
                except Exception:
                    stderr_text = ""
            raise RuntimeError(
                f"C++ fast executor returned no response. stderr={stderr_text}"
            )
        size = struct.unpack(">I", header)[0]
        if size == 0:
            return b""
        response = self._read_exact_locked(size)
        if len(response) != size:
            raise RuntimeError("C++ fast executor returned truncated response")
        return response

    def _read_exact_locked(self, size: int) -> bytes:
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("C++ fast executor is not running")
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = self._proc.stdout.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse_ping_response(response: bytes) -> dict:
        if response == b"PONG":
            return {"ok": True, "pong": True}
        return json.loads(response.decode("utf-8"))

    @staticmethod
    def _parse_refresh_response(response: bytes) -> dict:
        if response.startswith(b"REFRESH\t"):
            parts = response.split(b"\t", 2)
            return {
                "ok": len(parts) >= 2 and parts[1] == b"1",
                "symbol_count": int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0,
            }
        return json.loads(response.decode("utf-8"))

    @staticmethod
    def _parse_warmup_response(response: bytes) -> dict:
        if response.startswith(b"KEEPWARM\t"):
            parts = response.split(b"\t", 2)
            return {
                "ok": len(parts) >= 2 and parts[1] == b"1",
                "scheduled": True,
                "symbol_count": int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0,
            }
        return CppFastBuyerBridge._parse_refresh_response(response)

    @staticmethod
    def _parse_buy_response(response: bytes) -> dict:
        if not response.startswith(b"BUY\t"):
            return json.loads(response.decode("utf-8"))
        return CppFastBuyerBridge._parse_buy_response_line(response)

    @staticmethod
    def _parse_buy_response_line(line: bytes) -> dict:
        parts = line.split(b"\t", 7)
        if len(parts) < 8:
            raise RuntimeError(
                f"Malformed C++ fast executor response: {line!r}"
            )

        _, executed, attempted, symbol, order_id, ret_code, transport, reason = parts
        payload = {
            "executed": executed == b"1",
            "attempted": attempted == b"1",
            "symbol": symbol.decode("utf-8", errors="ignore"),
            "order_id": order_id.decode("utf-8", errors="ignore"),
            "ret_code": int(ret_code or b"-1"),
            "transport": transport.decode("utf-8", errors="ignore") or "cpp_fast_path",
        }
        if reason:
            payload["reason"] = reason.decode("utf-8", errors="ignore")
        return payload

    @staticmethod
    def _parse_bulk_buy_response(
        response: bytes,
        orders: list[tuple[str, str]],
        quote_amount_text: str,
    ) -> list[dict]:
        if not response.startswith(b"BULK\t"):
            payload = json.loads(response.decode("utf-8"))
            return [payload]
        lines = response.split(b"\n")
        try:
            expected_count = int(lines[0].split(b"\t", 1)[1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"Malformed C++ fast executor bulk response: {response!r}") from exc

        payloads: list[dict] = []
        for index, line in enumerate(lines[1:1 + expected_count]):
            payload = CppFastBuyerBridge._parse_buy_response_line(line)
            if index < len(orders):
                symbol, _ = orders[index]
                payload.setdefault("symbol", symbol)
            payload.setdefault("attempted", False)
            payload.setdefault("executed", False)
            try:
                payload["requested_usdt"] = float(quote_amount_text)
            except ValueError:
                payload["requested_usdt"] = 0.0
            payload["transport"] = payload.get("transport", "cpp_fast_path")
            payloads.append(payload)
        return payloads
