"""Tests run against markup copied verbatim from the live Pulse page."""

import json
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pulsebot.config import parse_chat_ids  # noqa: E402
from pulsebot.scraper import Article, parse  # noqa: E402
from pulsebot.state import SeenStore  # noqa: E402
from pulsebot.telegram import MAX_MESSAGE_CHARS, render  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixture.html").read_text(encoding="utf-8")


class TestParser(unittest.TestCase):
    def setUp(self):
        self.articles = parse(FIXTURE)

    def test_extracts_every_item(self):
        self.assertEqual(len(self.articles), 5)

    def test_fields_of_first_article(self):
        a = self.articles[0]
        self.assertEqual(a.id, 11610517)
        self.assertEqual(a.title, "HCLTech opens advanced semiconductor lab in Bengaluru")
        self.assertTrue(a.url.startswith("https://www.thehindu.com/"))
        self.assertIn("semiconductor innovation", a.description)
        self.assertEqual(a.source, "The Hindu Business")

    def test_em_dash_stripped_from_source(self):
        for a in self.articles:
            self.assertFalse(a.source.startswith("—"), a.source)
        self.assertEqual({a.source for a in self.articles},
                         {"The Hindu Business", "NDTV Business", "Economic Times", "Finshots"})

    def test_timestamp_parsed_as_ist(self):
        a = self.articles[0]
        self.assertEqual(a.published.year, 2026)
        self.assertEqual(a.published.month, 9)
        self.assertEqual(a.published.day, 8)
        self.assertEqual((a.published.hour, a.published.minute), (20, 57))  # 08:57 PM
        self.assertEqual(a.published.utcoffset().total_seconds(), 5.5 * 3600)

    def test_am_timestamp(self):
        finshots = next(a for a in self.articles if a.source == "Finshots")
        self.assertEqual((finshots.published.hour, finshots.published.minute), (7, 0))

    def test_unicode_and_punctuation_survive(self):
        a = self.articles[1]
        self.assertIn("₹41,198", a.title)
        self.assertIn("insurers’", a.title)

    def test_url_fragment_preserved(self):
        a = next(a for a in self.articles if a.id == 11610526)
        self.assertTrue(a.url.endswith("#publisher=newsstand"))

    def test_missing_container_raises(self):
        with self.assertRaises(ValueError):
            parse("<html><body><p>nothing here</p></body></html>")

    def test_malformed_item_skipped_not_fatal(self):
        html = FIXTURE.replace('id="item-11610518"', 'id="broken"')
        self.assertEqual(len(parse(html)), 4)


class TestRender(unittest.TestCase):
    def setUp(self):
        self.articles = parse(FIXTURE)

    def test_apostrophes_and_percent_are_escaped_safely(self):
        a = next(x for x in self.articles if x.id == 11610526)
        out = render(a)
        # The raw apostrophe must not appear unescaped inside the anchor text
        # in a way that breaks Telegram's HTML parser.
        self.assertIn("&#x27;De-Dollarisation&#x27;", out)
        self.assertIn("90%", out)
        self.assertTrue(out.startswith("<b><a href="))

    def test_contains_link_title_source_and_time(self):
        a = self.articles[0]
        out = render(a)
        self.assertIn(a.url, out)
        self.assertIn("HCLTech", out)
        self.assertIn("The Hindu Business", out)
        self.assertIn("08 Sep 2026", out)
        self.assertIn("08:57 PM IST", out)

    def test_ampersand_in_title_is_escaped(self):
        a = Article(1, 'M&M "beats" Q1 <est>', "https://x.com/a?b=1&c=2", "Tata & Sons", "ET", None)
        out = render(a)
        self.assertIn("M&amp;M", out)
        self.assertIn("&lt;est&gt;", out)
        self.assertIn("&amp;c=2", out)
        self.assertNotIn("<est>", out)

    def test_long_description_truncated_within_limit(self):
        a = Article(2, "T" * 100, "https://x.com/a", "D" * 9000, "ET", None)
        out = render(a)
        self.assertLessEqual(len(out), MAX_MESSAGE_CHARS)
        self.assertIn("…", out)

    def test_renders_without_timestamp(self):
        a = Article(3, "No date", "https://x.com/a", "desc", "ET", None)
        self.assertIn("ET", render(a))


class TestChatIdParsing(unittest.TestCase):
    def test_comma_newline_and_space_separated(self):
        self.assertEqual(parse_chat_ids("@a,@b"), ["@a", "@b"])
        self.assertEqual(parse_chat_ids("@a, @b , -1001"), ["@a", "@b", "-1001"])
        self.assertEqual(parse_chat_ids("@a\n@b\n"), ["@a", "@b"])

    def test_blanks_and_duplicates_dropped(self):
        # A channel listed twice would otherwise receive every article twice.
        self.assertEqual(parse_chat_ids("@a,,@b,@a"), ["@a", "@b"])
        self.assertEqual(parse_chat_ids(""), [])
        self.assertEqual(parse_chat_ids("   "), [])


class TestSeenStore(unittest.TestCase):
    A, B = "@alpha", "@beta"

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state" / "seen.json"
        self.articles = parse(FIXTURE)

    def tearDown(self):
        self.dir.cleanup()

    def test_new_channel_detected_then_persisted(self):
        s = SeenStore(self.path).load([self.A])
        self.assertTrue(s.is_new_channel(self.A))
        self.assertEqual(len(s.unseen(self.A, self.articles)), 5)
        s.mark(self.A, self.articles)
        s.save()

        s2 = SeenStore(self.path).load([self.A])
        self.assertFalse(s2.is_new_channel(self.A))
        self.assertEqual(s2.unseen(self.A, self.articles), [])

    def test_channels_are_independent(self):
        """The core multi-channel guarantee: B must not inherit A's history."""
        s = SeenStore(self.path)
        s.mark(self.A, self.articles)
        self.assertEqual(s.unseen(self.A, self.articles), [])
        self.assertEqual(len(s.unseen(self.B, self.articles)), 5)

    def test_failure_in_one_channel_does_not_mark_another(self):
        s = SeenStore(self.path).load([self.A, self.B])
        s.mark(self.A, self.articles[:2])
        s.save()

        s2 = SeenStore(self.path).load([self.A, self.B])
        # B never received them, so they must still be pending for B.
        pending_b = {a.id for a in s2.unseen(self.B, self.articles)}
        self.assertEqual(len(pending_b), 5)
        self.assertEqual(len(s2.unseen(self.A, self.articles)), 3)

    def test_channel_added_later_is_new(self):
        s = SeenStore(self.path).load([self.A])
        s.mark(self.A, self.articles)
        s.save()

        s2 = SeenStore(self.path).load([self.A, self.B])
        self.assertFalse(s2.is_new_channel(self.A))
        self.assertTrue(s2.is_new_channel(self.B))

    def test_v1_state_migrates_to_every_configured_channel(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        legacy = [a.id for a in self.articles]
        self.path.write_text(json.dumps({"seen_ids": legacy, "count": len(legacy)}))

        s = SeenStore(self.path).load([self.A, self.B])
        # The original channel keeps its exact history...
        self.assertEqual(s.unseen(self.A, self.articles), [])
        # ...and a channel added at the same time starts from the same
        # baseline rather than receiving the whole backlog.
        self.assertEqual(s.unseen(self.B, self.articles), [])

    def test_non_monotonic_ids_are_all_tracked(self):
        # The real page interleaves ids: 11610526 sorts above 11610500.
        # A high-water-mark scheme would drop 11610500; a set must not.
        s = SeenStore(self.path)
        s.mark(self.A, [a for a in self.articles if a.id == 11610526])
        still_new = [a.id for a in s.unseen(self.A, self.articles)]
        self.assertIn(11610500, still_new)
        self.assertIn(11610177, still_new)

    def test_pruning_keeps_highest_ids_per_channel(self):
        s = SeenStore(self.path, max_seen=3)
        s.channels[self.A] = {1, 2, 3, 4, 5, 900, 901}
        s.channels[self.B] = {7, 8}
        s.save()
        data = json.loads(self.path.read_text())
        self.assertEqual(data["channels"][self.A]["seen_ids"], [5, 900, 901])
        self.assertEqual(data["channels"][self.B]["seen_ids"], [7, 8])

    def test_corrupt_state_falls_back_to_first_run(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json")
        s = SeenStore(self.path).load([self.A])
        self.assertTrue(s.is_new_channel(self.A))
        self.assertEqual(s.channels, {})


class TestMultiChannelRun(unittest.TestCase):
    """End-to-end fan-out with a stubbed Telegram transport."""

    A, B, C = "@alpha", "@beta", "@gamma"

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name) / "seen.json"
        self.articles = parse(FIXTURE)

    def tearDown(self):
        self.dir.cleanup()
        for k in ("DRY_RUN", "STATE_PATH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS"):
            os.environ.pop(k, None)

    def _run(self, chat_ids, failing=(), html=None):
        import pulsebot.main as m
        import pulsebot.scraper as scraper
        from pulsebot.telegram import ChannelError

        os.environ["TELEGRAM_BOT_TOKEN"] = "t"
        os.environ["TELEGRAM_CHAT_IDS"] = ",".join(chat_ids)
        os.environ["STATE_PATH"] = str(self.state)
        m.scrape = lambda *a, **k: scraper.parse(html or FIXTURE)

        sent = {}

        class FakePub:
            def __init__(self, *a, **k):
                pass

            def post_article(self, chat_id, article):
                if chat_id in failing:
                    raise ChannelError(f"cannot post to {chat_id}")
                sent.setdefault(chat_id, []).append(article.id)
                return True

        m.TelegramPublisher = FakePub
        rc = m.run()
        return rc, sent

    def _extra(self, n):
        return FIXTURE.replace("</ul>", "".join(
            f'<li class="box item" id="item-9{i:06d}"><h2 class="title">'
            f'<a href="https://e.com/{i}">S{i}</a></h2><div class="desc">d</div>'
            f'<span class="date" title="10:{i:02d} AM, 08 Sep 2026">x</span>'
            f'<span class="feed">&mdash; Economic Times</span></li>'
            for i in range(n)) + "</ul>")

    def test_first_run_seeds_all_channels_without_posting(self):
        rc, sent = self._run([self.A, self.B])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, {})

    def test_same_articles_go_to_every_channel(self):
        self._run([self.A, self.B, self.C])              # seed
        rc, sent = self._run([self.A, self.B, self.C], html=self._extra(3))
        self.assertEqual(rc, 0)
        self.assertEqual(set(sent), {self.A, self.B, self.C})
        self.assertEqual(sent[self.A], sent[self.B])
        self.assertEqual(sent[self.B], sent[self.C])
        self.assertEqual(len(sent[self.A]), 3)

    def test_no_repeats_on_a_second_run(self):
        self._run([self.A, self.B])
        self._run([self.A, self.B], html=self._extra(2))
        _, sent = self._run([self.A, self.B], html=self._extra(2))
        self.assertEqual(sent, {})

    def test_one_broken_channel_does_not_block_the_others(self):
        self._run([self.A, self.B, self.C])
        rc, sent = self._run([self.A, self.B, self.C], failing=(self.B,), html=self._extra(2))
        self.assertEqual(rc, 1, "a failing channel should surface as a non-zero exit")
        self.assertEqual(len(sent.get(self.A, [])), 2)
        self.assertEqual(len(sent.get(self.C, [])), 2)
        self.assertNotIn(self.B, sent)

    def test_broken_channel_retries_those_articles_next_run(self):
        """B's failure must not mark the articles seen for B."""
        self._run([self.A, self.B])
        self._run([self.A, self.B], failing=(self.B,), html=self._extra(2))
        _, sent = self._run([self.A, self.B], html=self._extra(2))
        self.assertNotIn(self.A, sent, "A already had them")
        self.assertEqual(len(sent.get(self.B, [])), 2, "B must receive what it missed")

    def test_channel_added_later_seeds_then_receives(self):
        self._run([self.A])
        self._run([self.A], html=self._extra(2))
        # B joins: it seeds silently rather than getting the backlog.
        _, sent = self._run([self.A, self.B], html=self._extra(2))
        self.assertNotIn(self.B, sent)
        # ...and from the next run it receives new articles like everyone else.
        _, sent2 = self._run([self.A, self.B], html=self._extra(4))
        self.assertEqual(len(sent2.get(self.B, [])), 2)
        self.assertEqual(len(sent2.get(self.A, [])), 2)


class TestDryRun(unittest.TestCase):
    """A dry run must preview articles and never write state.

    Regression: on a cold start the seeding shortcut used to return before
    printing anything, so `DRY_RUN=1 python -m pulsebot.main` showed the user
    no preview at all - and silently marked the backlog as seen.
    """

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name) / "seen.json"

    def tearDown(self):
        self.dir.cleanup()
        for k in ("DRY_RUN", "STATE_PATH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS"):
            os.environ.pop(k, None)

    def _run(self):
        import io
        import logging
        import pulsebot.main as m
        import pulsebot.scraper as scraper
        os.environ["DRY_RUN"] = "1"
        os.environ["STATE_PATH"] = str(self.state)
        os.environ["TELEGRAM_CHAT_IDS"] = "@alpha,@beta"
        m.scrape = lambda *a, **k: scraper.parse(FIXTURE)
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        logger = logging.getLogger("pulsebot")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            rc = m.run()
        finally:
            logger.removeHandler(handler)
        return rc, buf.getvalue()

    def test_cold_start_dry_run_previews_and_writes_nothing(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("would post", out)
        self.assertIn("2 channel(s) configured", out)
        self.assertFalse(self.state.exists(), "dry run must not write a state file")

    def test_dry_run_preview_is_capped(self):
        from pulsebot.main import DRY_RUN_PREVIEW
        _, out = self._run()
        self.assertLessEqual(out.count("would post"), DRY_RUN_PREVIEW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
