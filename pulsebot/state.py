"""Persistent record of which Pulse articles have already been posted.

Why a set and not a high-water mark
-----------------------------------
Pulse sorts its homepage by *publish time*, but item ids are assigned at
*ingestion time*. Those two orders do not agree: on a live sample of 307
items the ids were NOT monotonically decreasing down the page. So an article
ingested late (high id) can carry an older timestamp and sort below articles
with lower ids. Tracking only "the highest id seen" would therefore skip
those articles permanently and silently. We keep an explicit set instead.

The set is bounded: Pulse publishes ~300 articles/day and its homepage only
reaches back ~24 hours, so anything older than the retained window can never
reappear on the page and is safe to forget.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ~300 articles/day on Pulse, so 5000 ids is roughly a two-week memory —
# far beyond the ~24 hours the homepage actually reaches back.
MAX_SEEN = 5000


class SeenStore:
    """A bounded set of article ids, persisted as JSON."""

    def __init__(self, path: str | Path, max_seen: int = MAX_SEEN):
        self.path = Path(path)
        self.max_seen = max_seen
        self.ids: set[int] = set()
        self._existed = False

    @property
    def is_first_run(self) -> bool:
        """True when no usable state file was found on disk."""
        return not self._existed

    def load(self) -> "SeenStore":
        if not self.path.exists():
            log.info("No state file at %s - treating this as a first run", self.path)
            return self

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.ids = {int(i) for i in data.get("seen_ids", [])}
            self._existed = True
            log.info("Loaded %d seen ids from %s", len(self.ids), self.path)
        except (json.JSONDecodeError, ValueError, TypeError, OSError) as exc:
            # A corrupt state file must not cause the whole backlog to be
            # re-posted. Fail closed: behave like a first run, which seeds
            # rather than floods.
            log.error("State file %s is unreadable (%s); treating as first run", self.path, exc)
            self.ids = set()
            self._existed = False

        return self

    def unseen(self, articles):
        """Return the articles whose ids are not yet in the set."""
        return [a for a in articles if a.id not in self.ids]

    def mark(self, articles) -> None:
        self.ids.update(a.id for a in articles)

    def save(self) -> None:
        # Keep the highest ids; ingestion ids only ever grow, so the largest
        # are the most recent and the only ones the homepage can still show.
        kept = sorted(self.ids)[-self.max_seen :]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seen_ids": kept, "count": len(kept)}

        # Write-then-rename so an interrupted run cannot leave a half-written
        # file that would read as corrupt on the next run.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)
        log.info("Saved %d seen ids to %s", len(kept), self.path)
