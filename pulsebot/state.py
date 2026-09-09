"""Per-channel record of which Pulse articles have already been posted.

Why a set and not a high-water mark
-----------------------------------
Pulse sorts its homepage by *publish time*, but item ids are assigned at
*ingestion time*. Those two orders do not agree: on a live sample of 307
items the ids were NOT monotonically decreasing down the page. So an article
ingested late (high id) can carry an older timestamp and sort below articles
with lower ids. Tracking only "the highest id seen" would therefore skip
those articles permanently and silently. We keep an explicit set instead.

Why state is per channel and not shared
---------------------------------------
Channels are added at different times and fail independently - a bot can be
removed from one channel, lose its post permission there, or hit a rate
limit on one chat while others are fine. With a single shared set, an
article delivered to channel A but rejected by channel B would be marked
seen for both, and B would lose it permanently. Each channel therefore owns
its own set, advanced only by its own successful sends.

The sets are bounded: Pulse publishes ~300 articles/day and its homepage
only reaches back ~24 hours, so anything older than the retained window can
never reappear on the page and is safe to forget.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ~300 articles/day on Pulse, so 5000 ids is roughly a two-week memory -
# far beyond the ~24 hours the homepage actually reaches back.
MAX_SEEN = 5000

SCHEMA_VERSION = 2


class SeenStore:
    """Bounded per-channel sets of article ids, persisted as one JSON file."""

    def __init__(self, path: str | Path, max_seen: int = MAX_SEEN):
        self.path = Path(path)
        self.max_seen = max_seen
        self.channels: dict[str, set[int]] = {}
        self._known: set[str] = set()  # channels present when the file loaded

    # ---------- loading ----------

    def load(self, chat_ids: list[str] | None = None) -> "SeenStore":
        """Load state, migrating the old single-channel format if present.

        `chat_ids` is the currently configured channel list. A v1 file holds
        one flat set with no channel attribution, so its ids are adopted as
        the baseline for every configured channel: the original channel keeps
        its exact history, and any channel added at the same time starts from
        the current page instead of receiving a backlog dump.
        """
        if not self.path.exists():
            log.info("No state file at %s - treating this as a first run", self.path)
            return self

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt state file must not cause the whole backlog to be
            # re-posted. Fail closed: behave like a first run, which seeds
            # rather than floods.
            log.error("State file %s is unreadable (%s); treating as first run", self.path, exc)
            return self

        try:
            if "channels" in data:
                for chat_id, entry in data["channels"].items():
                    self.channels[chat_id] = {int(i) for i in entry.get("seen_ids", [])}
            elif "seen_ids" in data:
                legacy = {int(i) for i in data["seen_ids"]}
                targets = chat_ids or []
                log.info(
                    "Migrating v1 state (%d ids) to per-channel format for %d channel(s)",
                    len(legacy), len(targets),
                )
                for chat_id in targets:
                    self.channels[chat_id] = set(legacy)
        except (ValueError, TypeError, AttributeError) as exc:
            log.error("State file %s has unexpected shape (%s); treating as first run", self.path, exc)
            self.channels = {}
            return self

        self._known = set(self.channels)
        log.info(
            "Loaded state for %d channel(s): %s",
            len(self.channels),
            ", ".join(f"{c}={len(ids)}" for c, ids in sorted(self.channels.items())) or "none",
        )
        return self

    # ---------- per-channel queries ----------

    def is_new_channel(self, chat_id: str) -> bool:
        """True when this channel had no state when the file was loaded."""
        return chat_id not in self._known

    def unseen(self, chat_id: str, articles):
        seen = self.channels.get(chat_id, set())
        return [a for a in articles if a.id not in seen]

    def mark(self, chat_id: str, articles) -> None:
        self.channels.setdefault(chat_id, set()).update(a.id for a in articles)

    def count(self, chat_id: str) -> int:
        return len(self.channels.get(chat_id, set()))

    # ---------- saving ----------

    def save(self) -> None:
        payload = {"version": SCHEMA_VERSION, "channels": {}}
        for chat_id, ids in self.channels.items():
            # Keep the highest ids; ingestion ids only ever grow, so the
            # largest are the most recent and the only ones the homepage can
            # still show.
            kept = sorted(ids)[-self.max_seen :]
            payload["channels"][chat_id] = {"seen_ids": kept, "count": len(kept)}

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so an interrupted run cannot leave a half-written
        # file that would read as corrupt on the next run.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)
        log.info(
            "Saved state for %d channel(s) to %s",
            len(payload["channels"]), self.path,
        )
