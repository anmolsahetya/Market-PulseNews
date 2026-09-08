"""Configuration, read entirely from environment variables.

Secrets are never read from files in the repo. On GitHub Actions these come
from repository Secrets; locally, export them in your shell.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: str
    state_path: str
    dry_run: bool
    seed_on_first_run: bool
    max_per_run: int

    @classmethod
    def from_env(cls) -> "Config":
        dry_run = _flag("DRY_RUN", False)
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

        if not dry_run:
            missing = [
                n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id)) if not v
            ]
            if missing:
                raise SystemExit(
                    "Missing required environment variable(s): "
                    + ", ".join(missing)
                    + "\nSet them as GitHub repository Secrets, or export them locally. "
                    "Use DRY_RUN=1 to test the scraper without posting."
                )

        return cls(
            bot_token=token,
            chat_id=chat_id,
            state_path=os.environ.get("STATE_PATH", "state/seen.json"),
            dry_run=dry_run,
            # First run seeds the store instead of dumping ~300 backlogged
            # articles into the channel.
            seed_on_first_run=_flag("SEED_ON_FIRST_RUN", True),
            # A safety valve: if something goes wrong upstream and the diff
            # explodes, don't flood the channel.
            max_per_run=int(os.environ.get("MAX_PER_RUN", "40")),
        )
