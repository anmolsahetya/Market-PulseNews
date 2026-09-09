"""Preflight check: verify Pulse, the bot token, and the channel all work.

    python -m pulsebot.verify          # check everything, send nothing
    python -m pulsebot.verify --send   # also post one real test message

Run this before enabling the schedule. It exercises the three things that
can only fail outside a test suite: reaching Pulse over plain HTTP, the bot
token, and the bot's permission to post to your channel.
"""

from __future__ import annotations

import os
import sys

import requests

from .config import parse_chat_ids
from .scraper import PULSE_URL, fetch, parse
from .telegram import API_BASE, render

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def _line(status: str, msg: str) -> None:
    # An empty status renders as blank padding, for continuation lines.
    print(f"[{status:^6}] {msg}" if status.strip() else f" {'':6}  {msg}")


def check_pulse() -> bool:
    print("\n1. Pulse (https://pulse.zerodha.com/)")
    try:
        html = fetch(PULSE_URL)
    except requests.RequestException as exc:
        _line(BAD, f"Could not reach Pulse: {exc}")
        _line("", "  Check outbound network access / proxy settings.")
        return False

    try:
        articles = parse(html)
    except ValueError as exc:
        _line(BAD, f"Reached Pulse but could not parse it: {exc}")
        _line("", "  Pulse's markup has probably changed - update pulsebot/scraper.py.")
        return False

    _line(OK, f"Fetched and parsed {len(articles)} articles")
    sources = sorted({a.source for a in articles})
    _line(OK, f"Sources: {', '.join(sources)}")
    dated = [a for a in articles if a.published]
    if dated:
        _line(OK, f"Newest: {max(a.published for a in dated).strftime('%d %b %Y, %I:%M %p IST')}")
    if len(articles) - len(dated):
        _line(WARN, f"{len(articles) - len(dated)} article(s) had an unparseable timestamp")

    print("\n   Newest 3 headlines:")
    for a in articles[:3]:
        print(f"     • [{a.source}] {a.title[:78]}")
    return True


def check_bot(token: str) -> str | None:
    print("\n2. Telegram bot token")
    if not token:
        _line(BAD, "TELEGRAM_BOT_TOKEN is not set")
        return None
    try:
        r = requests.get(f"{API_BASE}/bot{token}/getMe", timeout=20)
    except requests.RequestException as exc:
        _line(BAD, f"Could not reach the Telegram API: {exc}")
        return None

    if r.status_code == 401:
        _line(BAD, "Telegram rejected the token (401). Re-copy it from @BotFather.")
        return None
    if r.status_code != 200:
        _line(BAD, f"getMe returned HTTP {r.status_code}: {r.text[:200]}")
        return None

    me = r.json()["result"]
    _line(OK, f"Authenticated as @{me.get('username')} ({me.get('first_name')})")
    return me.get("username")


def check_channel(token: str, chat_id: str, bot_username: str | None) -> bool:
    r = requests.get(f"{API_BASE}/bot{token}/getChat", params={"chat_id": chat_id}, timeout=20)
    if r.status_code != 200:
        body = r.text.lower()
        _line(BAD, f"{chat_id}: cannot see this chat (HTTP {r.status_code})")
        if "chat not found" in body:
            _line("", "  Either the id is wrong, or the bot has not been added to the channel.")
            _line("", "  Public channel: use @yourchannelname")
            _line("", "  Private channel: use the numeric -100... id from getUpdates")
        return False

    chat = r.json()["result"]
    _line(OK, f"{chat_id}: found {chat.get('type')} {chat.get('title') or chat.get('username')!r}")

    # Confirm the bot is an administrator that may post.
    if bot_username:
        me = requests.get(
            f"{API_BASE}/bot{token}/getChatMember",
            params={"chat_id": chat_id, "user_id": token.split(":")[0]},
            timeout=20,
        )
        if me.status_code == 200:
            member = me.json()["result"]
            status = member.get("status")
            if status not in ("administrator", "creator"):
                _line(BAD, f"{chat_id}: bot is '{status}', not an administrator - it cannot post.")
                _line("", "  Channel > Manage Channel > Administrators > Add your bot.")
                return False
            if status == "administrator" and member.get("can_post_messages") is False:
                _line(BAD, f"{chat_id}: bot is an admin but lacks 'Post Messages'.")
                return False
            _line(OK, f"{chat_id}: bot is {status} with permission to post")
    return True


def send_test(token: str, chat_ids: list[str]) -> bool:
    from .scraper import Article
    from .telegram import ChannelError, TelegramPublisher

    sample = Article(
        id=0,
        title="Pulse → Telegram is connected",
        url="https://pulse.zerodha.com/",
        description="If you can read this in your channel, the relay is working. "
                    "Real articles will look like this.",
        source="Setup check",
        published=None,
    )
    publisher = TelegramPublisher(token)
    all_ok = True
    for chat_id in chat_ids:
        try:
            ok = publisher.post_article(chat_id, sample)
        except ChannelError as exc:
            _line(BAD, str(exc))
            all_ok = False
            continue
        if ok:
            _line(OK, f"{chat_id}: test message delivered")
        else:
            _line(BAD, f"{chat_id}: test message was not delivered")
            all_ok = False
    return all_ok


def main() -> None:
    send = "--send" in sys.argv
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    raw = os.environ.get("TELEGRAM_CHAT_IDS") or os.environ.get("TELEGRAM_CHAT_ID") or ""
    chat_ids = parse_chat_ids(raw)

    print("=" * 64)
    print("  Pulse → Telegram preflight")
    print("=" * 64)

    results = [check_pulse()]
    username = check_bot(token)
    results.append(username is not None)

    print(f"\n3. Channel access ({len(chat_ids)} configured)")
    if not chat_ids:
        _line(BAD, "TELEGRAM_CHAT_IDS is not set")
        results.append(False)
    elif username:
        reachable = []
        for chat_id in chat_ids:
            ok = check_channel(token, chat_id, username)
            results.append(ok)
            if ok:
                reachable.append(chat_id)

        print("\n4. Test message")
        if send and reachable:
            results.append(send_test(token, reachable))
        elif reachable:
            _line(WARN, f"Skipped. Re-run with --send to post to {len(reachable)} channel(s).")
        else:
            _line(BAD, "No reachable channels to test.")

    print("\n" + "=" * 64)
    if all(results):
        print(f"  All checks passed for {len(chat_ids)} channel(s). Safe to enable the schedule.")
        sys.exit(0)
    print("  Some checks failed - fix the items marked FAIL above.")
    sys.exit(1)


if __name__ == "__main__":
    main()
