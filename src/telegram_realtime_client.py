from __future__ import annotations

"""Realtime Telegram source using a user session via Telethon."""

import asyncio
import getpass
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.errors import SessionPasswordNeededError
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged

from .env_loader import MODULE_DIR, load_env_settings

logger = logging.getLogger(__name__)

DEFAULT_SESSION_PATH = MODULE_DIR / "data" / "listing_source.session"


class RealtimeTelegramChannelClient:
    """Listen to new Telegram messages from target channels in realtime."""

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
                "LISTING_SOURCE_TELEGRAM_SESSION",
            }
        )

        self.api_id = int(
            api_id
            or settings.get("LISTING_SOURCE_TELEGRAM_API_ID")
            or 0
        )
        self.api_hash = api_hash or settings.get("LISTING_SOURCE_TELEGRAM_API_HASH", "")
        self.phone = phone or settings.get("LISTING_SOURCE_TELEGRAM_PHONE", "")
        self.session_path = Path(
            session_path
            or settings.get("LISTING_SOURCE_TELEGRAM_SESSION")
            or DEFAULT_SESSION_PATH
        )
        self.session_path.parent.mkdir(parents=True, exist_ok=True)

    def is_configured(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def has_session_file(self) -> bool:
        return self.session_path.exists()

    def create_client(self) -> TelegramClient:
        # Abridged transport reduces MTProto framing overhead compared with TcpFull.
        return TelegramClient(
            str(self.session_path),
            self.api_id,
            self.api_hash,
            connection=ConnectionTcpAbridged,
            receive_updates=True,
            sequential_updates=False,
            catch_up=False,
        )

    async def login_interactive(self) -> bool:
        if not self.is_configured():
            raise RuntimeError("LISTING_SOURCE_TELEGRAM_API_ID/API_HASH 설정이 필요합니다.")

        client = self.create_client()
        await client.connect()
        try:
            if await client.is_user_authorized():
                logger.info("텔레그램 유저 세션이 이미 인증되어 있습니다.")
                return True

            if not self.phone:
                raise RuntimeError("LISTING_SOURCE_TELEGRAM_PHONE 설정이 필요합니다.")

            sent = await client.send_code_request(self.phone)
            code = input(f"Telegram code for {self.phone}: ").strip()
            try:
                await client.sign_in(
                    phone=self.phone,
                    code=code,
                    phone_code_hash=sent.phone_code_hash,
                )
            except SessionPasswordNeededError:
                password = getpass.getpass("Telegram 2FA password: ")
                await client.sign_in(password=password)

            authorized = await client.is_user_authorized()
            if authorized:
                logger.info("텔레그램 유저 세션 로그인 완료")
            return authorized
        finally:
            await client.disconnect()

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
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                "텔레그램 유저 세션이 없습니다. 먼저 `python main.py --login-source-telegram` 을 실행하세요."
            )

        entities = []
        handle_by_peer_id: dict[int, str] = {}
        for handle in channel_handles:
            normalized = handle.lstrip("@")
            entity = await client.get_entity(normalized)
            entities.append(entity)
            handle_by_peer_id[utils.get_peer_id(entity)] = normalized

        @client.on(events.NewMessage(chats=entities))
        async def _handler(event):
            received_monotonic_ns = time.monotonic_ns()
            handle = handle_by_peer_id.get(event.chat_id)
            if handle is None:
                chat = await event.get_chat()
                username = getattr(chat, "username", None)
                handle = username or ""
            if not handle:
                logger.debug("채널 핸들을 확인할 수 없어 메시지 스킵: %s", event.chat_id)
                return

            raw_text = event.raw_text or ""
            if not raw_text:
                return
            if trade_post:
                title = RealtimeTelegramChannelClient.extract_title(raw_text)
                if not title:
                    return
                post = self.build_trade_post(
                    channel_handle=handle,
                    message_id=int(event.message.id),
                    text=raw_text,
                    published_at=event.message.date,
                    received_monotonic_ns=received_monotonic_ns,
                    title=title,
                )
            elif minimal_post:
                if not RealtimeTelegramChannelClient.has_nonspace(raw_text):
                    return
                received_at = datetime.now(timezone.utc)
                post = self.build_minimal_post(
                    channel_handle=handle,
                    message_id=int(event.message.id),
                    text=raw_text,
                    published_at=event.message.date,
                    received_at=received_at,
                    received_monotonic_ns=received_monotonic_ns,
                )
            else:
                if not RealtimeTelegramChannelClient.has_nonspace(raw_text):
                    return
                received_at = datetime.now(timezone.utc)
                post = self.build_post(
                    channel_handle=handle,
                    message_id=int(event.message.id),
                    text=raw_text,
                    published_at=event.message.date,
                    received_at=received_at,
                    received_monotonic_ns=received_monotonic_ns,
                )

            maybe_result = on_post(post)
            if asyncio.iscoroutine(maybe_result):
                await maybe_result

        logger.info(
            "실시간 텔레그램 감시 시작 — %s",
            ", ".join(channel_handles),
        )
        try:
            await client.run_until_disconnected()
        finally:
            await client.disconnect()

    @staticmethod
    def build_post(
        *,
        channel_handle: str,
        message_id: int,
        text: str,
        published_at: datetime,
        received_at: datetime | None = None,
        received_monotonic_ns: int | None = None,
    ) -> dict:
        clean_text = text.strip()
        title = RealtimeTelegramChannelClient.extract_title(clean_text)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if received_at is None:
            received_at = datetime.now(timezone.utc)
        if received_monotonic_ns is None:
            received_monotonic_ns = time.monotonic_ns()
        return {
            "channel_handle": channel_handle,
            "message_id": int(message_id),
            "published_at": published_at.isoformat(),
            "received_at": received_at.isoformat(),
            "received_monotonic_ns": int(received_monotonic_ns),
            "title": title,
            "text": clean_text,
            "post_url": f"https://t.me/{channel_handle}/{message_id}",
        }

    @staticmethod
    def build_trade_post(
        *,
        channel_handle: str,
        message_id: int,
        text: str,
        published_at: datetime,
        received_monotonic_ns: int | None = None,
        title: str | None = None,
    ) -> dict:
        if title is None:
            title = RealtimeTelegramChannelClient.extract_title(text)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if received_monotonic_ns is None:
            received_monotonic_ns = time.monotonic_ns()
        return {
            "channel_handle": channel_handle,
            "message_id": int(message_id),
            "published_at": published_at,
            "received_monotonic_ns": int(received_monotonic_ns),
            "title": title,
        }

    @staticmethod
    def build_minimal_post(
        *,
        channel_handle: str,
        message_id: int,
        text: str,
        published_at: datetime,
        received_at: datetime | None = None,
        received_monotonic_ns: int | None = None,
    ) -> dict:
        clean_text = text.strip()
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if received_at is None:
            received_at = datetime.now(timezone.utc)
        if received_monotonic_ns is None:
            received_monotonic_ns = time.monotonic_ns()
        return {
            "channel_handle": channel_handle,
            "message_id": int(message_id),
            "published_at": published_at,
            "received_at": received_at,
            "received_monotonic_ns": int(received_monotonic_ns),
            "text": clean_text,
        }

    @staticmethod
    def extract_title(text: str) -> str:
        if not text:
            return ""
        length = len(text)
        start = 0
        while start < length and text[start].isspace():
            start += 1
        if start >= length:
            return ""
        end = text.find("\n", start)
        if end == -1:
            return text[start:].strip()
        return text[start:end].strip()

    @staticmethod
    def has_nonspace(text: str) -> bool:
        for char in text:
            if not char.isspace():
                return True
        return False
