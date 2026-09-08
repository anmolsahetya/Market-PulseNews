"""Tests run against markup copied verbatim from the live Pulse page."""

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


class TestSeenStore(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state" / "seen.json"
        self.articles = parse(FIXTURE)

    def tearDown(self):
        self.dir.cleanup()

    def test_first_run_detected_then_persisted(self):
        s = SeenStore(self.path).load()
        self.assertTrue(s.is_first_run)
        self.assertEqual(len(s.unseen(self.articles)), 5)
        s.mark(self.articles)
        s.save()

        s2 = SeenStore(self.path).load()
        self.assertFalse(s2.is_first_run)
        self.assertEqual(s2.unseen(self.articles), [])

    def test_only_new_ids_returned(self):
        s = SeenStore(self.path)
        s.mark(self.articles[:3])
        self.assertEqual([a.id for a in s.unseen(self.articles)], [11610500, 11610177])

    def test_non_monotonic_ids_are_all_tracked(self):
        # The real page interleaves ids: 11610526 sorts above 11610500.
        # A high-water-mark scheme would drop 11610500; a set must not.
        s = SeenStore(self.path)
        s.mark([a for a in self.articles if a.id == 11610526])
        still_new = [a.id for a in s.unseen(self.articles)]
        self.assertIn(11610500, still_new)
        self.assertIn(11610177, still_new)

    def test_pruning_keeps_highest_ids(self):
        s = SeenStore(self.path, max_seen=3)
        s.ids = {1, 2, 3, 4, 5, 900, 901}
        s.save()
        kept = json.loads(self.path.read_text())["seen_ids"]
        self.assertEqual(kept, [5, 900, 901])

    def test_corrupt_state_falls_back_to_first_run(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json")
        s = SeenStore(self.path).load()
        self.assertTrue(s.is_first_run)
        self.assertEqual(s.ids, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
