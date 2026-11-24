"""
Test suite for the briefing pipeline.
Run: python3 tests.py

No external services required — all tests use mocks or temp dirs.
"""
import os
import sys
import json
import tempfile
from collections import Counter
from datetime import datetime
import unittest
from unittest.mock import patch, MagicMock


# ── format tests ──────────────────────────────────────────────

class TestParseFormatmatter(unittest.TestCase):

    def setUp(self):
        from format import parse_frontmatter
        self.parse = parse_frontmatter

    def test_parses_basic_fields(self):
        md = '---\ntitle: "My Title"\ndate: 2026-04-08\ntopic: robotics\n---\n\nBody.'
        meta = self.parse(md)
        self.assertEqual(meta["title"], "My Title")
        self.assertEqual(meta["date"], "2026-04-08")
        self.assertEqual(meta["topic"], "robotics")

    def test_returns_empty_for_no_frontmatter(self):
        self.assertEqual(self.parse("No frontmatter here."), {})

    def test_strips_quotes_from_values(self):
        meta = self.parse('---\ntitle: "Quoted"\n---\n')
        self.assertEqual(meta["title"], "Quoted")


class TestBuildMarkdownMessage(unittest.TestCase):

    def setUp(self):
        from format import build_markdown_message
        self.build = build_markdown_message

    def _cfg(self, **kwargs):
        base = {"title": "Test Briefing", "ai_topic": "Test Topic",
                "briefing_title": "Morning Start", "_topic": "uk_capital_markets"}
        base.update(kwargs)
        return base

    def test_contains_frontmatter_block(self):
        msg = self.build("Content here.", {}, self._cfg())
        self.assertTrue(msg.startswith("---\n"))
        self.assertIn("\n---\n", msg)

    def test_contains_title_topic_and_pubdate(self):
        msg = self.build("Content.", {}, self._cfg())
        self.assertIn("Morning Start", msg)
        self.assertIn("uk_capital_markets", msg)
        self.assertIn("pubDate:", msg)
        self.assertIn("description:", msg)

    def test_contains_content(self):
        msg = self.build("The actual content.", {}, self._cfg())
        self.assertIn("The actual content.", msg)

    def test_falls_back_to_ai_topic(self):
        cfg = self._cfg()
        del cfg["briefing_title"]
        msg = self.build("Content.", {}, cfg)
        self.assertIn("Test Topic", msg)


class TestSplitToLimit(unittest.TestCase):

    def setUp(self):
        from format import _split_to_limit
        self.split = _split_to_limit

    def test_short_text_returned_as_is(self):
        self.assertEqual(self.split("Short text.", 280), ["Short text."])

    def test_splits_at_paragraph_boundary(self):
        text = ("A" * 150) + "\n\n" + ("B" * 150)
        chunks = self.split(text, 200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 200 for c in chunks))

    def test_splits_at_sentence_boundary(self):
        text = ("Word " * 30) + ". " + ("Word " * 30)
        chunks = self.split(text, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))

    def test_all_chunks_within_limit(self):
        text = "This is a fairly long sentence that should be split. " * 10
        chunks = self.split(text, 80)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 80)


class TestMarkdownDests(unittest.TestCase):

    def test_markdown_and_github_in_set(self):
        from format import MARKDOWN_DESTS
        self.assertIn("markdown", MARKDOWN_DESTS)
        self.assertIn("github", MARKDOWN_DESTS)


class TestValidateAstroFrontmatter(unittest.TestCase):

    def setUp(self):
        from format import validate_astro_frontmatter
        self.validate = validate_astro_frontmatter

    def _valid(self):
        return (
            '---\ntitle: "My Title"\ndescription: "A description"\n'
            'pubDate: 2026-04-08T18:00:00\ntopic: test\n---\n\nBody.'
        )

    def test_valid_frontmatter_returns_no_errors(self):
        self.assertEqual(self.validate(self._valid()), [])

    def test_missing_title_returns_error(self):
        md = '---\ndescription: "desc"\npubDate: 2026-04-08T18:00:00\n---\n'
        errors = self.validate(md)
        self.assertTrue(any("title" in e for e in errors))

    def test_missing_description_returns_error(self):
        md = '---\ntitle: "T"\npubDate: 2026-04-08T18:00:00\n---\n'
        errors = self.validate(md)
        self.assertTrue(any("description" in e for e in errors))

    def test_missing_pubdate_returns_error(self):
        md = '---\ntitle: "T"\ndescription: "D"\n---\n'
        errors = self.validate(md)
        self.assertTrue(any("pubDate" in e for e in errors))

    def test_invalid_pubdate_returns_error(self):
        md = '---\ntitle: "T"\ndescription: "D"\npubDate: not-a-date\n---\n'
        errors = self.validate(md)
        self.assertTrue(any("Invalid pubDate" in e for e in errors))

    def test_no_frontmatter_returns_error(self):
        errors = self.validate("No frontmatter here.")
        self.assertTrue(len(errors) > 0)


# ── delivery tests ────────────────────────────────────────────

class TestMarkdownDelivery(unittest.TestCase):

    def setUp(self):
        from delivery import MarkdownDelivery
        self.cls = MarkdownDelivery

    def _msg(self, topic="robotics"):
        return (
            f'---\ntitle: "Test"\ndescription: "A test."\n'
            f'pubDate: 2026-04-08T09:00:00\ntopic: {topic}\nmodel: test\n---\n\nContent.\n'
        )

    def test_writes_md_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self.cls(tmpdir)
            ok = d.send(self._msg())
            self.assertTrue(ok)
            files = os.listdir(tmpdir)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].endswith(".md"))

    def test_filename_includes_date_and_topic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self.cls(tmpdir)
            d.send(self._msg("robotics"))
            filename = os.listdir(tmpdir)[0]
            self.assertIn("2026-04-08", filename)
            self.assertIn("robotics", filename)

    def test_file_content_matches_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self.cls(tmpdir)
            msg = self._msg("test")
            d.send(msg)
            filename = os.listdir(tmpdir)[0]
            with open(os.path.join(tmpdir, filename)) as f:
                self.assertEqual(f.read(), msg)


class TestGitHubDelivery(unittest.TestCase):

    def setUp(self):
        from delivery import GitHubDelivery
        self.cls = GitHubDelivery

    def _msg(self):
        return (
            '---\ntitle: "GitHub Test"\ndescription: "A test."\n'
            'pubDate: 2026-04-08T09:00:00\ntopic: test\nmodel: test\n---\n\nTest content.\n'
        )

    def test_pushes_on_successful_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_dir = "content"
            os.makedirs(os.path.join(tmpdir, md_dir))
            d = self.cls(repo_path=tmpdir, md_dir=md_dir, branch="main")
            with patch.object(d, "_git") as mock_git:
                with patch.object(d.__class__.__bases__[0], "send", return_value=True):
                    d.send(self._msg())
                    calls = [c.args[0] for c in mock_git.call_args_list]
                    self.assertIn("add", calls)
                    self.assertIn("commit", calls)
                    self.assertIn("push", calls)

    def test_skips_git_if_file_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self.cls(repo_path=tmpdir, md_dir="content", branch="main")
            with patch.object(d.__class__.__bases__[0], "send", return_value=False):
                with patch.object(d, "_git") as mock_git:
                    result = d.send(self._msg())
                    self.assertFalse(result)
                    mock_git.assert_not_called()

    def test_skips_push_on_weekend(self):
        with patch("delivery.datetime") as mock_dt:
            mock_dt.now.return_value.weekday.return_value = 5  # Saturday
            with tempfile.TemporaryDirectory() as tmpdir:
                d = self.cls(repo_path=tmpdir, md_dir="content", branch="main")
                with patch.object(d, "_git") as mock_git:
                    result = d.send(self._msg())
                    self.assertTrue(result)
                    mock_git.assert_not_called()

    def test_returns_false_on_git_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_dir = "content"
            os.makedirs(os.path.join(tmpdir, md_dir))
            d = self.cls(repo_path=tmpdir, md_dir=md_dir, branch="main")
            with patch.object(d.__class__.__bases__[0], "send", return_value=True):
                with patch.object(d, "_git", side_effect=RuntimeError("auth failed")):
                    result = d.send(self._msg())
                    self.assertFalse(result)


class TestMakeDelivery(unittest.TestCase):

    def test_dry_run_returns_console(self):
        from delivery import make_delivery, ConsoleDelivery
        d = make_delivery("telegram", dry_run=True)
        self.assertIsInstance(d, ConsoleDelivery)

    def test_console_dest(self):
        from delivery import make_delivery, ConsoleDelivery
        d = make_delivery("console", dry_run=False)
        self.assertIsInstance(d, ConsoleDelivery)

    def test_markdown_dest(self):
        from delivery import make_delivery, MarkdownDelivery
        with patch.dict(os.environ, {"MARKDOWN_OUTPUT_DIR": "/tmp/md"}):
            d = make_delivery("markdown", dry_run=False)
            self.assertIsInstance(d, MarkdownDelivery)

    def test_unknown_dest_exits(self):
        from delivery import make_delivery
        with self.assertRaises(SystemExit):
            make_delivery("unknown_dest", dry_run=False)

    def test_multi_dest(self):
        from delivery import make_delivery, MultiDelivery
        with patch.dict(os.environ, {"MARKDOWN_OUTPUT_DIR": "/tmp/md"}):
            d = make_delivery("console,markdown", dry_run=False)
            self.assertIsInstance(d, MultiDelivery)


# ── search / mock tests ───────────────────────────────────────

class TestSearchPagination(unittest.TestCase):

    def _page(self, n, offset=0):
        return [{"url": f"https://example.com/{offset+i}", "title": f"Article {offset+i}", "content": "", "publishedDate": ""} for i in range(n)]

    def test_single_page_when_count_within_page_size(self):
        from search import search_searxng
        with patch("search._search_page", return_value=self._page(5)) as mock:
            results = search_searxng("test", count=5)
            self.assertEqual(mock.call_count, 1)
            self.assertEqual(len(results), 5)

    def test_paginates_when_count_exceeds_page_size(self):
        from search import search_searxng
        pages = [self._page(10, offset=i*10) for i in range(3)]
        with patch("search._search_page", side_effect=pages):
            results = search_searxng("test", count=25)
            self.assertEqual(len(results), 25)

    def test_stops_when_no_new_results(self):
        from search import search_searxng
        with patch("search._search_page", return_value=self._page(3)):
            results = search_searxng("test", count=20)
            self.assertEqual(len(results), 3)

    def test_deduplicates_across_pages(self):
        from search import search_searxng
        same_page = self._page(5)
        with patch("search._search_page", return_value=same_page):
            results = search_searxng("test", count=10)
            self.assertEqual(len(results), 5)


class TestMockFetchResults(unittest.TestCase):

    def test_returns_one_section_per_search(self):
        from search import mock_fetch_results
        searches = [
            {"title": "NEWS", "emoji": "📰", "count": 3},
            {"title": "TECH", "emoji": "💻", "count": 2},
        ]
        results = mock_fetch_results(searches)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["section"], "NEWS")
        self.assertEqual(len(results[0]["results"]), 3)

    def test_each_result_has_required_keys(self):
        from search import mock_fetch_results
        results = mock_fetch_results([{"title": "X", "emoji": "🔹", "count": 2}])
        for r in results[0]["results"]:
            self.assertIn("title", r)
            self.assertIn("url", r)
            self.assertIn("snippet", r)


# ── ai mock tests ─────────────────────────────────────────────

class TestMockOutput(unittest.TestCase):

    def _cfg(self):
        return {"ai_topic": "Test Topic", "title": "Test", "_topic": "test",
                "briefing_title": ""}

    def test_narrative_mock_returns_string(self):
        from ai import mock_output
        result = mock_output({"type": "narrative"}, self._cfg())
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_tweet_mock_returns_string(self):
        from ai import mock_output
        result = mock_output({"type": "tweet"}, self._cfg())
        self.assertIsInstance(result, str)

    def test_thread_mock_uses_paragraph_splitting(self):
        from ai import mock_output
        result = mock_output({"type": "thread", "max_chars_per_post": 280,
                               "numbered": True}, self._cfg())
        self.assertIsInstance(result, str)
        posts = result.split("\n---\n")
        self.assertGreater(len(posts), 1)
        for post in posts:
            self.assertLessEqual(len(post), 280)

    def test_thread_mock_uses_source_content(self):
        from ai import mock_output
        source = "Para one content.\n\nPara two content.\n\nPara three content."
        result = mock_output({"type": "thread", "max_chars_per_post": 280,
                               "numbered": True}, self._cfg(),
                             source_content=source)
        self.assertIn("Para one", result)
        self.assertIn("Para two", result)


# ── research tests ───────────────────────────────────────────

class TestExtractArticles(unittest.TestCase):

    def _rows(self, articles):
        results = json.dumps([{"section": "TEST", "emoji": "🔹", "results": articles}])
        return [("2026-04-16T10:00:00", "uk_capital_markets", results)]

    def test_returns_all_articles_without_filter(self):
        from research import _extract_articles
        rows = self._rows([
            {"title": "Barclays rises", "url": "https://a.com/1", "snippet": "Up 2%", "published": ""},
            {"title": "HSBC falls",     "url": "https://a.com/2", "snippet": "Down 1%", "published": ""},
        ])
        articles = _extract_articles(rows)
        self.assertEqual(len(articles), 2)

    def test_keyword_filter_matches_title(self):
        from research import _extract_articles
        rows = self._rows([
            {"title": "Barclays Q1 results", "url": "https://a.com/1", "snippet": "Strong quarter", "published": ""},
            {"title": "HSBC outlook",        "url": "https://a.com/2", "snippet": "Cautious tone",  "published": ""},
        ])
        articles = _extract_articles(rows, keyword="barclays")
        self.assertEqual(len(articles), 1)
        self.assertIn("Barclays", articles[0]["title"])

    def test_keyword_filter_matches_snippet(self):
        from research import _extract_articles
        rows = self._rows([
            {"title": "UK Banks Update", "url": "https://a.com/1", "snippet": "Barclays leads gains", "published": ""},
            {"title": "Gilt Yields",     "url": "https://a.com/2", "snippet": "BoE holds rates",      "published": ""},
        ])
        articles = _extract_articles(rows, keyword="barclays")
        self.assertEqual(len(articles), 1)

    def test_keyword_filter_is_case_insensitive(self):
        from research import _extract_articles
        rows = self._rows([
            {"title": "BARCLAYS UPDATE", "url": "https://a.com/1", "snippet": "", "published": ""},
        ])
        self.assertEqual(len(_extract_articles(rows, keyword="barclays")), 1)
        self.assertEqual(len(_extract_articles(rows, keyword="Barclays")), 1)

    def test_deduplicates_by_url(self):
        from research import _extract_articles
        article = {"title": "Same article", "url": "https://a.com/1", "snippet": "", "published": ""}
        rows = self._rows([article, article])
        articles = _extract_articles(rows)
        self.assertEqual(len(articles), 1)

    def test_article_has_expected_keys(self):
        from research import _extract_articles
        rows = self._rows([{"title": "Test", "url": "https://a.com/1", "snippet": "snip", "published": "2026-04-16"}])
        a = _extract_articles(rows)[0]
        for key in ("collected", "topic", "section", "title", "url", "snippet", "published"):
            self.assertIn(key, a)

    def test_empty_rows_returns_empty(self):
        from research import _extract_articles
        self.assertEqual(_extract_articles([]), [])


class TestQueryDb(unittest.TestCase):

    def test_returns_empty_for_missing_db(self):
        from research import _query_db
        result = _query_db("/nonexistent/path/db.db", "topic", None, None)
        self.assertEqual(result, [])

    def test_queries_by_topic(self):
        import sqlite3
        from research import _query_db
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE collections (id INTEGER PRIMARY KEY, timestamp TEXT, topic TEXT, results TEXT)")
            con.execute("INSERT INTO collections VALUES (1, '2026-04-16T10:00:00', 'robotics', '[]')")
            con.execute("INSERT INTO collections VALUES (2, '2026-04-16T11:00:00', 'uk_capital_markets', '[]')")
            con.commit()
            con.close()
            rows = _query_db(db_path, "robotics", None, None)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], "robotics")
        finally:
            os.unlink(db_path)

    def test_filters_by_date_range(self):
        import sqlite3
        from research import _query_db
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE collections (id INTEGER PRIMARY KEY, timestamp TEXT, topic TEXT, results TEXT)")
            con.execute("INSERT INTO collections VALUES (1, '2026-04-01T10:00:00', 'test', '[]')")
            con.execute("INSERT INTO collections VALUES (2, '2026-04-15T10:00:00', 'test', '[]')")
            con.commit()
            con.close()
            rows = _query_db(db_path, "test", "2026-04-10", None)
            self.assertEqual(len(rows), 1)
            self.assertIn("2026-04-15", rows[0][0])
        finally:
            os.unlink(db_path)


class TestArticlesFromLive(unittest.TestCase):

    def _mock_results(self):
        return [
            {"title": "Live Article", "url": "https://live.com/1",
             "content": "Some snippet", "publishedDate": "2026-04-16"}
        ]

    def test_calls_searxng_with_correct_time_range(self):
        from research import _articles_from_live
        with patch("research.search_searxng", return_value=self._mock_results()) as mock_search:
            _articles_from_live(["gilts UK"], time_range="week")
            mock_search.assert_called_once_with("gilts UK", count=5, category="news", time_range="week")

    def test_default_time_range_is_day(self):
        from research import _articles_from_live
        with patch("research.search_searxng", return_value=self._mock_results()) as mock_search:
            _articles_from_live(["gilts UK"])
            _, kwargs = mock_search.call_args
            self.assertEqual(kwargs["time_range"], "day")

    def test_returns_articles_with_expected_keys(self):
        from research import _articles_from_live
        with patch("research.search_searxng", return_value=self._mock_results()):
            articles = _articles_from_live(["test query"])
            self.assertEqual(len(articles), 1)
            a = articles[0]
            self.assertEqual(a["title"], "Live Article")
            self.assertEqual(a["published"], "2026-04-16")

    def test_deduplicates_across_queries(self):
        from research import _articles_from_live
        with patch("research.search_searxng", return_value=self._mock_results()):
            articles = _articles_from_live(["query one", "query two"])
            self.assertEqual(len(articles), 1)

    def test_empty_results_returns_empty(self):
        from research import _articles_from_live
        with patch("research.search_searxng", return_value=[]):
            articles = _articles_from_live(["nothing"])
            self.assertEqual(articles, [])


# ── company export tests ──────────────────────────────────────

def _rns_row(rns, ticker, ts, name="Acme Plc", category="Results", score=None,
             insight=None, themes=None, headline="Final Results"):
    return {
        "rns_number": str(rns), "timestamp": ts, "company_name": name, "ticker": ticker,
        "headline": headline, "url": f"https://example.com/{rns}", "category": category,
        "impact_score": score, "insight": insight, "key_themes": themes,
    }


class TestCompanyExportHelpers(unittest.TestCase):

    def test_slug_is_name_plus_ticker(self):
        from export_companies import make_slug
        self.assertEqual(make_slug("Ceres Power Holdings", "CWR"), "ceres-power-holdings-cwr")

    def test_slug_handles_dotted_ticker_and_punctuation(self):
        from export_companies import make_slug
        self.assertEqual(make_slug("BT Group PLC (The)", "BT.A"), "bt-group-bt-a")

    def test_slug_strips_accents(self):
        from export_companies import slugify
        self.assertEqual(slugify("Société Générale"), "societe-generale")

    def test_slug_falls_back_to_ticker_when_name_empty(self):
        from export_companies import make_slug
        self.assertEqual(make_slug("", "0HAF"), "0haf")

    def test_slug_strips_share_class_and_legal_form(self):
        from export_companies import make_slug
        self.assertEqual(make_slug("Amedeo Air Four Plus Limited Red Ord NPV", "AA4"),
                         "amedeo-air-four-plus-aa4")

    def test_slug_name_variants(self):
        from export_companies import slug_name
        cases = {
            "Nolato AB B Ord Shs":           "Nolato",
            "Brookfield Corporation Clas":   "Brookfield",
            "Brookfield Corporation Cls A":  "Brookfield",
            "Taseko Mines Limited NPV (DI)": "Taseko Mines",
            "Hemnet Group AB (PUBL)":        "Hemnet Group",
            "Sdiptech Ab (Publ)":            "Sdiptech",
            "Acme PLC Ord 0.1p":             "Acme",
            "Acme Plc ORD GBP0.01":          "Acme",
            "Ceres Power Holdings":          "Ceres Power Holdings",
            "Plus 500":                      "Plus 500",
        }
        for raw, expected in cases.items():
            self.assertEqual(slug_name(raw), expected, raw)

    def test_slug_name_drops_repeated_short_name(self):
        from export_companies import slug_name
        cases = {
            "SIEMENS AG SIEMENS N ORD SHS":  "SIEMENS",
            "Genmab A/S Genmab Ord Shs":     "Genmab",
            "Nordex SE Nordex Ord Shs":      "Nordex",
            "IRISH RESIDENTIAL PROPERTIES REIT P IRISH RESIDENTIAL PROPERTIES ORD SHS":
                                             "IRISH RESIDENTIAL PROPERTIES REIT",
            "Mahindra & Mahindra":           "Mahindra & Mahindra",
        }
        for raw, expected in cases.items():
            self.assertEqual(slug_name(raw), expected, raw)

    def test_slug_name_legal_form_variants(self):
        from export_companies import slug_name
        cases = {
            "Coloplast A/S B Ord Shs":                  "Coloplast",
            "EFG-HERMES HOLDING S.A.E.":                "EFG-HERMES HOLDING",
            "TMS ISSUER S.A.R.L.":                      "TMS ISSUER",
            "CTS Eventim AG & Co KGAA":                 "CTS Eventim",
            "Elecosoft Public Limited Company":         "Elecosoft",
            "BH Macro Ltd. GBP Shares":                 "BH Macro",
            "Fair Oaks Income Limited 2021 Shs NPV":    "Fair Oaks Income",
            "Ruffer Investment Company Ltd Red PTG Pref Shares": "Ruffer Investment Company",
        }
        for raw, expected in cases.items():
            self.assertEqual(slug_name(raw), expected, raw)

    def test_share_class_words_kept_once_legal_form_stripped(self):
        from export_companies import slug_name
        self.assertEqual(slug_name("LEVERAGE SHARES PUBLIC LIMITED COMPANY"), "LEVERAGE SHARES")
        self.assertEqual(slug_name("DP Aircraft I Limited Pref"), "DP Aircraft I")

    def test_slug_name_never_strips_to_empty(self):
        from export_companies import slug_name
        self.assertEqual(slug_name("Ord Shs"), "Ord")
        self.assertEqual(slug_name("(DI)"), "(DI)")

    def test_debt_programme_issuer_detection(self):
        from export_companies import is_debt_programme_issuer
        debt = [_rns_row(1, "BAH", "2026-05-01T09:00:00", headline="Publication of Final Terms"),
                _rns_row(2, "BAH", "2026-05-02T09:00:00", headline="Final Terms - XS1234567890")]
        self.assertTrue(is_debt_programme_issuer(debt))
        mixed = debt + [_rns_row(3, "BAH", "2026-05-03T09:00:00", headline="Annual Report")]
        self.assertFalse(is_debt_programme_issuer(mixed))
        self.assertFalse(is_debt_programme_issuer([]))

    def test_key_themes_json_array(self):
        from export_companies import parse_key_themes
        self.assertEqual(parse_key_themes('["buyback", "capital return"]'), ["buyback", "capital return"])

    def test_key_themes_null_and_empty(self):
        from export_companies import parse_key_themes
        self.assertEqual(parse_key_themes(None), [])
        self.assertEqual(parse_key_themes(""), [])

    def test_key_themes_non_json_string_wrapped(self):
        from export_companies import parse_key_themes
        self.assertEqual(parse_key_themes("mining"), ["mining"])
        self.assertEqual(parse_key_themes('"mining"'), ["mining"])

    def test_invalid_timestamp_returns_none(self):
        from export_companies import parse_timestamp
        self.assertIsNone(parse_timestamp("25 May 2026 05:45 XM"))
        self.assertIsNone(parse_timestamp(None))
        self.assertIsNotNone(parse_timestamp("2026-05-25T17:45:00"))

    def test_parses_iso_timestamp(self):
        from export_companies import parse_timestamp
        from datetime import datetime
        self.assertEqual(parse_timestamp("2026-05-22T22:00:00"), datetime(2026, 5, 22, 22, 0))

    def test_parses_investegate_native_timestamp(self):
        from export_companies import parse_timestamp
        from datetime import datetime
        self.assertEqual(parse_timestamp("21 Sep 2026 07:32 AM"), datetime(2026, 9, 21, 7, 32))
        self.assertEqual(parse_timestamp("03 Nov 2026 04:15 PM"), datetime(2026, 11, 3, 16, 15))

    def test_parses_timestamp_variants(self):
        from export_companies import parse_timestamp
        from datetime import datetime
        expected = datetime(2026, 9, 21, 7, 32)
        for raw in ("21 September 2026 07:32 AM", "21 Sept 2026 07:32 AM",
                    "21 Sep 2026 07:32", "  21  Sep 2026 07:32 AM "):
            self.assertEqual(parse_timestamp(raw), expected, raw)

    def test_normalise_timestamp_to_iso(self):
        from export_companies import normalise_timestamp
        self.assertEqual(normalise_timestamp("21 Sep 2026 07:32 PM"), "2026-09-21T19:32:00")
        self.assertEqual(normalise_timestamp("2026-05-22T22:00:00"), "2026-05-22T22:00:00")
        self.assertEqual(normalise_timestamp("garbage"), "garbage")
        self.assertIsNone(normalise_timestamp(None))

    def test_unenriched_row_has_explicit_nulls(self):
        from export_companies import format_announcement
        a = format_announcement(_rns_row(1, "ACM", "2026-05-01T09:00:00", category=""))
        self.assertFalse(a["enriched"])
        for key in ("impact_score", "insight", "key_themes", "category"):
            self.assertIn(key, a)
            self.assertIsNone(a[key])

    def test_low_score_is_distinguishable_from_unenriched(self):
        from export_companies import format_announcement
        a = format_announcement(_rns_row(1, "ACM", "2026-05-01T09:00:00", score=1,
                                         insight="Routine.", themes=None))
        self.assertTrue(a["enriched"])
        self.assertEqual(a["impact_score"], 1)
        self.assertEqual(a["key_themes"], [])

    def test_exported_fields_exclude_anything_else(self):
        from export_companies import format_announcement
        row = _rns_row(1, "ACM", "2026-05-01T09:00:00")
        row["body"] = "SHOULD NEVER APPEAR"
        a = format_announcement(row)
        self.assertEqual(set(a), {"timestamp", "category", "headline", "url", "routine_type", "enriched",
                                  "impact_score", "insight", "key_themes"})

    def test_routine_type_exported(self):
        from export_companies import format_announcement
        row = _rns_row(1, "ACM", "2026-05-01T09:00:00")
        row.update(headline="Transaction in Own Shares", category="")
        self.assertEqual(format_announcement(row)["routine_type"], "Transaction in Own Shares")
        row.update(headline="Result of Placing and TVR", category="Placing")
        self.assertIsNone(format_announcement(row)["routine_type"])


class TestExportCompanies(unittest.TestCase):

    COMPANIES = os.path.join("src", "data", "companies")

    def setUp(self):
        self.tmp  = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.git  = MagicMock()
        self.dir  = os.path.join(self.repo, self.COMPANIES)
        self.map  = os.path.join(self.repo, "src", "data", "company-slugs.json")
        self.idx  = os.path.join(self.repo, "src", "data", "companies-index.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, name="Acme Plc"):
        return [
            _rns_row(1, "ACM", "2026-05-01T09:00:00", name=name),
            _rns_row(2, "ACM", "2026-05-03T09:00:00", name=name, score=7,
                     insight="Beat.", themes='["growth"]'),
            _rns_row(3, "ACM", "2026-05-02T09:00:00", name=name),
            _rns_row(4, "SML", "2026-05-04T09:00:00", name="Small Co"),
            _rns_row(5, "",    "2026-05-04T09:00:00", name="No Ticker Ltd"),
        ]

    def _run(self, rows, **kwargs):
        from export_companies import export_companies
        with patch("export_companies.log"):
            return export_companies(rows, self.repo, self.COMPANIES, git=self.git, **kwargs)

    def _load(self, path):
        with open(path) as f:
            return json.load(f)

    def test_exports_only_companies_at_or_above_minimum(self):
        s = self._run(self._rows())
        self.assertEqual(os.listdir(self.dir), ["acme-acm.json"])
        self.assertEqual(s["exported"], 1)
        self.assertEqual(s["below_min"], 1)
        self.assertEqual(s["no_ticker_rows"], 1)

    def test_minimum_is_configurable(self):
        s = self._run(self._rows(), min_announcements=1)
        self.assertEqual(s["exported"], 2)

    def test_company_file_contents(self):
        self._run(self._rows())
        c = self._load(os.path.join(self.dir, "acme-acm.json"))
        self.assertEqual(c["name"], "Acme Plc")
        self.assertEqual(c["ticker"], "ACM")
        self.assertEqual(c["slug"], "acme-acm")
        self.assertEqual(c["announcement_count"], 3)
        self.assertEqual(c["latest_announcement"], "2026-05-03T09:00:00")
        self.assertEqual([a["timestamp"][:10] for a in c["announcements"]],
                         ["2026-05-03", "2026-05-02", "2026-05-01"])
        self.assertEqual(c["announcements"][0]["key_themes"], ["growth"])
        self.assertIsNone(c["announcements"][1]["impact_score"])

    def test_invalid_timestamp_sorted_last_and_excluded_from_latest(self):
        rows = self._rows() + [_rns_row(9, "ACM", "garbage 99 XM")]
        s = self._run(rows)
        c = self._load(os.path.join(self.dir, "acme-acm.json"))
        self.assertEqual(c["latest_announcement"], "2026-05-03T09:00:00")
        self.assertEqual(c["announcements"][-1]["timestamp"], "garbage 99 XM")
        self.assertEqual(s["invalid_timestamps"], 1)

    def test_mixed_timestamp_formats_sort_and_normalise(self):
        rows = [
            _rns_row(1, "MIX", "2026-05-22T22:00:00", name="Mixed Holdings"),
            _rns_row(2, "MIX", "21 Sep 2026 07:32 AM", name="Mixed Holdings"),
            _rns_row(3, "MIX", "03 Jun 2026 04:15 PM", name="Mixed Holdings"),
            _rns_row(4, "MIX", "not a date", name="Mixed Holdings"),
        ]
        s = self._run(rows)
        c = self._load(os.path.join(self.dir, "mixed-holdings-mix.json"))
        self.assertEqual([a["timestamp"] for a in c["announcements"]],
                         ["2026-09-21T07:32:00", "2026-06-03T16:15:00",
                          "2026-05-22T22:00:00", "not a date"])
        self.assertEqual(c["latest_announcement"], "2026-09-21T07:32:00")
        self.assertEqual(s["invalid_timestamps"], 1)

    def test_index_orders_across_timestamp_formats(self):
        rows = [_rns_row(i, "ISO", f"2026-05-2{i}T09:00:00", name="Iso Co") for i in range(1, 4)]
        rows += [_rns_row(10 + i, "NAT", f"0{i} Oct 2026 09:00 AM", name="Native Co") for i in range(1, 4)]
        self._run(rows)
        index = self._load(self.idx)
        self.assertEqual([c["ticker"] for c in index], ["NAT", "ISO"])
        self.assertEqual(index[0]["latest_announcement"], "2026-10-03T09:00:00")

    def test_skips_debt_programme_issuers(self):
        rows = self._rows() + [
            _rns_row(20 + i, "ANG", f"2026-05-1{i}T09:00:00", name="Republic of Angola",
                     headline="Publication of Final Terms") for i in range(3)]
        s = self._run(rows)
        self.assertEqual(os.listdir(self.dir), ["acme-acm.json"])
        self.assertEqual(s["debt_programme"], 1)
        self.assertEqual(s["below_min"], 1)
        self.assertNotIn("ANG", self._load(self.map))

    def test_share_class_stripped_from_slug_but_not_display_name(self):
        rows = [_rns_row(i, "AA4", f"2026-05-0{i}T09:00:00",
                         name="Amedeo Air Four Plus Limited Red Ord NPV") for i in range(1, 4)]
        self._run(rows)
        c = self._load(os.path.join(self.dir, "amedeo-air-four-plus-aa4.json"))
        self.assertEqual(c["name"], "Amedeo Air Four Plus Limited Red Ord NPV")

    def test_existing_slug_kept_when_strip_rules_change(self):
        os.makedirs(os.path.dirname(self.map))
        with open(self.map, "w") as f:
            json.dump({"ACM": "acme-plc-acm"}, f)
        self._run(self._rows())
        self.assertEqual(os.listdir(self.dir), ["acme-plc-acm.json"])

    def test_writes_index(self):
        self._run(self._rows())
        index = self._load(self.idx)
        self.assertEqual(index, [{"name": "Acme Plc", "ticker": "ACM", "slug": "acme-acm",
                                  "announcement_count": 3,
                                  "latest_announcement": "2026-05-03T09:00:00"}])

    def test_commits_and_pushes_only_export_paths(self):
        self._run(self._rows())
        subcommands = [c.args[1] for c in self.git.call_args_list]
        self.assertEqual(subcommands, ["pull", "add", "commit", "pull", "push"])
        commit_args = self.git.call_args_list[2].args
        self.assertIn(self.COMPANIES, commit_args)
        self.assertIn("--", commit_args)

    def test_second_identical_run_changes_nothing(self):
        self._run(self._rows())
        self.git.reset_mock()
        s = self._run(self._rows())
        self.assertEqual(s["new"] + s["changed"], 0)
        self.assertEqual(s["unchanged"], 1)
        self.assertEqual([c.args[1] for c in self.git.call_args_list], ["pull"])

    def test_pulls_before_reading_and_aborts_if_pull_fails(self):
        self.git.side_effect = RuntimeError("cannot pull with rebase")
        with self.assertRaises(RuntimeError):
            self._run(self._rows())
        self.assertFalse(os.path.exists(os.path.join(self.repo, "src")))

    def test_slug_is_sticky_across_rename(self):
        self._run(self._rows())
        s = self._run(self._rows(name="Acme Holdings Group"))
        self.assertEqual(os.listdir(self.dir), ["acme-acm.json"])
        c = self._load(os.path.join(self.dir, "acme-acm.json"))
        self.assertEqual(c["name"], "Acme Holdings Group")
        self.assertEqual(s["pruned"], 0)

    def test_slug_recovered_from_files_if_map_lost(self):
        self._run(self._rows())
        os.remove(self.map)
        self._run(self._rows(name="Acme Holdings Group"))
        self.assertEqual(os.listdir(self.dir), ["acme-acm.json"])
        self.assertEqual(self._load(self.map), {"ACM": "acme-acm"})

    def test_corrupt_slug_map_aborts(self):
        os.makedirs(os.path.dirname(self.map))
        with open(self.map, "w") as f:
            f.write("{not json")
        with self.assertRaises(RuntimeError):
            self._run(self._rows())

    def test_new_slug_collision_gets_suffix(self):
        os.makedirs(os.path.dirname(self.map))
        with open(self.map, "w") as f:
            json.dump({"OLD": "acme-acm"}, f)
        self._run(self._rows())
        self.assertIn("acme-acm-2.json", os.listdir(self.dir))

    def test_prunes_stale_files(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "gone-co-gne.json"), "w") as f:
            json.dump({"ticker": "GNE", "slug": "gone-co-gne"}, f)
        s = self._run(self._rows())
        self.assertEqual(s["pruned"], 1)
        self.assertEqual(os.listdir(self.dir), ["acme-acm.json"])

    def test_dry_run_writes_nothing(self):
        s = self._run(self._rows(), dry_run=True)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "src")))
        self.assertEqual(s["new"], 1)
        self.git.assert_not_called()

    def test_limit_skips_index_prune_and_git(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "gone-co-gne.json"), "w") as f:
            json.dump({"ticker": "GNE", "slug": "gone-co-gne"}, f)
        s = self._run(self._rows(), limit=1, min_announcements=1)
        self.assertEqual(s["exported"], 1)
        self.assertEqual(s["pruned"], 0)
        self.assertFalse(os.path.exists(self.idx))
        self.assertIn("gone-co-gne.json", os.listdir(self.dir))
        self.git.assert_not_called()

    def test_git_failure_raises(self):
        self.git.side_effect = RuntimeError("auth failed")
        with self.assertRaises(RuntimeError):
            self._run(self._rows())


class TestGetRnsForExport(unittest.TestCase):

    def test_returns_stored_fields_including_unenriched(self):
        import db
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "test.db")), patch("db.log"):
                db.init_db()
                db.save_rns_announcements([
                    {"rns_number": "1", "timestamp": "2026-05-01T09:00:00", "company_name": "Acme",
                     "ticker": "ACM", "headline": "Results", "url": "u1", "category": "Results"},
                    {"rns_number": "2", "timestamp": "2026-05-02T09:00:00", "company_name": "Acme",
                     "ticker": "ACM", "headline": "AGM", "url": "u2", "category": "AGM/EGM"},
                ])
                db.update_rns_enrichment("1", 6, "Solid.", ["growth"])
                rows = {r["rns_number"]: r for r in db.get_rns_for_export()}
        self.assertEqual(rows["1"]["impact_score"], 6)
        self.assertEqual(json.loads(rows["1"]["key_themes"]), ["growth"])
        self.assertIsNone(rows["2"]["impact_score"])
        self.assertIsNone(rows["2"]["key_themes"])


# ── headline categorisation ───────────────────────────────────

class TestDetectCategory(unittest.TestCase):

    CASES = [
        # Results, incl. report publications; meeting results are not Results
        ("Final Results", "Results"),
        ("Annual Financial Report", "Results"),
        ("Half-year Financial Report", "Results"),
        ("Half-Yearly Report", "Results"),
        ("Interim Management Statement", "Results"),
        ("Results of AGM", "AGM/EGM"),
        ("Result of General Meeting", "AGM/EGM"),
        ("Results of Court Meeting and General Meeting", "AGM/EGM"),
        ("Annual Financial Report and Notice of AGM", "Results"),
        # New listings — bare admission to trading is not one
        ("Admission to AIM and First Day of Dealings", "New Listing"),
        ("Publication of Prospectus and Intention to Float", "New Listing"),
        ("Pricing of the IPO of METKA", "New Listing"),
        ("Admission to Trading", None),
        ("Admission of Further Securities to Trading", None),
        # Acquisition — stems now match their full words
        ("Acquisition of ClanTect Limited", "Acquisition"),
        ("GSK plc to acquire BIOG portfolio company", "Acquisition"),
        ("Statement re Possible Offer", "Acquisition"),
        ("Extension of PUSU Deadline", "Acquisition"),
        ("Irish Takeover Panel - Form 8 - DCC Energy plc", None),
        # Director dealing — TR-1 is a major-holder notice, not a dealing
        ("Director/PDMR Shareholding", "Director Dealing"),
        ("Director Dealings", "Director Dealing"),
        ("Notification of Managers’ and Closely Related", "Director Dealing"),
        ("TR-1: Notification of Major Holdings", None),
        ("Holding(s) in Company", None),
        # Placing — fundraise stems, retail offers; issue of equity is not a placing
        ("Intention to Fundraise", "Placing"),
        ("Result of Retail Offer", "Placing"),
        ("Issue of Equity", None),
        # Appointment
        ("Directorate change", "Appointment"),
        ("Board Changes", "Appointment"),
        ("Appointment of Non-Executive Director", "Appointment"),
        ("Resignation of Chief Financial Officer", "Appointment"),
        ("Director Declaration", None),
        # Third-party research and routine filings stay uncategorised
        ("Results analysis from Kepler Trust Intelligence", None),
        ("Transaction in Own Shares", None),
        ("Total Voting Rights", None),
        ("", None),
    ]

    def test_cases(self):
        from collect import _detect_category
        for headline, expected in self.CASES:
            with self.subTest(headline=headline):
                self.assertEqual(_detect_category(headline), expected)

    def test_new_categories_are_high_signal(self):
        import db
        self.assertIn("New Listing", db._HIGH_SIGNAL_CATEGORIES)
        self.assertIn("Appointment", db._HIGH_SIGNAL_CATEGORIES)


class TestBackfillCategories(unittest.TestCase):

    NOW = datetime(2026, 11, 22, 18, 0)

    def _row(self, n, headline, category, ts="2026-06-01T09:00:00", score=None, published=None):
        return {"rns_number": str(n), "timestamp": ts, "headline": headline, "category": category,
                "impact_score": score, "telegram_published_at": published}

    def _plan(self, rows):
        from backfill_categories import plan_changes
        return {c["rns_number"]: c for c in plan_changes(rows, self.NOW)}

    def test_unchanged_rows_are_skipped(self):
        self.assertEqual(self._plan([self._row(1, "Final Results", "Results", score=6)]), {})

    def test_leaving_enriched_category_clears_enrichment(self):
        c = self._plan([self._row(1, "TR-1: Notification of Major Holdings", "Director Dealing", score=4,
                                  published="2026-06-01T09:30:00")])["1"]
        self.assertEqual((c["old"], c["new"], c["clear"], c["guard"]), ("Director Dealing", "", True, False))

    def test_new_old_high_signal_row_is_guarded(self):
        c = self._plan([self._row(1, "Annual Financial Report", "")])["1"]
        self.assertEqual((c["new"], c["clear"], c["guard"]), ("Results", False, True))

    def test_recent_row_is_not_guarded(self):
        c = self._plan([self._row(1, "Annual Financial Report", "", ts="2026-11-22T09:00:00")])["1"]
        self.assertFalse(c["guard"])
        self.assertTrue(c["pending"])

    def test_widened_set_guards_unchanged_appointment(self):
        # Already "Appointment", unchanged — but newly enrichable, so must not be posted
        c = self._plan([self._row(1, "Board Change", "Appointment")])["1"]
        self.assertEqual((c["old"], c["new"], c["guard"]), ("Appointment", "Appointment", True))

    def test_existing_backlog_is_not_guarded(self):
        # Unenriched Results row was already due to publish — behaviour unchanged
        self.assertEqual(self._plan([self._row(1, "Final Results", "Results")]), {})

    def test_reenriched_move_within_high_signal_is_guarded_if_unpublished(self):
        c = self._plan([self._row(1, "Trading Update and Completion of Acquisition", "Trading Update", score=5)])["1"]
        self.assertEqual((c["new"], c["clear"], c["guard"]), ("Acquisition", True, True))

    def test_already_published_row_is_not_reguarded(self):
        c = self._plan([self._row(1, "Directorate change", "", published="2026-06-01T10:00:00")])["1"]
        self.assertFalse(c["guard"])

    def test_unparseable_timestamp_is_guarded(self):
        c = self._plan([self._row(1, "Directorate change", "", ts="garbage")])["1"]
        self.assertTrue(c["guard"])

    def test_apply_against_db(self):
        import db
        from backfill_categories import plan_changes, apply_changes
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "test.db")), patch("db.log"):
                db.init_db()
                db.save_rns_announcements([
                    {"rns_number": "1", "timestamp": "2026-06-01T09:00:00", "company_name": "DCC",
                     "ticker": "DCC", "headline": "Irish Takeover Panel - Form 8 - DCC plc", "url": "u",
                     "category": "Acquisition"},
                    {"rns_number": "2", "timestamp": "2026-06-01T09:00:00", "company_name": "Acme",
                     "ticker": "ACM", "headline": "Directorate change", "url": "u", "category": ""},
                    {"rns_number": "3", "timestamp": "2026-11-22T09:00:00", "company_name": "Acme",
                     "ticker": "ACM", "headline": "Annual Financial Report", "url": "u", "category": ""},
                ])
                db.update_rns_enrichment("1", 7, "Wrong premise.", ["m&a"])
                con = db._connect()
                from backfill_categories import load_rows
                apply_changes(con, plan_changes(load_rows(con), self.NOW), "2026-11-22T18:00:00")
                got = {r[0]: r[1:] for r in con.execute(
                    "SELECT rns_number, category, impact_score, insight, key_themes, enriched_at, "
                    "telegram_published_at FROM rns_announcements")}
                con.close()
                pending = [r["rns_number"] for r in db.get_unenriched_rns()]
        self.assertEqual(got["1"], ("", None, None, None, None, None))
        self.assertEqual(got["2"], ("Appointment", None, None, None, None, "2026-11-22T18:00:00"))
        self.assertEqual(got["3"], ("Results", None, None, None, None, None))
        self.assertEqual(sorted(pending), ["2", "3"])  # both re-enriched; only 3 will be posted


# ── headline recovery ─────────────────────────────────────────

def _page(main_title, doc_h1=None, body="<p>Body text that must never be read.</p>"):
    doc = f'<div class="fr-view-element displayHScroll"><div><h1><span class="js">{doc_h1}</span></h1>{body}</div></div>' if doc_h1 else ""
    return (f'<html><head><title>{main_title} | Company Announcement | Investegate</title></head><body>'
            f'<div class="art-board py-3"><h1 id="main-title">{main_title}</h1></div>{doc}</body></html>')


class TestHeadlineExtraction(unittest.TestCase):

    def _x(self, page, stored):
        from recover_headlines import extract_full_headline
        return extract_full_headline(page, stored)

    def test_main_title_full(self):
        page = _page("Coinsilium Group Limited: Appointment of Chief Operating Officer and Executive Director")
        self.assertEqual(self._x(page, "Coinsilium Group Limited: Appointment of Chie..."),
                         ("Coinsilium Group Limited: Appointment of Chief Operating Officer and Executive Director", None))

    def test_rns_falls_back_to_document_h1(self):
        page = _page("Swedbank\u2019s interim report for the third quarter...",
                     doc_h1="Swedbank's interim report for the third quarter 2026")
        self.assertEqual(self._x(page, "Swedbank\u2019s interim report for the third quarter..."),
                         ("Swedbank's interim report for the third quarter 2026", None))

    def test_entities_and_whitespace(self):
        page = _page("Fuller, Smith &amp; Turner PLC:  Trading\n  Update for the 52 weeks")
        self.assertEqual(self._x(page, "Fuller, Smith & Turner PLC:  Trading Update fo...")[0],
                         "Fuller, Smith & Turner PLC: Trading Update for the 52 weeks")

    def test_mismatch_is_rejected(self):
        self.assertEqual(self._x(_page("Something else entirely and much longer"), "Appointment of Chair and Non-Exec..."),
                         (None, "mismatch"))

    def test_still_truncated_is_rejected(self):
        page = _page("Swedbank interim report for the...", doc_h1=None)
        self.assertEqual(self._x(page, "Swedbank interim report for the..."), (None, "still_truncated"))

    def test_no_title_is_unparseable(self):
        self.assertEqual(self._x("<html><body><p>nothing</p></body></html>", "Abc..."), (None, "unparseable"))

    def test_only_headings_are_read(self):
        from recover_headlines import candidate_titles
        page = _page("Short...", doc_h1="Short title in full", body="<p>Short title in full plus secret body</p>")
        self.assertEqual(candidate_titles(page), ["Short...", "Short title in full"])


class TestFetcher(unittest.TestCase):

    def _fetcher(self, get):
        from recover_headlines import Fetcher
        self.sleeps = []
        self.t = [0.0]
        def sleep(s): self.sleeps.append(s); self.t[0] += s
        return Fetcher(get=get, interval=2.0, retry_wait=30, sleep=sleep, clock=lambda: self.t[0])

    def test_rate_limit(self):
        f = self._fetcher(lambda url: "ok")
        f.fetch("a"); f.fetch("b"); f.fetch("c")
        self.assertEqual(self.sleeps, [2.0, 2.0])

    def test_retries_once_on_5xx(self):
        from recover_headlines import FetchError
        calls = []
        def get(url):
            calls.append(url)
            if len(calls) == 1:
                raise FetchError("http_503")
            return "ok"
        self.assertEqual(self._fetcher(get).fetch("a"), "ok")
        self.assertEqual(len(calls), 2)
        self.assertIn(30, self.sleeps)

    def test_404_not_retried(self):
        from recover_headlines import FetchError
        calls = []
        def get(url):
            calls.append(url); raise FetchError("not_found")
        with self.assertRaises(FetchError):
            self._fetcher(get).fetch("a")
        self.assertEqual(len(calls), 1)


class TestRecoverHeadlines(unittest.TestCase):

    URL = "https://www.investegate.co.uk/announcement/eqs/x/y/"

    def _db(self, tmpdir, rows):
        import db
        db.init_db()
        db.save_rns_announcements([
            {"rns_number": n, "timestamp": f"2026-06-0{i + 1}T09:00:00", "company_name": "Acme", "ticker": "ACM",
             "headline": h, "url": self.URL + n, "category": c} for i, (n, h, c) in enumerate(rows)])
        db.update_rns_enrichment("1", 7, "Insight.", ["growth"])

    def _run(self, pages, dry_run=False, now=None, force=True):
        import db
        from recover_headlines import Fetcher, FetchError, recover, load_truncated_rows
        def get(url):
            page = pages[url.rsplit("/", 1)[-1]]
            if isinstance(page, Exception):
                raise page
            return page
        con = db._connect()
        rows = load_truncated_rows(con)
        counts = recover(con, rows, Fetcher(get=get, interval=0, retry_wait=0, sleep=lambda s: None),
                         dry_run=dry_run, now=now or (lambda: datetime(2026, 11, 22, 20, 0)), force=force)
        got = {r[0]: r[1:] for r in con.execute(
            "SELECT rns_number, headline, category, impact_score, insight, key_themes FROM rns_announcements")}
        con.close()
        return counts, got

    ROWS = [("1", "Acme plc: Appointment of Chief Exec...", "Appointment"),
            ("2", "Acme plc: Update on the acquisition of B...", "Acquisition"),
            ("3", "Final Results", "Results"),
            ("4", "Gone missing announcement headl...", "")]

    def _pages(self):
        from recover_headlines import FetchError
        return {"1": _page("Acme plc: Appointment of Chief Executive Officer"),
                "2": _page("Totally different page"),
                "4": FetchError("not_found", "404")}

    def test_updates_only_headline_and_reports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"), patch("recover_headlines.log"):
                self._db(tmpdir, self.ROWS)
                counts, got = self._run(self._pages())
        self.assertEqual(counts["fixed"], 1)
        self.assertEqual(counts["mismatch"], 1)
        self.assertEqual(counts["not_found"], 1)
        self.assertEqual(got["1"], ("Acme plc: Appointment of Chief Executive Officer", "Appointment", 7,
                                    "Insight.", json.dumps(["growth"])))
        self.assertEqual(got["2"][0], "Acme plc: Update on the acquisition of B...")   # left as is
        self.assertEqual(got["3"][0], "Final Results")                                # never selected
        self.assertEqual(got["4"][0], "Gone missing announcement headl...")

    def test_rerun_skips_fixed_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"), patch("recover_headlines.log"):
                self._db(tmpdir, self.ROWS)
                self._run(self._pages())
                pages = self._pages(); del pages["1"]          # a re-fetch of 1 would KeyError
                counts, _ = self._run(pages)
        self.assertEqual(counts["fixed"], 0)
        self.assertEqual(sum(counts.values()), 2)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"), patch("recover_headlines.log"):
                self._db(tmpdir, self.ROWS)
                counts, got = self._run(self._pages(), dry_run=True)
        self.assertEqual(counts["fixed"], 1)
        self.assertEqual(got["1"][0], "Acme plc: Appointment of Chief Exec...")

    def test_stops_before_collector_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"), patch("recover_headlines.log"):
                self._db(tmpdir, self.ROWS)
                counts, got = self._run(self._pages(), now=lambda: datetime(2026, 11, 23, 6, 56), force=False)
        self.assertEqual(counts["stopped_for_window"], 3)
        self.assertEqual(got["1"][0], "Acme plc: Appointment of Chief Exec...")

    def test_aborts_after_consecutive_failures(self):
        from recover_headlines import FetchError, MAX_CONSECUTIVE_FAILURES
        n = MAX_CONSECUTIVE_FAILURES + 3
        rows = [(str(i), f"Headline number {i} that was cut...", "") for i in range(1, n + 1)]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"), patch("recover_headlines.log"):
                self._db(tmpdir, rows)
                counts, _ = self._run({str(i): FetchError("http_403") for i in range(1, n + 1)})
        self.assertEqual(counts["fetch_failed"], MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(counts["aborted_remaining"], 3)

    def test_collector_window(self):
        from recover_headlines import in_collector_window
        self.assertTrue(in_collector_window(datetime(2026, 11, 23, 12, 0)))
        self.assertTrue(in_collector_window(datetime(2026, 11, 23, 6, 55)))
        self.assertFalse(in_collector_window(datetime(2026, 11, 23, 16, 40)))
        self.assertFalse(in_collector_window(datetime(2026, 11, 23, 2, 0)))


# ── routine filings ───────────────────────────────────────────

class TestRoutineFilings(unittest.TestCase):

    def _rt(self, headline, category=""):
        from routine_filings import routine_type
        return routine_type(headline, category)

    CASES = [
        ("Transaction in Own Shares", "", "Transaction in Own Shares"),
        ("Fuller, Smith & Turner PLC: Transaction in Ow...", "", "Transaction in Own Shares"),
        ("Holding(s) in Company", "", "Holding(s) in Company"),
        ("TR-1: Notification of Major Holdings", "", "Holding(s) in Company"),
        ("EQS-PVR: Nagarro SE: Release according to Art", "", "Holding(s) in Company"),
        ("Total Voting Rights", "", "Total Voting Rights"),
        ("Form 8.3 - DCC Energy plc", "", "Form 8 Disclosure"),
        ("Form 8 (DD) - Beazley plc", "", "Form 8 Disclosure"),
        ("Irish Takeover Panel - Form 8 - DCC plc", "", "Form 8 Disclosure"),
        ("Rule 2.9 Announcement", "", "Form 8 Disclosure"),
        ("Rule 38.5_A - DCC Energy plc", "", "Form 38.5"),
        ("Final Terms", "", "Final Terms"),
        ("Gearing Announcement", "", "Gearing Disclosure"),
        ("Monthly Factsheet as at 31 October 2026", "", "Portfolio Disclosure"),
        ("Top 10 Holdings", "", "Portfolio Disclosure"),
        ("Admission to Trading", "", "Block Listing / Admission"),
        ("Block Listing Six Monthly Return", "", "Block Listing / Admission"),
        ("Issue of Equity", "", "Block Listing / Admission"),
        ("Sale of Shares from Treasury", "", "Treasury Shares"),
        ("EBT Share Purchase", "", "Employee Share Schemes"),
        ("Exercise of Warrants", "", "Employee Share Schemes"),
        ("Director Declaration", "", "Director Declaration"),
        ("Post Stabilisation Notice", "", "Debt Notices"),
        ("Publication of a Supplementary Prospectus", "", "Debt Notices"),
        ("Notice of AGM", "AGM/EGM", "Meeting Notices"),
        ("Compliance with Market Abuse Regulation", "", "Administrative"),
        ("Kepler Trust Intelligence: New Research", "", "Third-party Research"),
        # Judged by the model
        ("Issue of Debt", "", None),
        ("Rule 2.8 Statement", "", None),
        ("Result of AGM", "AGM/EGM", None),
        ("Portfolio Update", "", None),
        ("Coinsilium Backs Gen Z Founders", "", None),
        ("Admission to AIM and First Day of Dealings", "New Listing", None),
        ("Issue of Equity following Placing", "", None),
        ("", "", None),
    ]

    def test_cases(self):
        for headline, category, expected in self.CASES:
            with self.subTest(headline=headline):
                self.assertEqual(self._rt(headline, category), expected)

    # Precedence: a substantive category wins over a routine match
    def test_substantive_category_wins(self):
        for headline, category in [
            ("Result of Placing and Total Voting Rights", "Placing"),
            ("Final Results and TVR", "Results"),
            ("Director/PDMR Shareholding and TVR", "Director Dealing"),
            ("Completion of Acquisition and Issue of Equity", "Acquisition"),
            ("Directorate Change and Total Voting Rights", "Appointment"),
            ("Trading Update and Transaction in Own Shares", "Trading Update"),
        ]:
            with self.subTest(headline=headline):
                self.assertIsNone(self._rt(headline, category))

    def test_buyback_execution_stays_routine(self):
        self.assertEqual(self._rt("Daily Buy-back Notification", "Share Buyback"), "Transaction in Own Shares")
        self.assertEqual(self._rt("Share Repurchase Programme: Transactions of week 21", "Share Buyback"),
                         "Transaction in Own Shares")

    def test_buyback_launch_is_not_routine(self):
        self.assertIsNone(self._rt("Launch of Share Buyback Programme", "Share Buyback"))

    def test_non_substantive_category_does_not_win(self):
        self.assertEqual(self._rt("Result of AGM and Total Voting Rights", "AGM/EGM"), "Total Voting Rights")
        self.assertEqual(self._rt("Dividend Declaration and TVR", "Dividend"), "Total Voting Rights")


class TestEnrichDiagnostics(unittest.TestCase):

    def test_variants_edit_only_the_category_line(self):
        import ai
        from enrich_diagnostics import VARIANTS
        base = ai._RNS_ENRICH_PROMPT
        other, omitted = VARIANTS["other"](base), VARIANTS["omitted"](base)
        self.assertIn("Category: Other\n", other)
        self.assertNotIn("Category:", omitted)
        self.assertEqual(other.replace("Category: Other\n", ""), omitted)
        self.assertEqual(omitted, base.replace("Category: {category}\n", ""))


# ── blocklist enrichment ──────────────────────────────────────

def _seed_rns(rows):
    """rows: (rns, headline, category, timestamp)"""
    import db
    db.init_db()
    db.save_rns_announcements([
        {"rns_number": n, "timestamp": ts, "company_name": "Acme", "ticker": "ACM",
         "headline": h, "url": "u" + n, "category": c} for n, h, c, ts in rows])


class TestBlocklistSelection(unittest.TestCase):

    ROWS = [
        ("1", "Coinsilium Backs Gen Z Founders", "", "2026-11-20T09:00:00"),
        ("2", "Transaction in Own Shares", "", "2026-11-20T10:00:00"),
        ("3", "Result of Placing and TVR", "Placing", "2026-11-20T11:00:00"),
        ("4", "Daily Buy-back Notification", "Share Buyback", "2026-11-20T12:00:00"),
        ("5", "Result of AGM", "AGM/EGM", "2026-11-20T13:00:00"),
        ("6", "Final Results", "Results", "2026-11-20T14:00:00"),
    ]

    def test_routine_excluded_everything_else_newest_first(self):
        import db
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"):
                _seed_rns(self.ROWS)
                db.update_rns_enrichment("6", 7, "x", [])
                got = db.get_unenriched_rns(limit=50)
                skipped = db.get_unenriched_rns(limit=50, skip=frozenset({"5"}))
                one = db.get_unenriched_rns(limit=1)
        self.assertEqual([r["rns_number"] for r in got], ["5", "3", "1"])
        self.assertEqual([r["rns_number"] for r in skipped], ["3", "1"])
        self.assertEqual([r["rns_number"] for r in one], ["5"])

    def test_blank_category_passed_as_constant(self):
        import db
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"):
                _seed_rns(self.ROWS[:1])
                got = db.get_unenriched_rns()
        self.assertEqual(got[0]["category"], db.PROMPT_CATEGORY_FOR_BLANK)


class TestTelegramMinScore(unittest.TestCase):

    def test_default_threshold_filters(self):
        import db
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"), \
                 patch("db.TELEGRAM_MIN_SCORE", 6):
                _seed_rns([(str(i), f"Headline {i}", "Results", "2026-11-20T09:00:00") for i in (1, 2, 3)])
                for n, sc in (("1", 5), ("2", 6), ("3", 9)):
                    db.update_rns_enrichment(n, sc, "x", [])
                default = [r["rns_number"] for r in db.get_unpublished_enriched_rns()]
                explicit = [r["rns_number"] for r in db.get_unpublished_enriched_rns(min_score=1)]
        self.assertEqual(default, ["3", "2"])
        self.assertEqual(sorted(explicit), ["1", "2", "3"])

    def test_env_var(self):
        import importlib, config
        with patch.dict(os.environ, {"TELEGRAM_MIN_SCORE": "8"}):
            self.assertEqual(importlib.reload(config).TELEGRAM_MIN_SCORE, 8)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEGRAM_MIN_SCORE", None)
            self.assertEqual(importlib.reload(config).TELEGRAM_MIN_SCORE, 6)
        importlib.reload(config)


class TestEnrichBacklog(unittest.TestCase):

    NOW = datetime(2026, 11, 23, 20, 0)

    def test_guard_selection(self):
        from enrich_backlog import rows_to_guard
        rows = [
            {"rns_number": "old", "timestamp": "2026-10-01T09:00:00", "telegram_published_at": None},
            {"rns_number": "recent", "timestamp": "2026-11-23T09:00:00", "telegram_published_at": None},
            {"rns_number": "sent", "timestamp": "2026-10-01T09:00:00", "telegram_published_at": "2026-10-01T10:00:00"},
            {"rns_number": "bad", "timestamp": "garbage", "telegram_published_at": None},
            {"rns_number": "legacy", "timestamp": "1 October 2026 09:00 AM", "telegram_published_at": None},
        ]
        self.assertEqual(rows_to_guard(rows, self.NOW), ["old", "bad", "legacy"])

    def test_guard_and_eligible_against_db(self):
        import db
        from enrich_backlog import eligible_rows, rows_to_guard, apply_guard
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"):
                _seed_rns([("1", "Coinsilium Backs Gen Z Founders", "", "2026-10-01T09:00:00"),
                           ("2", "Holding(s) in Company", "", "2026-10-01T09:00:00"),
                           ("3", "Predictive Labs Unveils Platform", "", "2026-11-23T15:00:00")])
                con = db._connect()
                rows = eligible_rows(con)
                apply_guard(con, rows_to_guard(rows, self.NOW), "2026-11-23T20:00:00")
                got = dict(con.execute("SELECT rns_number, telegram_published_at FROM rns_announcements"))
                con.close()
        self.assertEqual(sorted(r["rns_number"] for r in rows), ["1", "3"])
        self.assertEqual(got, {"1": "2026-11-23T20:00:00", "2": None, "3": None})

    def _queue(self, n):
        self.store = {str(i): None for i in range(n, 0, -1)}       # newest first
        def get(limit, skip):
            return [{"rns_number": k} for k, v in self.store.items() if v is None and k not in skip][:limit]
        def save(rns, score, insight, themes):
            self.store[rns] = score
        return get, save

    def _run(self, n, enrich_fn, **kw):
        from enrich_backlog import enrich
        get, save = self._queue(n)
        with patch("enrich_backlog.log"):
            return enrich(get=get, enrich_fn=enrich_fn, save=save, now=lambda: self.NOW, **kw)

    @staticmethod
    def _ok(batch):
        return [(a["rns_number"], {"impact_score": 6, "insight": "x", "key_themes": []}) for a in batch]

    def test_enriches_everything_and_resumes(self):
        counts = self._run(25, self._ok)
        self.assertEqual(counts["enriched"], 25)
        self.assertTrue(all(v == 6 for v in self.store.values()))

    def test_limit(self):
        counts = self._run(25, self._ok, limit=7)
        self.assertEqual(counts["enriched"], 7)
        self.assertEqual(sum(v is not None for v in self.store.values()), 7)

    def test_failures_not_retried_within_run(self):
        seen = Counter()
        def flaky(batch):
            for a in batch: seen[a["rns_number"]] += 1
            return [r for r in self._ok(batch) if r[0] != "3"]
        counts = self._run(12, flaky)
        self.assertEqual((counts["enriched"], counts["failed"]), (11, 1))
        self.assertEqual(seen["3"], 1)

    def test_aborts_when_model_down(self):
        from enrich_backlog import MAX_CONSECUTIVE_FAILURES
        counts = self._run(100, lambda batch: [])
        self.assertEqual(counts["aborted"], 1)
        self.assertLess(counts["failed"], MAX_CONSECUTIVE_FAILURES + 10)

    def test_stops_for_collector_window(self):
        from enrich_backlog import enrich
        get, save = self._queue(5)
        with patch("enrich_backlog.log"):
            counts = enrich(get=get, enrich_fn=self._ok, save=save, now=lambda: datetime(2026, 11, 24, 6, 56))
        self.assertEqual(counts["stopped_for_window"], 1)
        self.assertEqual(counts["enriched"], 0)


# ── github connection test ────────────────────────────────────

class TestPromptV2(unittest.TestCase):
    """Headline-only prompt (v2), no score floors, versioned re-enrichment."""

    def test_prompt_forbids_outside_facts(self):
        import ai
        p = ai._RNS_ENRICH_PROMPT
        self.assertGreaterEqual(ai.RNS_PROMPT_VERSION, 2)
        for phrase in ("Reason only from the company name, category and headline",
                       "Never name a company, person, product, project, place, amount, percentage or date",
                       "generally signifies for shareholders, without inventing specifics"):
            self.assertIn(phrase, p)
        for gone in ("precedent", "sector trends", "Never generic"):
            self.assertNotIn(gone, p.replace("peers, precedents", ""))
        filled = p.format(company="Acme", ticker="ACM", category="Other", headline="Offer")
        self.assertIn("Headline: Offer", filled)

    def test_no_score_floor(self):
        import ai
        for cat in ("Acquisition", "Results", "Trading Update", "Placing", "Director Dealing", ""):
            got = ai._parse_enrichment_json('{"impact_score": 2, "insight": "x", "key_themes": []}', cat)
            self.assertEqual(got["impact_score"], 2, cat)
        self.assertFalse(hasattr(ai, "_SCORE_FLOORS"))

    ROWS = [
        ("1", "Unscored news", "", "2026-11-20T09:00:00"),
        ("2", "Old prompt news", "", "2026-11-20T10:00:00"),
        ("3", "Transaction in Own Shares", "", "2026-11-20T11:00:00"),   # routine, old enrichment
        ("4", "Holding(s) in Company", "", "2026-11-20T12:00:00"),       # routine, never enriched
        ("5", "Current prompt news", "", "2026-11-20T13:00:00"),
    ]

    def _seed(self):
        import db, sqlite3
        _seed_rns(self.ROWS)
        db.update_rns_enrichment("2", 6, "old", [], version=None)
        db.update_rns_enrichment("3", 5, "old", [], version=1)
        db.update_rns_enrichment("5", 4, "new", [])

    def test_save_stamps_current_version(self):
        import db, ai, sqlite3
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "t.db")
            with patch("db.DB_PATH", path), patch("db.log"):
                self._seed()
            con = sqlite3.connect(path)
            got = dict(con.execute("SELECT rns_number, enrichment_version FROM rns_announcements"))
        self.assertEqual(got, {"1": None, "2": None, "3": 1, "4": None, "5": ai.RNS_PROMPT_VERSION})

    def test_live_selection_unchanged_backlog_includes_stale(self):
        import db
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"):
                self._seed()
                live = db.get_unenriched_rns(limit=50)
                backlog = db.get_unenriched_rns(limit=50, include_stale=True)
                skipped = db.get_unenriched_rns(limit=50, include_stale=True, skip=frozenset({"3"}))
        self.assertEqual([r["rns_number"] for r in live], ["1"])
        self.assertEqual([r["rns_number"] for r in backlog], ["3", "2", "1"])    # newest first
        self.assertEqual([r["rns_number"] for r in skipped], ["2", "1"])

    def test_backlog_counts_stale_and_guards_them(self):
        import db, enrich_backlog as eb
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"):
                self._seed()
                con = db._connect()
                rows = eb.eligible_rows(con)
                guard = eb.rows_to_guard(rows, datetime(2026, 11, 24, 17, 0))
                con.close()
        self.assertEqual({r["rns_number"]: bool(r["stale"]) for r in rows},
                         {"1": False, "2": True, "3": True})
        self.assertEqual(sorted(guard), ["1", "2", "3"])

    def test_reenrichment_replaces_and_stamps(self):
        import db, ai, enrich_backlog as eb
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"), patch("enrich_backlog.log"):
                self._seed()
                fake = lambda batch: [(a["rns_number"], {"impact_score": 3, "insight": "v2", "key_themes": []})
                                      for a in batch]
                counts = eb.enrich(force=True, enrich_fn=fake)
                con = db._connect()
                left = eb.eligible_rows(con)
                rows = con.execute("SELECT rns_number, insight, enrichment_version FROM rns_announcements "
                                   "ORDER BY rns_number").fetchall()
                con.close()
        self.assertEqual(counts["enriched"], 3)
        self.assertEqual(left, [])
        self.assertEqual(rows, [("1", "v2", ai.RNS_PROMPT_VERSION), ("2", "v2", ai.RNS_PROMPT_VERSION),
                                ("3", "v2", ai.RNS_PROMPT_VERSION), ("4", None, None),
                                ("5", "new", ai.RNS_PROMPT_VERSION)])


class TestRnsDigest(unittest.TestCase):
    def _con(self, rows):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE rns_announcements (rns_number, timestamp, company_name, ticker, "
                    "headline, impact_score, insight)")
        con.executemany("INSERT INTO rns_announcements VALUES (?,?,?,?,?,?,?)", rows)
        con.commit()
        return con

    def test_selects_by_announcement_day_and_score(self):
        import rns_digest as d
        from datetime import date
        con = self._con([
            ("1", "2026-11-23T08:00:00", "A plc", "AAA", "Offer", 9, "x"),
            ("2", "2026-11-22T08:00:00", "B plc", "BBB", "Old news enriched today", 10, "x"),
            ("3", "2026-11-23T09:00:00", "C plc", "CCC", "Small", 6, "x"),
            ("4", "2026-11-23T10:00:00", "D plc", "DDD", "Later", 9, "x"),
            ("5", "2026-11-23T11:00:00", "E plc", "EEE", "Unscored", None, None),
        ])
        rows = d.rows_for_day(con, date(2026, 11, 23), 7)
        self.assertEqual([r["rns_number"] for r in rows], ["4", "1"])   # score, then latest

    def test_one_per_company_and_top(self):
        import rns_digest as d
        rows = [{"rns_number": n, "ticker": t, "company_name": t} for n, t in
                [("1", "AAA"), ("2", "AAA"), ("3", "BBB"), ("4", "CCC")]]
        self.assertEqual([r["rns_number"] for r in d.pick(rows, 2)], ["1", "3"])

    def test_display_headline_keeps_other_prefixes(self):
        import rns_digest as d
        self.assertEqual(d.display_headline("Rule 2.7: Firm Offer", "Acme plc"), "Rule 2.7: Firm Offer")
        self.assertEqual(d.display_headline("Acme: Results", "Acme Group plc"), "Results")
        self.assertEqual(d.display_headline("No colon here", "Acme plc"), "No colon here")

    def test_company_count_label(self):
        import rns_digest as d
        self.assertEqual(d.company_count_label(1722), "1,700+")
        self.assertEqual(d.company_count_label(2001), "2,000+")
        self.assertEqual(d.company_count_label(42), "42")

    def test_x_length(self):
        import rns_digest as d
        self.assertEqual(d.x_length("abc"), 3)
        self.assertEqual(d.x_length("📋"), 2)
        self.assertEqual(d.x_length("→"), 2)
        self.assertEqual(d.x_length("—£é"), 3)
        self.assertEqual(d.x_length("see angloedge.com/companies now"), 4 + 23 + 4)

    def _item(self, insight, headline="Recommended All Share Offer", company="Reabold Resources"):
        return {"ticker": "RBD", "company": company, "slug": "reabold-rbd", "score": 9,
                "headline": headline, "insight": insight, "timestamp": "2026-11-23T08:00:00"}

    def test_posts_short_insight_kept_whole(self):
        import rns_digest as d
        from datetime import date
        posts = d.build_posts([self._item("A recommended offer is likely to complete.")], 1722, date(2026, 11, 23))
        self.assertEqual(posts, [
            "Highest-impact UK RNS today 📋\nMon 23 November 2026",
            "$RBD Reabold Resources — 9/10\nRecommended All Share Offer\nA recommended offer is likely to complete.",
            "Scored filing history for 1,700+ UK companies → angloedge.com/companies"])

    def test_posts_trimmed_to_limit(self):
        import rns_digest as d
        from datetime import date
        long_sentences = "First sentence is short. " + "Second sentence goes on and on " * 12 + "."
        one_long = "word " * 120
        for insight in (long_sentences, one_long):
            post = d.build_posts([self._item(insight)], 1722, date(2026, 11, 23))[1]
            self.assertLessEqual(d.x_length(post), d.POST_LIMIT)
            self.assertIn("Recommended All Share Offer", post)
        self.assertTrue(d.build_posts([self._item(long_sentences)], 1, date(2026, 11, 23))[1]
                        .endswith("First sentence is short."))
        self.assertTrue(d.build_posts([self._item(one_long)], 1, date(2026, 11, 23))[1].endswith("…"))
        huge = d.build_posts([self._item("x", headline="H " * 300, company="C " * 200)], 1, date(2026, 11, 23))[1]
        self.assertLessEqual(d.x_length(huge), d.POST_LIMIT)

    def test_delimiter_never_inside_a_post(self):
        import rns_digest as d
        from datetime import date
        posts = d.build_posts([self._item("Line one\n---\nline two")], 1, date(2026, 11, 23))
        self.assertFalse(any(line.strip() == "---" for p in posts for line in p.split("\n")))

    def test_empty_day_has_no_posts(self):
        import rns_digest as d
        from datetime import date
        dg = d.build_digest([], {}, 1722, date(2026, 11, 22))
        self.assertEqual((dg["date"], dg["items"], dg["posts"]), ("2026-11-22", [], []))

    def test_telegram_format_escapes(self):
        import rns_digest as d
        from datetime import date
        dg = {"items": [self._item("Big <deal> & it's final")], "posts": ["t", "footer → x"]}
        m = d.format_telegram(dg)
        self.assertIn("Big &lt;deal&gt; &amp; it's final", m)
        self.assertTrue(m.endswith("footer → x"))

    # ── end to end with a temp site repo and fake git ──

    def _run(self, argv, tmp, deliver=None):
        import rns_digest as d
        import sqlite3
        repo = os.path.join(tmp, "site")
        os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
        os.makedirs(os.path.join(repo, "src", "data", "companies"), exist_ok=True)
        with open(os.path.join(repo, "src", "data", "companies-index.json"), "w") as f:
            json.dump([{"ticker": "AAA", "slug": "a-aaa"}, {"ticker": "BBB", "slug": "b-bbb"}], f)
        path = os.path.join(tmp, "t.db")
        if not os.path.exists(path):
            src = self._con([("1", datetime.now().strftime("%Y-%m-%dT08:00:00"), "A plc", "AAA", "Offer", 9, "x")])
            src.execute("VACUUM INTO ?", (path,))
        calls = []
        env = {"GITHUB_REPO_PATH": repo, "GITHUB_COMPANIES_DIR": "src/data/companies"}
        with patch.dict(os.environ, env), patch.object(d, "SENT_DIR", os.path.join(tmp, "sent")), \
             patch.object(d.db, "init_db"), patch.object(d.db, "_connect", side_effect=lambda: sqlite3.connect(path)), \
             patch("rns_digest.log"), patch("export_companies.log"):
            rc = d.main(argv, deliver=deliver, git=lambda repo, *a: calls.append(a) or "")
        out = os.path.join(repo, "src", "data", "rns-today.json")
        data = None
        if os.path.exists(out):
            with open(out) as f:
                data = json.load(f)
        return rc, calls, data

    def test_writes_site_file_and_commits_only_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls, data = self._run([], tmp)
            self.assertEqual(rc, 0)
            self.assertEqual(data["items"][0]["slug"], "a-aaa")
            self.assertEqual(len(data["posts"]), 3)
            self.assertIn(("add", "-A", "--", "src/data/rns-today.json"), calls)
            self.assertTrue(any(c[0] == "push" for c in calls))
            rc, calls2, _ = self._run([], tmp)                     # same content again
            self.assertEqual(calls2, [])

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls, data = self._run(["--dry-run"], tmp)
            self.assertEqual((rc, calls, data), (0, [], None))

    def test_telegram_is_opt_in_and_once_per_day(self):
        sent = []
        deliver = lambda m: sent.append(m) or True
        with tempfile.TemporaryDirectory() as tmp:
            self._run([], tmp, deliver)
            self.assertEqual(sent, [])                                    # not the default
            self.assertEqual(self._run(["--telegram"], tmp, deliver)[0], 0)
            self.assertEqual(len(sent), 1)
            self.assertEqual(self._run(["--telegram"], tmp, deliver)[0], 1)
            self.assertEqual(len(sent), 1)

    def test_failed_telegram_send_leaves_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(["--telegram"], tmp, lambda m: False)[0], 1)
            self.assertFalse(os.path.exists(os.path.join(tmp, "sent")))


class TestInsightAudit(unittest.TestCase):
    def test_baseline_file(self):
        import enrich_diagnostics as d
        b = d.load_baseline()
        self.assertEqual(b["counts"], {"unsupported": 11, "outside": 8, "clean": 31})
        self.assertEqual(len(b["rows"]), 50)
        self.assertEqual(d.breakdown([r["label"] for r in b["rows"]]),
                         "unsupported 11 (22%) · outside 8 (16%) · clean 31 (62%)")

    def test_breakdown_ignores_unparsed(self):
        import enrich_diagnostics as d
        self.assertEqual(d.breakdown(["clean", None, "outside", "clean"]),
                         "unsupported 0 (0%) · outside 1 (33%) · clean 2 (67%)")

    def test_judge_parses_and_rejects(self):
        import enrich_diagnostics as d
        row = {"company_name": "A", "category": None, "headline": "H", "insight": "I"}
        with patch.object(d.ai, "_ollama_call", return_value='x {"label": "Outside", "reason": "macro"} y'):
            self.assertEqual(d.judge(row), ("outside", "macro"))
        with patch.object(d.ai, "_ollama_call", return_value='{"label": "maybe"}'):
            self.assertIsNone(d.judge(row)[0])
        with patch.object(d.ai, "_ollama_call", return_value=None):
            self.assertIsNone(d.judge(row)[0])

    def test_audit_samples_current_version_only_and_writes_sheet(self):
        import db, ai, sqlite3, enrich_diagnostics as d
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("db.DB_PATH", os.path.join(tmpdir, "t.db")), patch("db.log"):
                _seed_rns([(str(i), f"News {i}", "", f"2026-11-20T{9 + i % 8:02d}:00:00") for i in range(10)])
                for i in range(10):
                    db.update_rns_enrichment(str(i), 5, f"insight {i}", [], version=None if i < 4 else ai.RNS_PROMPT_VERSION)
                con = db._connect()
                with patch("enrich_diagnostics.log"):
                    rows = d.audit(con, 50, 1, judge_fn=lambda r: ("clean", ""), out_dir=os.path.join(tmpdir, "a"),
                                   today=datetime(2026, 11, 25))
                con.close()
            self.assertEqual(sorted(r["rns_number"] for r in rows), [str(i) for i in range(4, 10)])
            sheet = os.path.join(tmpdir, "a", f"insights_v{ai.RNS_PROMPT_VERSION}_2026-11-25.json")
            with open(sheet) as f:
                data = json.load(f)
        self.assertEqual(data["judge_counts"], {"clean": 6})
        self.assertTrue(all(r["human_label"] == "" for r in data["rows"]))

    def test_calibrate_reports_agreement(self):
        import enrich_diagnostics as d
        with patch("enrich_diagnostics.log"):
            pairs = d.audit_calibrate(judge_fn=lambda r: ("clean", ""))
        self.assertEqual(sum(a == b for a, b in pairs), 31)


class TestOutsideTerms(unittest.TestCase):
    def test_flags_invented_names_only(self):
        from enrich_diagnostics import outside_terms as o
        self.assertEqual(o("Finalises the acquisition of iSoftStone.", "Elecosoft Court Sanction"), ["iSoftStone"])
        self.assertEqual(o("Mirroring collapses like Wirecard's.", "Distil Appoint Admin'rs"), ["Wirecard"])
        self.assertEqual(o("Advances the Senala project in Sierra Leone.", "Oriole Completion of Senala JV"),
                         ["Sierra Leone"])
        self.assertEqual(o("Securing $7m of orders.", "Concurrent $7m Production Orders"), [])
        self.assertEqual(o("A formal sale process means the board seeks offers.", "Futura Formal Sale Process"), [])


class TestGitHubConnection(unittest.TestCase):
    """Integration test — only runs if GITHUB_REPO_PATH is set in env."""

    def setUp(self):
        self.repo_path = os.environ.get("GITHUB_REPO_PATH", "")
        if not self.repo_path or not os.path.isdir(self.repo_path):
            self.skipTest("GITHUB_REPO_PATH not set or path does not exist — skipping live GitHub test")

    def test_repo_path_is_git_repo(self):
        self.assertTrue(
            os.path.isdir(os.path.join(self.repo_path, ".git")),
            f"{self.repo_path} is not a git repository"
        )

    def test_can_run_git_status(self):
        import subprocess
        result = subprocess.run(
            ["git", "-C", self.repo_path, "status"],
            capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
