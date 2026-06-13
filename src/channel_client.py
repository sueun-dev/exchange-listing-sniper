"""Public Telegram channel HTML fetcher."""

from __future__ import annotations

import html
import logging
import re
import urllib.request

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
MESSAGE_RE = re.compile(
    r'data-post="[^/]+/(\d+)".*?'
    r'<div class="tgme_widget_message_text js-message_text" dir="auto">(.*?)</div>.*?'
    r'<time datetime="([^"]+)" class="time">',
    re.S,
)


def _clean_html_text(raw_html: str) -> str:
    text = raw_html.replace("<br/>", "\n").replace("<br>", "\n")
    text = re.sub(r"<mark class=\"highlight\">(.*?)</mark>", r"\1", text)
    text = re.sub(r"<a [^>]*>(.*?)</a>", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


class TelegramChannelClient:
    """Fetch recent posts from public Telegram channel pages."""

    def __init__(self, timeout: int = REQUEST_TIMEOUT):
        self.timeout = timeout

    def fetch_recent_posts(self, channel_handle: str, limit: int = 20) -> list[dict]:
        url = f"https://t.me/s/{channel_handle}"
        logger.debug("텔레그램 채널 조회: %s", url)

        with urllib.request.urlopen(url, timeout=self.timeout) as response:
            raw_html = response.read().decode("utf-8", "ignore")

        posts = self._parse_posts(raw_html, channel_handle)
        return posts[:limit]

    def _parse_posts(self, raw_html: str, channel_handle: str) -> list[dict]:
        posts: list[dict] = []
        for message_id, text_html, published_at in MESSAGE_RE.findall(raw_html):
            text = _clean_html_text(text_html)
            if not text:
                continue

            posts.append(
                {
                    "channel_handle": channel_handle,
                    "message_id": int(message_id),
                    "published_at": published_at,
                    "title": _first_nonempty_line(text),
                    "text": text,
                    "post_url": f"https://t.me/{channel_handle}/{message_id}",
                }
            )

        posts.sort(key=lambda post: post["message_id"], reverse=True)
        logger.debug("[%s] 최근 포스트 %d건 파싱", channel_handle, len(posts))
        return posts
