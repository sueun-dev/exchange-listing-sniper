from __future__ import annotations

"""Realtime Telegram source using TDLib via a lightweight relay process."""

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue

from .env_loader import MODULE_DIR, load_env_settings
from .payload_normalization import string_list

logger = logging.getLogger(__name__)

DEFAULT_RELAY_PATH = MODULE_DIR / "bin" / "tdlib_json_relay"
DEFAULT_DB_DIR = MODULE_DIR / "data" / "tdlib_source_db"
DEFAULT_WATCH_CHAT_CACHE_PATH = MODULE_DIR / "data" / "tdlib_watch_chats.json"
TDLIB_RELAY_ENV_KEYS = {
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "BYBIT_API_BASE_URL",
    "BYBIT_RECV_WINDOW",
    "BYBIT_TIMESTAMP_BIAS_MS",
    "BYBIT_SPOT_BUY_ENABLED",
    "BYBIT_SPOT_BUY_USDT_AMOUNT",
    "LISTING_BYBIT_HTTP_TIMEOUT_MS",
    "LISTING_BYBIT_ORDER_RESPONSE_TIMEOUT_MS",
    "LISTING_BYBIT_CONNECT_TIMEOUT_MS",
    "LISTING_TDLIB_RECEIVE_TIMEOUT_SEC",
    "LISTING_TDLIB_FLUSH_LISTING_EVENTS",
    "LISTING_TDLIB_EMIT_LISTING_EVENTS",
    "LISTING_TDLIB_DISABLE_QOS_BOOST",
    "LISTING_TDLIB_DISABLE_BACKGROUND_QOS_LOWER",
    "LISTING_TDLIB_NATIVE_TIMING_ENABLED",
    "LISTING_TDLIB_NATIVE_KEEPWARM_INTERVAL",
    "LISTING_TDLIB_NATIVE_SYMBOL_REFRESH_INTERVAL",
    "LISTING_TDLIB_NATIVE_PARALLEL_KEEPWARM_CLIENTS",
    "LISTING_TDLIB_NATIVE_ORDER_CLIENT_KEEPWARM_ENABLED",
    "LISTING_TDLIB_NATIVE_IMMEDIATE_KEEPWARM_REFRESH",
    "LISTING_TDLIB_NATIVE_BLOCKING_HOT_ORDER_WARMUP",
    "LISTING_TDLIB_NATIVE_ASYNC_ORDER_DISPATCH",
    "LISTING_TDLIB_NATIVE_WORKER_SPIN_WAIT",
    "LISTING_TDLIB_NATIVE_WORKER_SPIN_COUNT",
    "LISTING_TDLIB_NATIVE_ORDER_START_SPIN_COUNT",
    "LISTING_TDLIB_NATIVE_ORDER_ON_CACHE_MISS",
    "LISTING_TDLIB_NATIVE_SYMBOL_CACHE_PATH",
    "LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MAX_AGE_SEC",
    "LISTING_TDLIB_NATIVE_SYMBOL_CACHE_MIN_COUNT",
}


def _is_truthy(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _handle_key(handle: str) -> str:
    return handle.strip().lstrip("@").casefold()


def _parse_watch_chat_ids(value: str | None) -> dict[str, int]:
    """Parse optional TDLib chat-id cache.

    Supported forms:
    - -100123:upbit_news,-100456:BithumbExchange
    - upbit_news=-100123,BithumbExchange=-100456
    """
    if not value:
        return {}

    chat_ids: dict[str, int] = {}
    for raw_item in value.replace(";", ",").split(","):
        item = raw_item.strip()
        if not item:
            continue

        if "=" in item:
            handle, chat_id_text = item.split("=", 1)
        elif ":" in item:
            left, right = item.split(":", 1)
            if left.strip().lstrip("-").isdigit():
                chat_id_text, handle = left, right
            else:
                handle, chat_id_text = left, right
        else:
            logger.warning("Ignoring invalid LISTING_TDLIB_WATCH_CHATS item: %s", item)
            continue

        key = _handle_key(handle)
        if not key:
            logger.warning("Ignoring LISTING_TDLIB_WATCH_CHATS item with empty handle: %s", item)
            continue

        try:
            chat_ids[key] = int(chat_id_text.strip())
        except ValueError:
            logger.warning("Ignoring LISTING_TDLIB_WATCH_CHATS item with invalid chat id: %s", item)
    return chat_ids


def _native_listing_defaults(channel_handle: str) -> tuple[str, str]:
    normalized = _handle_key(channel_handle)
    if normalized == "upbit_news":
        return "upbit", "new_listing"
    if normalized == "bithumbexchange":
        return "bithumb", "market_add"
    return "", ""


def _build_listing_matched_post(
    *,
    payload: dict,
    event_received_monotonic_ns: int,
    clock_offset_ns: int,
) -> dict:
    relay_received_monotonic_ns = int(
        payload.get(
            "relay_received_monotonic_ns",
            event_received_monotonic_ns,
        )
    )
    received_python_monotonic_ns = relay_received_monotonic_ns - clock_offset_ns
    published_at = datetime.fromtimestamp(
        int(payload.get("published_at_unix", 0)),
        tz=timezone.utc,
    )
    channel_handle = str(payload["channel_handle"])
    message_id = int(payload["message_id"])
    default_exchange, default_signal_type = _native_listing_defaults(channel_handle)
    ticker = str(payload.get("ticker", ""))
    tickers = string_list(payload.get("tickers"))
    if not tickers and ticker:
        tickers = [ticker]

    native_listing = {
        "exchange": payload.get("exchange") or default_exchange,
        "signal_type": payload.get("signal_type") or default_signal_type,
        "ticker": ticker,
        "tickers": tickers,
        "markets": string_list(payload.get("markets")) or ["KRW"],
    }
    asset_name = payload.get("asset_name")
    if asset_name:
        native_listing["asset_name"] = asset_name

    post = {
        "channel_handle": channel_handle,
        "message_id": message_id,
        "published_at": published_at.isoformat(),
        "received_monotonic_ns": relay_received_monotonic_ns,
        "received_python_monotonic_ns": received_python_monotonic_ns,
        "relay_received_monotonic_ns": relay_received_monotonic_ns,
        "title": payload.get("title", ""),
        "text": payload.get("title", ""),
        "post_url": f"https://t.me/{channel_handle}/{message_id}",
        "native_listing": native_listing,
    }
    native_trade = payload.get("native_trade")
    if isinstance(native_trade, dict):
        post["native_trade"] = native_trade
    native_trades = payload.get("native_trades")
    if isinstance(native_trades, list):
        post["native_trades"] = [
            item for item in native_trades if isinstance(item, dict)
        ]
    return post


def _tdlib_relay_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(load_env_settings(TDLIB_RELAY_ENV_KEYS))
    return env


def _realtime_telegram_client_class():
    from .telegram_realtime_client import RealtimeTelegramChannelClient

    return RealtimeTelegramChannelClient


def _load_watch_chat_cache(path: Path = DEFAULT_WATCH_CHAT_CACHE_PATH) -> dict[str, int]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("TDLib watch chat cache read failed: %s", exc)
        return {}

    if not isinstance(raw, dict):
        logger.warning("Ignoring invalid TDLib watch chat cache: %s", path)
        return {}

    chat_ids: dict[str, int] = {}
    for handle, chat_id in raw.items():
        key = _handle_key(str(handle))
        if not key:
            continue
        try:
            chat_ids[key] = int(chat_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid cached TDLib chat id for %s: %r", handle, chat_id)
    return chat_ids


def _save_watch_chat_cache(chat_ids: dict[str, int], path: Path = DEFAULT_WATCH_CHAT_CACHE_PATH):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(chat_ids, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(path)
    except OSError as exc:
        logger.warning("TDLib watch chat cache write failed: %s", exc)


class _TdlibEvent:
    def __init__(self, received_monotonic_ns: int, payload: dict):
        self.received_monotonic_ns = received_monotonic_ns
        self.payload = payload


class _TdlibRelay:
    def __init__(self, relay_path: Path):
        self.relay_path = relay_path
        self.proc: subprocess.Popen[str] | None = None
        self.queue: Queue[_TdlibEvent] = Queue()
        self.clock_queue: Queue[int] = Queue()
        self.native_status_queue: Queue[dict] = Queue()
        self._reader: threading.Thread | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_queue: asyncio.Queue[_TdlibEvent] | None = None

    def start(self):
        if not self.relay_path.exists():
            raise RuntimeError(f"TDLib relay binary not found: {self.relay_path}")
        self.proc = subprocess.Popen(
            [str(self.relay_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=_tdlib_relay_env(),
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().strip()
        if line != "__relay_ready__":
            raise RuntimeError(f"TDLib relay failed to start: {line}")
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self):
        assert self.proc is not None
        assert self.proc.stdout is not None
        for raw_line in self.proc.stdout:
            line = raw_line.strip()
            if line.startswith("__clock__\t"):
                try:
                    _, value = line.split("\t", 1)
                    self.clock_queue.put(int(value))
                except ValueError:
                    pass
                continue
            if line.startswith("__native_buy_status__\t"):
                try:
                    _, payload = line.split("\t", 1)
                    status = json.loads(payload)
                except (ValueError, json.JSONDecodeError):
                    continue
                self.native_status_queue.put(status)
                if not status.get("active"):
                    logger.info(
                        "TDLib native-buy inactive: %s",
                        status.get("reason", "inactive"),
                    )
                elif status.get("ready"):
                    logger.info("TDLib native-buy ready: %s", status.get("reason", "ready"))
                else:
                    logger.warning(
                        "TDLib native-buy not ready: %s",
                        status.get("reason", "unknown"),
                    )
                continue
            if not line or "\t" not in line:
                continue
            prefix, payload = line.split("\t", 1)
            try:
                event = _TdlibEvent(
                    received_monotonic_ns=int(prefix),
                    payload=json.loads(payload),
                )
            except (ValueError, json.JSONDecodeError):
                continue
            self.queue.put(event)
            if self._async_loop is not None and self._async_queue is not None:
                self._async_loop.call_soon_threadsafe(
                    self._async_queue.put_nowait,
                    event,
                )

    def attach_async_loop(self, loop: asyncio.AbstractEventLoop):
        self._async_loop = loop
        self._async_queue = asyncio.Queue()

    def send(self, obj: dict):
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("TDLib relay not started")
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def send_raw(self, line: str):
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("TDLib relay not started")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def wait_for(self, predicate, timeout: float) -> _TdlibEvent:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("TDLib relay timed out")
            try:
                event = self.queue.get(timeout=remaining)
            except Empty as exc:
                raise TimeoutError("TDLib relay timed out") from exc
            if predicate(event):
                return event

    def send_request(self, obj: dict, timeout: float = 10.0) -> dict:
        extra = f"req-{uuid.uuid4().hex}"
        payload = dict(obj)
        payload["@extra"] = extra
        self.send(payload)
        event = self.wait_for(lambda event: event.payload.get("@extra") == extra, timeout=timeout)
        return event.payload

    async def async_wait_for(self, predicate, timeout: float) -> _TdlibEvent:
        if self._async_queue is None:
            raise RuntimeError("TDLib relay async queue not attached")
        while True:
            event = await asyncio.wait_for(self._async_queue.get(), timeout=timeout)
            if predicate(event):
                return event

    def measure_clock_offset_ns(self, attempts: int = 7) -> int:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("TDLib relay not started")
        samples: list[tuple[int, int]] = []
        for _ in range(attempts):
            start_ns = time.monotonic_ns()
            self.proc.stdin.write("__clock__\n")
            self.proc.stdin.flush()
            try:
                relay_ns = self.clock_queue.get(timeout=5)
            except Empty as exc:
                raise TimeoutError("TDLib relay clock calibration timed out") from exc
            end_ns = time.monotonic_ns()
            midpoint_ns = (start_ns + end_ns) // 2
            samples.append((end_ns - start_ns, relay_ns - midpoint_ns))
        samples.sort(key=lambda item: item[0])
        return samples[0][1]

    def wait_for_native_status(self, timeout: float = 15.0) -> dict:
        return self.native_status_queue.get(timeout=timeout)

    def close(self):
        if self.proc is not None and self.proc.stdin is not None:
            try:
                self.proc.stdin.write("__quit__\n")
                self.proc.stdin.flush()
            except BrokenPipeError:
                pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class TdlibRealtimeChannelClient:
    """Listen to new Telegram messages from target channels in realtime via TDLib."""

    def __init__(
        self,
        api_id: int | None = None,
        api_hash: str | None = None,
        phone: str | None = None,
        relay_path: str | Path | None = None,
        database_dir: str | Path | None = None,
    ):
        settings = load_env_settings(
            {
                "LISTING_SOURCE_TELEGRAM_API_ID",
                "LISTING_SOURCE_TELEGRAM_API_HASH",
                "LISTING_SOURCE_TELEGRAM_PHONE",
            }
        )
        self.api_id = int(api_id or settings.get("LISTING_SOURCE_TELEGRAM_API_ID") or 0)
        self.api_hash = api_hash or settings.get("LISTING_SOURCE_TELEGRAM_API_HASH", "")
        self.phone = phone or settings.get("LISTING_SOURCE_TELEGRAM_PHONE", "")
        self.relay_path = Path(relay_path or DEFAULT_RELAY_PATH)
        self.database_dir = Path(database_dir or DEFAULT_DB_DIR)
        self.database_dir.mkdir(parents=True, exist_ok=True)

    def is_configured(self) -> bool:
        return bool(self.api_id and self.api_hash and self.phone)

    def has_session_file(self) -> bool:
        return self.database_dir.exists() and any(self.database_dir.iterdir())

    def _tdlib_parameter_fields(self) -> dict:
        return {
            "database_directory": str(self.database_dir),
            "files_directory": str(self.database_dir / "files"),
            "database_encryption_key": "",
            "use_message_database": True,
            "use_secret_chats": False,
            "use_chat_info_database": True,
            "use_file_database": False,
            "use_test_dc": False,
            "api_id": int(self.api_id),
            "api_hash": self.api_hash,
            "system_language_code": "en",
            "device_model": "Codex TDLib Source",
            "system_version": "macOS",
            "application_version": "11.7",
            "enable_storage_optimizer": False,
            "ignore_file_names": True,
        }

    def _tdlib_send_auth_parameters(self, relay: _TdlibRelay, *, legacy: bool = False):
        fields = self._tdlib_parameter_fields()
        if legacy:
            fields.pop("database_encryption_key", None)
            relay.send(
                {
                    "@type": "setTdlibParameters",
                    "parameters": {
                        "@type": "tdlibParameters",
                        **fields,
                    },
                }
            )
            return
        relay.send({"@type": "setTdlibParameters", **fields})

    @staticmethod
    def _is_auth_state(payload: dict, name: str) -> bool:
        if payload.get("@type") == name:
            return True
        if payload.get("@type") != "updateAuthorizationState":
            return False
        state = payload.get("authorization_state", {})
        return state.get("@type") == name

    def _ensure_ready(self, relay: _TdlibRelay, interactive: bool):
        sent_parameters = False
        sent_legacy_parameters = False
        sent_phone_number = False
        sent_encryption_key = False

        relay.send({"@type": "getAuthorizationState"})
        while True:
            event = relay.wait_for(lambda _: True, timeout=60)
            payload = event.payload
            if self._is_auth_state(payload, "authorizationStateWaitTdlibParameters"):
                if not sent_parameters:
                    self._tdlib_send_auth_parameters(relay)
                    sent_parameters = True
                continue
            if self._is_auth_state(payload, "authorizationStateWaitEncryptionKey"):
                if not sent_encryption_key:
                    relay.send({"@type": "checkDatabaseEncryptionKey", "encryption_key": ""})
                    sent_encryption_key = True
                continue
            if self._is_auth_state(payload, "authorizationStateWaitPhoneNumber"):
                if not sent_phone_number:
                    relay.send(
                        {
                            "@type": "setAuthenticationPhoneNumber",
                            "phone_number": self.phone,
                        }
                    )
                    sent_phone_number = True
                continue
            if self._is_auth_state(payload, "authorizationStateWaitCode"):
                if not interactive:
                    raise RuntimeError(
                        "TDLib authorization code required. Run `python main.py --login-source-telegram --realtime-backend tdlib` first."
                    )
                code = input(f"TDLib Telegram code for {self.phone}: ").strip()
                relay.send({"@type": "checkAuthenticationCode", "code": code})
                continue
            if self._is_auth_state(payload, "authorizationStateWaitPassword"):
                if not interactive:
                    raise RuntimeError(
                        "TDLib 2FA password required. Run `python main.py --login-source-telegram --realtime-backend tdlib` first."
                    )
                password = input("TDLib Telegram 2FA password: ").strip()
                relay.send({"@type": "checkAuthenticationPassword", "password": password})
                continue
            if self._is_auth_state(payload, "authorizationStateReady"):
                return
            if self._is_auth_state(payload, "authorizationStateClosed"):
                raise RuntimeError("TDLib authorization closed unexpectedly")
            if payload.get("@type") == "error":
                message = str(payload.get("message", ""))
                if "Parameters" in message and "specified" in message and not sent_parameters:
                    self._tdlib_send_auth_parameters(relay)
                    sent_parameters = True
                    continue
                if (
                    "Parameters" in message
                    and "specified" in message
                    and sent_parameters
                    and not sent_legacy_parameters
                ):
                    self._tdlib_send_auth_parameters(relay, legacy=True)
                    sent_legacy_parameters = True
                    continue
                raise RuntimeError(f"TDLib error during auth: {payload.get('message', payload)}")

    async def login_interactive(self) -> bool:
        if not self.is_configured():
            raise RuntimeError("LISTING_SOURCE_TELEGRAM_API_ID/API_HASH/PHONE 설정이 필요합니다.")
        relay = _TdlibRelay(self.relay_path)
        await asyncio.to_thread(relay.start)
        try:
            await asyncio.to_thread(self._ensure_ready, relay, True)
            return True
        finally:
            await asyncio.to_thread(relay.close)

    async def _resolve_watch_chats(
        self,
        relay: _TdlibRelay,
        channel_handles: list[str],
    ) -> dict[int, str]:
        chat_id_to_handle: dict[int, str] = {}
        cached_chat_ids = _load_watch_chat_cache()
        configured_chat_ids = dict(cached_chat_ids)
        configured_chat_ids.update(
            _parse_watch_chat_ids(os.environ.get("LISTING_TDLIB_WATCH_CHATS"))
        )
        resolved_chat_ids: dict[str, int] = {}
        for handle in channel_handles:
            username = handle.lstrip("@")
            username_key = _handle_key(username)
            configured_chat_id = configured_chat_ids.get(username_key)
            if configured_chat_id is not None:
                chat_id_to_handle[configured_chat_id] = username
                logger.info(
                    "TDLib chat id cache 사용: %s=%s",
                    username,
                    configured_chat_id,
                )
                continue

            response = await asyncio.to_thread(
                relay.send_request,
                {"@type": "searchPublicChat", "username": username},
                20,
            )
            if response.get("@type") != "chat":
                raise RuntimeError(f"TDLib failed to resolve chat {username}: {response}")
            resolved_chat_id = int(response["id"])
            chat_id_to_handle[resolved_chat_id] = username
            logger.info(
                "TDLib chat resolved: %s=%s (LISTING_TDLIB_WATCH_CHATS에 넣으면 재시작 resolve 생략)",
                username,
                resolved_chat_id,
            )
            resolved_chat_ids[username_key] = resolved_chat_id

        if resolved_chat_ids:
            cache_to_save = dict(cached_chat_ids)
            cache_to_save.update(resolved_chat_ids)
            asyncio.create_task(asyncio.to_thread(_save_watch_chat_cache, cache_to_save))

        return chat_id_to_handle

    @staticmethod
    def _watch_spec(chat_id_to_handle: dict[int, str]) -> str:
        return ",".join(
            f"{chat_id}:{handle}"
            for chat_id, handle in chat_id_to_handle.items()
        )

    async def run_native_buy_relay_only(
        self,
        channel_handles: list[str],
        on_ready=None,
    ):
        """Start only the TDLib C++ relay native-buy path and keep it alive."""
        if not self.is_configured():
            raise RuntimeError("LISTING_SOURCE_TELEGRAM_API_ID/API_HASH/PHONE 설정이 필요합니다.")
        if not _is_truthy(os.environ.get("LISTING_TDLIB_NATIVE_BUY_ACTIVE")):
            raise RuntimeError("TDLib native relay-only mode requires LISTING_TDLIB_NATIVE_BUY_ACTIVE=1")
        if not _is_truthy(
            os.environ.get("LISTING_TDLIB_NATIVE_BUY_ENABLED"),
            default=True,
        ):
            raise RuntimeError("TDLib native relay-only mode requires LISTING_TDLIB_NATIVE_BUY_ENABLED=1")

        relay = _TdlibRelay(self.relay_path)
        await asyncio.to_thread(relay.start)
        try:
            await asyncio.to_thread(self._ensure_ready, relay, False)
            chat_id_to_handle = await self._resolve_watch_chats(relay, channel_handles)
            watch_spec = self._watch_spec(chat_id_to_handle)
            if not watch_spec:
                raise RuntimeError("TDLib native relay-only mode has no watch chats")

            await asyncio.to_thread(relay.send_raw, f"__native_start__\t{watch_spec}")
            native_status = await asyncio.to_thread(
                relay.wait_for_native_status,
                30.0,
            )
            if not native_status.get("ready"):
                raise RuntimeError(
                    "TDLib native-buy requested but not ready: "
                    f"{native_status.get('reason', 'unknown')}"
                )

            logger.info(
                "실시간 텔레그램 감시 시작 (TDLib native relay-only) — %s",
                ", ".join(channel_handles),
            )
            if on_ready is not None:
                maybe_ready = on_ready()
                if hasattr(maybe_ready, "__await__"):
                    await maybe_ready

            await asyncio.Event().wait()
        finally:
            await asyncio.to_thread(relay.close)

    @staticmethod
    def _extract_text(payload: dict) -> str:
        if payload.get("@type") != "updateNewMessage":
            return ""
        message = payload.get("message", {})
        content = message.get("content", {})
        if content.get("@type") != "messageText":
            return ""
        return (content.get("text", {}) or {}).get("text", "") or ""

    async def run(
        self,
        channel_handles: list[str],
        on_post,
        minimal_post: bool = False,
        trade_post: bool = False,
        on_ready=None,
    ):
        if not self.is_configured():
            raise RuntimeError("LISTING_SOURCE_TELEGRAM_API_ID/API_HASH/PHONE 설정이 필요합니다.")

        relay = _TdlibRelay(self.relay_path)
        await asyncio.to_thread(relay.start)
        try:
            await asyncio.to_thread(self._ensure_ready, relay, False)
            skip_clock_calibration = _is_truthy(
                os.environ.get("LISTING_TDLIB_SKIP_CLOCK_CALIBRATION"),
                default=trade_post,
            )
            if skip_clock_calibration:
                clock_offset_ns = 0
                logger.info("TDLib clock calibration skipped for hot path startup")
            else:
                try:
                    clock_offset_ns = await asyncio.to_thread(relay.measure_clock_offset_ns)
                except TimeoutError as exc:
                    logger.warning(
                        "TDLib relay clock calibration failed; using raw relay timestamps: %s",
                        exc,
                    )
                    clock_offset_ns = 0

            native_listing_mode = bool(trade_post)
            native_buy_enabled = (
                native_listing_mode
                and _is_truthy(os.environ.get("LISTING_TDLIB_NATIVE_BUY_ACTIVE"))
                and _is_truthy(
                    os.environ.get("LISTING_TDLIB_NATIVE_BUY_ENABLED"),
                    default=True,
                )
            )
            chat_id_to_handle = await self._resolve_watch_chats(relay, channel_handles)

            relay.attach_async_loop(asyncio.get_running_loop())

            if native_listing_mode:
                watch_spec = self._watch_spec(chat_id_to_handle)
                if native_buy_enabled:
                    await asyncio.to_thread(relay.send_raw, f"__native_start__\t{watch_spec}")
                    native_status = await asyncio.to_thread(
                        relay.wait_for_native_status,
                        30.0,
                    )
                    if not native_status.get("ready"):
                        raise RuntimeError(
                            "TDLib native-buy requested but not ready: "
                            f"{native_status.get('reason', 'unknown')}"
                        )
                else:
                    await asyncio.to_thread(relay.send_raw, f"__watch_chats__\t{watch_spec}")
                    await asyncio.to_thread(relay.send_raw, "__native_buy_off__")
                    await asyncio.to_thread(relay.wait_for_native_status, 10.0)
                    await asyncio.to_thread(relay.send_raw, "__native_listing_on__")

            logger.info(
                "실시간 텔레그램 감시 시작 (TDLib) — %s",
                ", ".join(channel_handles),
            )
            if on_ready is not None:
                maybe_ready = on_ready()
                if hasattr(maybe_ready, "__await__"):
                    await maybe_ready

            while True:
                event = await relay.async_wait_for(
                    lambda event: event.payload.get("@type") in {"updateNewMessage", "listingMatched"},
                    3600,
                )
                payload = event.payload
                if payload.get("@type") == "listingMatched":
                    post = _build_listing_matched_post(
                        payload=payload,
                        event_received_monotonic_ns=int(event.received_monotonic_ns),
                        clock_offset_ns=clock_offset_ns,
                    )
                    maybe_result = on_post(post)
                    if hasattr(maybe_result, "__await__"):
                        await maybe_result
                    continue
                message = payload.get("message", {})
                chat_id = int(message.get("chat_id", 0))
                handle = chat_id_to_handle.get(chat_id)
                if handle is None:
                    continue
                text = self._extract_text(payload)
                if not text:
                    continue

                published_at = datetime.fromtimestamp(
                    int(message.get("date", 0)),
                    tz=timezone.utc,
                )
                received_monotonic_ns = int(event.received_monotonic_ns) - clock_offset_ns
                realtime_client = _realtime_telegram_client_class()
                if trade_post:
                    title = realtime_client.extract_title(text)
                    if not title:
                        continue
                    post = realtime_client.build_trade_post(
                        channel_handle=handle,
                        message_id=int(message["id"]),
                        text=text,
                        published_at=published_at,
                        received_monotonic_ns=received_monotonic_ns,
                        title=title,
                    )
                elif minimal_post:
                    if not realtime_client.has_nonspace(text):
                        continue
                    received_at = datetime.now(timezone.utc)
                    post = realtime_client.build_minimal_post(
                        channel_handle=handle,
                        message_id=int(message["id"]),
                        text=text,
                        published_at=published_at,
                        received_at=received_at,
                        received_monotonic_ns=received_monotonic_ns,
                    )
                else:
                    if not realtime_client.has_nonspace(text):
                        continue
                    received_at = datetime.now(timezone.utc)
                    post = realtime_client.build_post(
                        channel_handle=handle,
                        message_id=int(message["id"]),
                        text=text,
                        published_at=published_at,
                        received_at=received_at,
                        received_monotonic_ns=received_monotonic_ns,
                    )

                maybe_result = on_post(post)
                if hasattr(maybe_result, "__await__"):
                    await maybe_result
        finally:
            await asyncio.to_thread(relay.close)
