from __future__ import annotations

"""Realtime Telegram source using Pyrogram + TgCrypto for lowest-latency MTProto."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from .env_loader import MODULE_DIR, load_env_settings
from .telegram_realtime_client import RealtimeTelegramChannelClient

logger = logging.getLogger(__name__)

DEFAULT_SESSION_PATH = MODULE_DIR / "data" / "listing_source_pyrogram"


class PyrogramRealtimeChannelClient:
    """Listen to new Telegram messages from target channels via Pyrogram + TgCrypto."""

    def __init__(
        self,
        api_id: int | None = None,
        api_hash: str | None = None,
        phone: str | None = None,
        session_path: str | Path | None = None,
    ):
        settings = load_env_settings(
            {
                "LISTING_SOURCE_TELEGRAM_API_ID",
                "LISTING_SOURCE_TELEGRAM_API_HASH",
                "LISTING_SOURCE_TELEGRAM_PHONE",
            }
        )

        self.api_id = int(
            api_id if api_id is not None
            else (settings.get("LISTING_SOURCE_TELEGRAM_API_ID") or 0)
        )
        self.api_hash = (
            api_hash if api_hash is not None
            else settings.get("LISTING_SOURCE_TELEGRAM_API_HASH", "")
        )
        self.phone = (
            phone if phone is not None
            else settings.get("LISTING_SOURCE_TELEGRAM_PHONE", "")
        )
        self.session_path = Path(
            session_path or DEFAULT_SESSION_PATH
        )
        self.session_path.parent.mkdir(parents=True, exist_ok=True)

    def is_configured(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def has_session_file(self) -> bool:
        session_file = self.session_path.with_suffix(".session")
        return session_file.exists()

    def create_client(self) -> Client:
        return Client(
            name=str(self.session_path),
            api_id=self.api_id,
            api_hash=self.api_hash,
            phone_number=self.phone,
            no_updates=False,
            workers=1,
        )

    async def login_interactive(self) -> bool:
        if not self.is_configured():
            raise RuntimeError("LISTING_SOURCE_TELEGRAM_API_ID/API_HASH 설정이 필요합니다.")

        client = self.create_client()
        async with client:
            me = await client.get_me()
            if me:
                logger.info("Pyrogram 유저 세션 로그인 완료: %s", me.first_name)
                return True
        return False

    async def run(
        self,
        channel_handles: list[str],
        on_post,
        minimal_post: bool = False,
        trade_post: bool = False,
    ):
        if not self.is_configured():
            raise RuntimeError("LISTING_SOURCE_TELEGRAM_API_ID/API_HASH 설정이 필요합니다.")

        client = self.create_client()
        await client.start()

        # Resolve chat IDs for target channels
        chat_id_to_handle: dict[int, str] = {}
        for handle in channel_handles:
            normalized = handle.lstrip("@")
            try:
                chat = await client.get_chat(normalized)
                chat_id_to_handle[chat.id] = normalized
            except Exception:
                logger.warning("Pyrogram: 채널 %s resolve 실패", normalized)
                continue

        if not chat_id_to_handle:
            logger.error("Pyrogram: resolve 성공한 채널이 없습니다")
            await client.stop()
            return

        stop_event = asyncio.Event()

        async def _handler(client_instance, message):
            received_monotonic_ns = time.monotonic_ns()

            chat_id = message.chat.id
            handle = chat_id_to_handle.get(chat_id)
            if handle is None:
                return

            raw_text = message.text or message.caption or ""
            if not raw_text:
                return

            published_at = message.date
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)

            if trade_post:
                title = RealtimeTelegramChannelClient.extract_title(raw_text)
                if not title:
                    return
                post = RealtimeTelegramChannelClient.build_trade_post(
                    channel_handle=handle,
                    message_id=int(message.id),
                    text=raw_text,
                    published_at=published_at,
                    received_monotonic_ns=received_monotonic_ns,
                    title=title,
                )
            elif minimal_post:
                if not RealtimeTelegramChannelClient.has_nonspace(raw_text):
                    return
                received_at = datetime.now(timezone.utc)
                post = RealtimeTelegramChannelClient.build_minimal_post(
                    channel_handle=handle,
                    message_id=int(message.id),
                    text=raw_text,
                    published_at=published_at,
                    received_at=received_at,
                    received_monotonic_ns=received_monotonic_ns,
                )
            else:
                if not RealtimeTelegramChannelClient.has_nonspace(raw_text):
                    return
                received_at = datetime.now(timezone.utc)
                post = RealtimeTelegramChannelClient.build_post(
                    channel_handle=handle,
                    message_id=int(message.id),
                    text=raw_text,
                    published_at=published_at,
                    received_at=received_at,
                    received_monotonic_ns=received_monotonic_ns,
                )

            maybe_result = on_post(post)
            if asyncio.iscoroutine(maybe_result):
                await maybe_result

        # Register handler for all channels
        target_ids = list(chat_id_to_handle.keys())
        client.add_handler(
            MessageHandler(
                _handler,
                filters.chat(target_ids),
            )
        )

        logger.info(
            "실시간 텔레그램 감시 시작 (Pyrogram) — %s",
            ", ".join(channel_handles),
        )

        try:
            await stop_event.wait()
        finally:
            await client.stop()
