"""Telegram sender for listing signals."""

from __future__ import annotations

import json
import logging
import urllib.request

from .env_loader import load_env_settings

logger = logging.getLogger(__name__)
ENV_KEY_PAIRS = [
    ("LISTING_TELEGRAM_BOT_TOKEN", "LISTING_TELEGRAM_CHAT_ID"),
    ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
]


def format_listing_signal(signal: dict) -> str:
    market_label = ", ".join(signal.get("markets", [])) or "미확인"
    bybit_spot = "YES" if signal.get("bybit_spot") else "NO"
    bybit_perp = "YES" if signal.get("bybit_perp") else "NO"
    trade = signal.get("trade") or {}
    reason = str(trade.get("reason", "") or "")
    if trade.get("executed"):
        auto_buy = (
            f"EXECUTED ({trade.get('requested_usdt', 0):.2f} USDT)"
            if isinstance(trade.get("requested_usdt"), (float, int))
            else "EXECUTED"
        )
    elif trade.get("attempted") and "dispatch" in reason.lower():
        # Async native dispatch returns before Bybit confirms the fill: the order
        # was SENT but the outcome is unknown. Do NOT render this as FAILED — that
        # misled the operator into re-buying an order that likely filled. Surface
        # it as a distinct "sent, verify fill" state instead. See [2].
        auto_buy = f"DISPATCHED ({reason}) — 체결 확인 필요"
    elif trade.get("attempted"):
        auto_buy = f"FAILED ({reason or 'unknown'})"
    else:
        auto_buy = f"SKIP ({reason or 'disabled'})"

    lines = [
        "🟢 <b>[상장 공지 감지]</b>",
        "",
        f"거래소: <b>{signal.get('exchange_name', '')}</b>",
        f"코인: <b>{signal.get('asset_name', '')} ({signal.get('ticker', '')})</b>",
        f"마켓: {market_label}",
        f"Bybit Spot: <b>{bybit_spot}</b>",
        f"Bybit Perp: <b>{bybit_perp}</b>",
        f"자동매수: <b>{auto_buy}</b>",
        f"제목: {signal.get('title', '')}",
        f"공지 시각: {str(signal.get('published_at', ''))[:19]}",
        f"원문: {signal.get('post_url', '')}",
    ]
    if trade.get("order_id"):
        lines.append(f"주문 ID: {trade['order_id']}")
    if trade.get("avg_price") and trade.get("filled_qty"):
        lines.append(
            f"체결: {trade['filled_qty']:.8f} @ {trade['avg_price']:.8f}"
        )
    return "\n".join(lines)


class ExchangeListingTelegramNotifier:
    """Send exchange listing alerts to Telegram."""

    def __init__(self, bot_token: str | None = None, chat_id: str | None = None):
        settings = load_env_settings(
            {
                "LISTING_TELEGRAM_BOT_TOKEN",
                "LISTING_TELEGRAM_CHAT_ID",
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID",
            }
        )
        selected_token = bot_token or ""
        selected_chat_id = chat_id or ""

        if not selected_token or not selected_chat_id:
            for token_key, chat_key in ENV_KEY_PAIRS:
                token = selected_token or settings.get(token_key, "")
                resolved_chat_id = selected_chat_id or settings.get(chat_key, "")
                if token and resolved_chat_id:
                    selected_token = token
                    selected_chat_id = resolved_chat_id
                    break

        self.bot_token = selected_token
        self.chat_id = selected_chat_id

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send_message(self, text: str) -> bool:
        if not self.is_configured():
            logger.warning("Listing Telegram not configured.")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.load(response)
            ok = bool(body.get("ok"))
            if ok:
                logger.info("Listing Telegram sent")
            else:
                logger.error("Listing Telegram send failed: %s", body)
            return ok
        except Exception as exc:
            logger.error("Listing Telegram send failed: %s", exc)
            return False

    def send_signals(self, signals: list[dict]) -> int:
        sent = 0
        for signal in signals:
            if self.send_message(format_listing_signal(signal)):
                sent += 1
        return sent

    def send_test_message(self) -> bool:
        return self.send_message(
            "🧪 <b>[02 상장 공지] 텔레그램 테스트</b>\n\n"
            "이 메시지가 보이면 02 전용 텔레그램 알림이 정상 동작 중입니다."
        )
