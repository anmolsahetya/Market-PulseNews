"""Entry point: scrape Pulse, post anything new to Telegram, persist state."""

from __future__ import annotations

import logging
import sys

from .config import Config
from .scraper import scrape
from .state import SeenStore
from .telegram import TelegramError, TelegramPublisher, render


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


log = logging.getLogger("pulsebot")


def run() -> int:
    cfg = Config.from_env()
    store = SeenStore(cfg.state_path).load()

    articles = scrape()
    new = store.unseen(articles)

    if not new:
        log.info("No new articles (%d on page, all already seen)", len(articles))
        return 0

    # First run: remember everything on the page but post nothing, so the
    # channel doesn't receive a day's backlog on activation.
    if store.is_first_run and cfg.seed_on_first_run:
        store.mark(articles)
        store.save()
        log.info(
            "First run: seeded state with %d articles without posting. "
            "The next run will post genuinely new items only.",
            len(articles),
        )
        return 0

    # Pulse lists newest first; post oldest first so the channel reads
    # chronologically top to bottom.
    new.sort(key=lambda a: (a.published is not None, a.published, a.id))

    if len(new) > cfg.max_per_run:
        log.warning(
            "%d new articles exceeds MAX_PER_RUN=%d; posting the %d most recent "
            "and marking the rest as seen.",
            len(new), cfg.max_per_run, cfg.max_per_run,
        )
        skipped, new = new[: -cfg.max_per_run], new[-cfg.max_per_run :]
        store.mark(skipped)

    log.info("Posting %d new article(s)", len(new))

    if cfg.dry_run:
        for a in new:
            log.info("[DRY RUN] would post:\n%s\n%s", render(a), "-" * 60)
        return 0

    publisher = TelegramPublisher(cfg.bot_token, cfg.chat_id)
    posted = failed = 0

    try:
        for article in new:
            if publisher.post_article(article):
                posted += 1
            else:
                # A permanent rejection won't succeed on a retry next run,
                # so record it as seen rather than looping on it forever.
                failed += 1
                log.error("Dropped article %d: %s", article.id, article.url)
            store.mark([article])
    except TelegramError as exc:
        # Configuration problem: stop, but keep whatever was posted so those
        # articles are not sent twice on the next run.
        log.error("%s", exc)
        return 1
    finally:
        store.save()

    log.info("Done: %d posted, %d dropped", posted, failed)
    return 0


def main() -> None:
    setup_logging()
    try:
        sys.exit(run())
    except Exception:
        log.exception("Run failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
