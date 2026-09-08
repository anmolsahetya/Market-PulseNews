"""Post articles to a Telegram channel via the Bot API."""

from __future__ import annotations

import html
import logging
import time

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Telegram caps a single message at 4096 UTF-16 code units. We stay well
# under it and truncate the description rather than risk a rejected send.
MAX_MESSAGE_CHARS = 4000

# Telegram throttles messages to a single chat at roughly 20/minute. One
# message every 3.5s (~17/min) stays under that with margin. At Pulse's
# ~300 articles/day this only ever matters when catching up after an outage.
MIN_SECONDS_BETWEEN_SENDS = 3.5


class TelegramError(RuntimeError):
    pass


def render(article) -> str:
    """Render an Article as Telegram-flavoured HTML.

    Every piece of scraped text is HTML-escaped: headlines regularly contain
    & and quotes, and an unescaped one would make Telegram reject the message.
    """
    title = html.escape(article.title)
    url = html.escape(article.url, quote=True)

    meta_bits = [b for b in (html.escape(article.source), article.published_ist) if b]
    meta = " · ".join(meta_bits)

    head = f'<b><a href="{url}">{title}</a></b>'
    tail = f"<i>{html.escape(meta)}</i>" if meta else ""

    # Budget whatever is left over for the description.
    budget = MAX_MESSAGE_CHARS - len(head) - len(tail) - 4
    desc = html.escape(article.description)
    if budget > 40 and desc:
        if len(desc) > budget:
            desc = desc[: budget - 1].rstrip() + "…"
    else:
        desc = ""

    return "\n\n".join(part for part in (head, desc, tail) if part)


class TelegramPublisher:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        if not token:
            raise ValueError("Telegram bot token is empty")
        if not chat_id:
            raise ValueError("Telegram chat id is empty")
        self._token = token
        self.chat_id = chat_id
        self.session = session or requests.Session()
        self._last_send = 0.0

    @property
    def _url(self) -> str:
        return f"{API_BASE}/bot{self._token}/sendMessage"

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_send
        if elapsed < MIN_SECONDS_BETWEEN_SENDS:
            time.sleep(MIN_SECONDS_BETWEEN_SENDS - elapsed)

    def send(self, text: str, max_attempts: int = 5) -> bool:
        """Send one message. Returns True on success, False if it was dropped.

        Honours Telegram's 429 retry_after, retries transient 5xx and network
        errors with backoff, and gives up on 4xx (a malformed message will not
        get better by being retried).
        """
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

        for attempt in range(1, max_attempts + 1):
            self._throttle()
            try:
                resp = self.session.post(self._url, json=payload, timeout=30)
            except requests.RequestException as exc:
                log.warning("Network error on attempt %d/%d: %s", attempt, max_attempts, exc)
                time.sleep(min(2 ** attempt, 30))
                continue
            finally:
                self._last_send = time.monotonic()

            if resp.status_code == 200:
                return True

            if resp.status_code == 429:
                retry_after = 5
                try:
                    retry_after = int(resp.json()["parameters"]["retry_after"])
                except (ValueError, KeyError, TypeError):
                    pass
                log.warning("Rate limited by Telegram; sleeping %ds", retry_after)
                time.sleep(retry_after + 1)
                continue

            if 500 <= resp.status_code < 600:
                log.warning("Telegram %d on attempt %d/%d", resp.status_code, attempt, max_attempts)
                time.sleep(min(2 ** attempt, 30))
                continue

            # 4xx other than 429: unauthorized token, bad chat id, bad HTML.
            # These are configuration errors, not transient ones.
            log.error("Telegram rejected the message (%d): %s", resp.status_code, resp.text[:400])
            if resp.status_code in (401, 403, 400) and "chat not found" in resp.text.lower():
                raise TelegramError(
                    f"Telegram could not reach chat {self.chat_id!r}. Check TELEGRAM_CHAT_ID "
                    "and that the bot is an administrator of the channel."
                )
            if resp.status_code == 401:
                raise TelegramError("Telegram rejected the bot token (401 Unauthorized).")
            return False

        log.error("Giving up on a message after %d attempts", max_attempts)
        return False

    def post_article(self, article) -> bool:
        return self.send(render(article))
