"""
Test suite for the briefing pipeline.
Run: python3 tests.py

No external services required — all tests use mocks or temp dirs.
"""
import os
import sys
import json
import tempfile
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
        self.assertEqual(make_slug("BT Group PLC (The)", "BT.A"), "bt-group-plc-the-bt-a")

    def test_slug_strips_accents(self):
        from export_companies import slugify
        self.assertEqual(slugify("Société Générale"), "societe-generale")

    def test_slug_falls_back_to_ticker_when_name_empty(self):
        from export_companies import make_slug
        self.assertEqual(make_slug("", "0HAF"), "0haf")

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
        self.assertEqual(set(a), {"timestamp", "category", "headline", "url", "enriched",
                                  "impact_score", "insight", "key_themes"})


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
        self.assertEqual(os.listdir(self.dir), ["acme-plc-acm.json"])
        self.assertEqual(s["exported"], 1)
        self.assertEqual(s["below_min"], 1)
        self.assertEqual(s["no_ticker_rows"], 1)

    def test_minimum_is_configurable(self):
        s = self._run(self._rows(), min_announcements=1)
        self.assertEqual(s["exported"], 2)

    def test_company_file_contents(self):
        self._run(self._rows())
        c = self._load(os.path.join(self.dir, "acme-plc-acm.json"))
        self.assertEqual(c["name"], "Acme Plc")
        self.assertEqual(c["ticker"], "ACM")
        self.assertEqual(c["slug"], "acme-plc-acm")
        self.assertEqual(c["announcement_count"], 3)
        self.assertEqual(c["latest_announcement"], "2026-05-03T09:00:00")
        self.assertEqual([a["timestamp"][:10] for a in c["announcements"]],
                         ["2026-05-03", "2026-05-02", "2026-05-01"])
        self.assertEqual(c["announcements"][0]["key_themes"], ["growth"])
        self.assertIsNone(c["announcements"][1]["impact_score"])

    def test_invalid_timestamp_sorted_last_and_excluded_from_latest(self):
        rows = self._rows() + [_rns_row(9, "ACM", "garbage 99 XM")]
        s = self._run(rows)
        c = self._load(os.path.join(self.dir, "acme-plc-acm.json"))
        self.assertEqual(c["latest_announcement"], "2026-05-03T09:00:00")
        self.assertEqual(c["announcements"][-1]["timestamp"], "garbage 99 XM")
        self.assertEqual(s["invalid_timestamps"], 1)

    def test_mixed_timestamp_formats_sort_and_normalise(self):
        rows = [
            _rns_row(1, "MIX", "2026-05-22T22:00:00", name="Mixed Co"),
            _rns_row(2, "MIX", "21 Sep 2026 07:32 AM", name="Mixed Co"),
            _rns_row(3, "MIX", "03 Jun 2026 04:15 PM", name="Mixed Co"),
            _rns_row(4, "MIX", "not a date", name="Mixed Co"),
        ]
        s = self._run(rows)
        c = self._load(os.path.join(self.dir, "mixed-co-mix.json"))
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

    def test_writes_index(self):
        self._run(self._rows())
        index = self._load(self.idx)
        self.assertEqual(index, [{"name": "Acme Plc", "ticker": "ACM", "slug": "acme-plc-acm",
                                  "announcement_count": 3,
                                  "latest_announcement": "2026-05-03T09:00:00"}])

    def test_commits_and_pushes_only_export_paths(self):
        self._run(self._rows())
        subcommands = [c.args[1] for c in self.git.call_args_list]
        self.assertEqual(subcommands, ["add", "commit", "pull", "push"])
        commit_args = self.git.call_args_list[1].args
        self.assertIn(self.COMPANIES, commit_args)
        self.assertIn("--", commit_args)

    def test_second_identical_run_changes_nothing(self):
        self._run(self._rows())
        self.git.reset_mock()
        s = self._run(self._rows())
        self.assertEqual(s["new"] + s["changed"], 0)
        self.assertEqual(s["unchanged"], 1)
        self.git.assert_not_called()

    def test_slug_is_sticky_across_rename(self):
        self._run(self._rows())
        s = self._run(self._rows(name="Acme Holdings Group"))
        self.assertEqual(os.listdir(self.dir), ["acme-plc-acm.json"])
        c = self._load(os.path.join(self.dir, "acme-plc-acm.json"))
        self.assertEqual(c["name"], "Acme Holdings Group")
        self.assertEqual(s["pruned"], 0)

    def test_slug_recovered_from_files_if_map_lost(self):
        self._run(self._rows())
        os.remove(self.map)
        self._run(self._rows(name="Acme Holdings Group"))
        self.assertEqual(os.listdir(self.dir), ["acme-plc-acm.json"])
        self.assertEqual(self._load(self.map), {"ACM": "acme-plc-acm"})

    def test_corrupt_slug_map_aborts(self):
        os.makedirs(os.path.dirname(self.map))
        with open(self.map, "w") as f:
            f.write("{not json")
        with self.assertRaises(RuntimeError):
            self._run(self._rows())

    def test_new_slug_collision_gets_suffix(self):
        os.makedirs(os.path.dirname(self.map))
        with open(self.map, "w") as f:
            json.dump({"OLD": "acme-plc-acm"}, f)
        self._run(self._rows())
        self.assertIn("acme-plc-acm-2.json", os.listdir(self.dir))

    def test_prunes_stale_files(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "gone-co-gne.json"), "w") as f:
            json.dump({"ticker": "GNE", "slug": "gone-co-gne"}, f)
        s = self._run(self._rows())
        self.assertEqual(s["pruned"], 1)
        self.assertEqual(os.listdir(self.dir), ["acme-plc-acm.json"])

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


# ── github connection test ────────────────────────────────────

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
