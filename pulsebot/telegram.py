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
# The limit is PER CHAT, so the clock is tracked per chat: adding channels
# must not slow down delivery to the ones already configured.
MIN_SECONDS_BETWEEN_SENDS = 3.5

# Telegram also caps a bot at ~30 messages/second across all chats. With one
# article fanning out to N channels this is the binding limit only for very
# large N, so a small floor between any two sends keeps us clear of it.
MIN_SECONDS_BETWEEN_ANY_SENDS = 0.05


class TelegramError(RuntimeError):
    """A fault affecting every channel, such as a bad bot token."""


class ChannelError(TelegramError):
    """A fault confined to one channel; other channels can still proceed."""


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
    """Sends to any number of chats with one bot token.

    One publisher instance is shared across all channels so the per-chat and
    global rate-limit clocks stay coherent.
    """

    def __init__(self, token: str, session: requests.Session | None = None):
        if not token:
            raise ValueError("Telegram bot token is empty")
        self._token = token
        self.session = session or requests.Session()
        self._last_send_per_chat: dict[str, float] = {}
        self._last_send_any = 0.0

    @property
    def _url(self) -> str:
        return f"{API_BASE}/bot{self._token}/sendMessage"

    def _throttle(self, chat_id: str) -> None:
        now = time.monotonic()
        waits = [MIN_SECONDS_BETWEEN_ANY_SENDS - (now - self._last_send_any)]
        last_here = self._last_send_per_chat.get(chat_id)
        if last_here is not None:
            waits.append(MIN_SECONDS_BETWEEN_SENDS - (now - last_here))
        wait = max(waits)
        if wait > 0:
            time.sleep(wait)

    def send(self, chat_id: str, text: str, max_attempts: int = 5) -> bool:
        """Send one message. Returns True on success, False if it was dropped.

        Honours Telegram's 429 retry_after, retries transient 5xx and network
        errors with backoff, and gives up on 4xx (a malformed message will not
        get better by being retried).
        """
        if not chat_id:
            raise ValueError("Telegram chat id is empty")

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

        for attempt in range(1, max_attempts + 1):
            self._throttle(chat_id)
            try:
                resp = self.session.post(self._url, json=payload, timeout=30)
            except requests.RequestException as exc:
                log.warning("Network error on attempt %d/%d: %s", attempt, max_attempts, exc)
                time.sleep(min(2 ** attempt, 30))
                continue
            finally:
                now = time.monotonic()
                self._last_send_per_chat[chat_id] = now
                self._last_send_any = now

            if resp.status_code == 200:
                return True

            if resp.status_code == 429:
                retry_after = 5
                try:
                    retry_after = int(resp.json()["parameters"]["retry_after"])
                except (ValueError, KeyError, TypeError):
                    pass
                log.warning("Rate limited by Telegram on %s; sleeping %ds", chat_id, retry_after)
                time.sleep(retry_after + 1)
                continue

            if 500 <= resp.status_code < 600:
                log.warning("Telegram %d on %s, attempt %d/%d", resp.status_code, chat_id, attempt, max_attempts)
                time.sleep(min(2 ** attempt, 30))
                continue

            # 4xx other than 429: unauthorized token, bad chat id, bad HTML.
            # These are configuration errors, not transient ones.
            log.error(
                "Telegram rejected the message to %s (%d): %s",
                chat_id, resp.status_code, resp.text[:400],
            )
            if resp.status_code == 401:
                # The token is bad, so every channel will fail identically.
                raise TelegramError("Telegram rejected the bot token (401 Unauthorized).")

            body = resp.text.lower()
            if "chat not found" in body or resp.status_code == 403:
                # Wrong id, or the bot is not an admin here. This is specific
                # to ONE channel, so it must not abort the others.
                raise ChannelError(
                    f"Cannot post to {chat_id!r}: check the id and that the bot is an "
                    "administrator of that channel with 'Post Messages' permission."
                )
            return False

        log.error("Giving up on a message to %s after %d attempts", chat_id, max_attempts)
        return False

    def post_article(self, chat_id: str, article) -> bool:
        return self.send(chat_id, render(article))
