"""Scrape news items from https://pulse.zerodha.com/.

The page is fully server-rendered: a single GET returns ~300 items covering
roughly the last 24 hours, so no browser or JS execution is needed.

Markup shape (verified against the live page):

    <ul id="news">
      <li class="box item" id="item-11610517">
        <h2 class="title"><a data-id="11610517" href="https://...">Headline</a></h2>
        <div class="desc">Summary sentence.</div>
        <span class="date" title="08:57 PM, 08 Sep 2026">1 hour ago</span>
        <span class="feed">&mdash; The Hindu Business</span>
      </li>
      ...
    </ul>
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

PULSE_URL = "https://pulse.zerodha.com/"
IST = ZoneInfo("Asia/Kolkata")

# Pulse renders timestamps in IST, e.g. "08:57 PM, 08 Sep 2026".
DATE_FORMAT = "%I:%M %p, %d %b %Y"

_ID_RE = re.compile(r"^item-(\d+)$")

# A browser-like UA. Pulse does not currently gate on it, but an honest,
# identifiable agent is better manners than the requests default.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 pulse-telegram-bot/1.0"
)


@dataclass(frozen=True)
class Article:
    """One news item from Pulse."""

    id: int
    title: str
    url: str
    description: str
    source: str
    published: datetime | None  # timezone-aware (IST), or None if unparseable

    @property
    def published_ist(self) -> str:
        if self.published is None:
            return ""
        return self.published.strftime("%d %b %Y, %I:%M %p IST")


def fetch(url: str = PULSE_URL, timeout: int = 30) -> str:
    """Fetch the Pulse homepage HTML, raising on any non-2xx response."""
    resp = requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    resp.raise_for_status()
    return resp.text


def _parse_published(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), DATE_FORMAT).replace(tzinfo=IST)
    except ValueError:
        log.warning("Unrecognised timestamp format: %r", raw)
        return None


def parse(html: str) -> list[Article]:
    """Parse Pulse HTML into Articles, newest first (page order).

    Items missing an id, headline or link are skipped with a warning rather
    than raising, so one malformed entry cannot stall the whole run.
    """
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("ul", id="news")
    if container is None:
        raise ValueError(
            "Could not find <ul id='news'> on the page. "
            "Pulse's markup has probably changed; the parser needs updating."
        )

    articles: list[Article] = []
    for li in container.find_all("li", class_="item"):
        id_match = _ID_RE.match(li.get("id") or "")
        if not id_match:
            log.warning("Skipping item with unexpected id attribute: %r", li.get("id"))
            continue

        link = li.select_one("h2.title a")
        if link is None or not link.get("href"):
            log.warning("Skipping item %s: no headline link", id_match.group(1))
            continue

        desc_el = li.select_one("div.desc")
        date_el = li.select_one("span.date")
        feed_el = li.select_one("span.feed")

        # The source renders as "— The Hindu Business"; drop the em dash.
        source = ""
        if feed_el:
            source = feed_el.get_text(strip=True).lstrip("—–-").strip()

        articles.append(
            Article(
                id=int(id_match.group(1)),
                title=link.get_text(strip=True),
                url=link["href"].strip(),
                description=desc_el.get_text(strip=True) if desc_el else "",
                source=source,
                published=_parse_published(date_el.get("title") if date_el else None),
            )
        )

    if not articles:
        raise ValueError("Found <ul id='news'> but extracted zero articles.")

    log.info("Parsed %d articles from Pulse", len(articles))
    return articles


def scrape(url: str = PULSE_URL) -> list[Article]:
    return parse(fetch(url))
