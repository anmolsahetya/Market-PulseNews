"""Configuration, read entirely from environment variables.

Secrets are never read from files in the repo. On GitHub Actions these come
from repository Secrets; locally, export them in your shell.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Channels may be separated by commas, newlines or whitespace, so a secret
# can be pasted as one line or as a list.
_SPLIT = re.compile(r"[,\s]+")


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_chat_ids(raw: str) -> list[str]:
    """Split a chat-id list, dropping blanks and duplicates, keeping order.

    Duplicates matter: the same channel listed twice would receive every
    article twice, since each send is a separate API call.
    """
    seen: set[str] = set()
    out: list[str] = []
    for part in _SPLIT.split(raw or ""):
        part = part.strip()
        if not part or part in seen:
            continue
        seen.add(part)
        out.append(part)
    return out


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_ids: list[str] = field(default_factory=list)
    state_path: str = "state/seen.json"
    dry_run: bool = False
    seed_on_first_run: bool = True
    max_per_run: int = 40

    @property
    def chat_id(self) -> str:
        """The first channel - kept for single-channel callers."""
        return self.chat_ids[0] if self.chat_ids else ""

    @classmethod
    def from_env(cls) -> "Config":
        dry_run = _flag("DRY_RUN", False)
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

        # TELEGRAM_CHAT_IDS is the multi-channel form; TELEGRAM_CHAT_ID is
        # still accepted so an existing single-channel setup keeps working.
        raw = os.environ.get("TELEGRAM_CHAT_IDS") or os.environ.get("TELEGRAM_CHAT_ID") or ""
        chat_ids = parse_chat_ids(raw)

        if not dry_run:
            missing = []
            if not token:
                missing.append("TELEGRAM_BOT_TOKEN")
            if not chat_ids:
                missing.append("TELEGRAM_CHAT_IDS")
            if missing:
                raise SystemExit(
                    "Missing required environment variable(s): "
                    + ", ".join(missing)
                    + "\nSet them as GitHub repository Secrets, or export them locally. "
                    "TELEGRAM_CHAT_IDS accepts several channels separated by commas. "
                    "Use DRY_RUN=1 to test the scraper without posting."
                )

        return cls(
            bot_token=token,
            chat_ids=chat_ids,
            state_path=os.environ.get("STATE_PATH", "state/seen.json"),
            dry_run=dry_run,
            # A newly added channel seeds instead of receiving the ~300
            # article backlog currently on the page.
            seed_on_first_run=_flag("SEED_ON_FIRST_RUN", True),
            # Flood guard, applied per channel.
            max_per_run=int(os.environ.get("MAX_PER_RUN", "40")),
        )
