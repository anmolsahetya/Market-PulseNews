"""Entry point: scrape Pulse, post anything new to every channel, persist state."""

from __future__ import annotations

import logging
import sys

from .config import Config
from .scraper import scrape
from .state import SeenStore
from .telegram import ChannelError, TelegramError, TelegramPublisher, render


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


log = logging.getLogger("pulsebot")

# How many articles a dry run renders, so a cold start stays readable.
DRY_RUN_PREVIEW = 5


def _chronological(articles):
    """Oldest first, so a channel reads top to bottom in publish order."""
    return sorted(articles, key=lambda a: (a.published is not None, a.published, a.id))


def _dry_run(cfg: Config, store: SeenStore, articles) -> int:
    """Preview without sending anything or writing any state."""
    targets = cfg.chat_ids or ["(no channels configured)"]
    log.info("[DRY RUN] %d channel(s) configured: %s", len(targets), ", ".join(targets))

    reference = cfg.chat_ids[0] if cfg.chat_ids else None
    new = _chronological(store.unseen(reference, articles) if reference else articles)

    preview = new[-DRY_RUN_PREVIEW:]
    log.info(
        "[DRY RUN] %d new article(s) for %s; previewing the newest %d. "
        "Nothing is sent and no state is written.",
        len(new), reference or "an unconfigured channel", len(preview),
    )
    if reference and store.is_new_channel(reference):
        log.info(
            "[DRY RUN] %s has no state yet: on a real run these would be seeded "
            "silently rather than posted.", reference,
        )
    for a in preview:
        log.info("[DRY RUN] would post:\n%s\n%s", render(a), "-" * 60)
    return 0


def _process_channel(cfg: Config, store: SeenStore, publisher, chat_id: str, articles) -> tuple[int, int]:
    """Post everything new to one channel. Returns (posted, dropped)."""
    new = _chronological(store.unseen(chat_id, articles))

    if not new:
        log.info("%s: nothing new (%d seen)", chat_id, store.count(chat_id))
        return 0, 0

    # A channel with no state - newly added, or the very first run - is
    # seeded rather than sent the ~300 article backlog on the page.
    if store.is_new_channel(chat_id) and cfg.seed_on_first_run:
        store.mark(chat_id, articles)
        log.info(
            "%s: new channel, seeded with %d articles without posting. "
            "It will receive genuinely new articles from the next run.",
            chat_id, len(articles),
        )
        return 0, 0

    if len(new) > cfg.max_per_run:
        log.warning(
            "%s: %d new articles exceeds MAX_PER_RUN=%d; posting the %d most recent "
            "and marking the rest as seen.",
            chat_id, len(new), cfg.max_per_run, cfg.max_per_run,
        )
        skipped, new = new[: -cfg.max_per_run], new[-cfg.max_per_run :]
        store.mark(chat_id, skipped)

    log.info("%s: posting %d new article(s)", chat_id, len(new))
    posted = dropped = 0
    for article in new:
        if publisher.post_article(chat_id, article):
            posted += 1
        else:
            # A permanent rejection won't succeed on a retry next run, so
            # record it as seen rather than looping on it forever.
            dropped += 1
            log.error("%s: dropped article %d (%s)", chat_id, article.id, article.url)
        # Marked per article so an interruption never loses what was sent.
        store.mark(chat_id, [article])

    return posted, dropped


def run() -> int:
    cfg = Config.from_env()
    store = SeenStore(cfg.state_path).load(cfg.chat_ids)
    articles = scrape()

    if cfg.dry_run:
        return _dry_run(cfg, store, articles)

    publisher = TelegramPublisher(cfg.bot_token)
    results: dict[str, tuple[int, int]] = {}
    broken: dict[str, str] = {}
    exit_code = 0

    try:
        for chat_id in cfg.chat_ids:
            try:
                results[chat_id] = _process_channel(cfg, store, publisher, chat_id, articles)
            except ChannelError as exc:
                # Confined to this channel - keep going so one misconfigured
                # channel cannot stop delivery to all the others.
                broken[chat_id] = str(exc)
                log.error("%s", exc)
                exit_code = 1
    except TelegramError as exc:
        # Affects every channel (a bad token); no point continuing.
        log.error("%s", exc)
        exit_code = 1
    finally:
        # Always persist: channels that succeeded must not resend next run.
        store.save()

    total_posted = sum(p for p, _ in results.values())
    total_dropped = sum(d for _, d in results.values())
    log.info(
        "Done: %d message(s) across %d channel(s); %d dropped, %d channel(s) failing",
        total_posted, len(results), total_dropped, len(broken),
    )
    for chat_id, (posted, dropped) in sorted(results.items()):
        if posted or dropped:
            log.info("  %s: %d posted, %d dropped", chat_id, posted, dropped)
    for chat_id, reason in sorted(broken.items()):
        log.error("  %s: FAILING - %s", chat_id, reason)

    return exit_code


def main() -> None:
    setup_logging()
    try:
        sys.exit(run())
    except Exception:
        log.exception("Run failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
