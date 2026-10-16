import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.entities import canonical_institution, normalize_metadata, normalize_ticker
from src.news_store import NewsStore
from tests.test_news_store import article


class NewsSearchTests(unittest.TestCase):
    def setUp(self):
        self.store = NewsStore(":memory:")
        self.addCleanup(self.store.close)

    def test_full_history_and_stable_pages_beyond_five_hundred(self):
        records = [article(url=f"https://example.com/{index}", title=f"Research {index}",
                           content=f"Distinct report {index} on capital spending.") for index in range(610)]
        records[0]["content"] = "Historical needle only in the oldest report body."
        records[0]["published_at"] = "2020-01-01T00:00:00Z"
        self.store.ingest(records)
        result = self.store.search_articles(query="historical needle")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["url"], "https://example.com/0")
        seen = []
        for offset in range(0, 610, 53):
            page = self.store.search_articles(limit=53, offset=offset)
            self.assertEqual(page["total"], 610)
            seen.extend(record["id"] for record in page["items"])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 610)
        self.assertEqual(seen, [record["id"] for record in self.store.search_articles(limit=1000)["items"]])
        self.assertEqual(self.store.search_articles(limit=0), {"total": 610, "items": []})

    def test_chinese_substrings_aliases_and_literal_query_syntax(self):
        self.store.ingest([article(title="人工智能資本開支前景", content="香港市場科技行業盈利改善。",
                                   institution="摩根士丹利", author="Jane Analyst")])
        for query in ("資本開支", "科技行業", "摩根士丹利", "Morgan Stanley", "Jane Analyst"):
            self.assertEqual(self.store.search_articles(query=query)["total"], 1, query)
        for query in ('" OR *', "NEAR(capital)", "x' OR 1=1 --", "[", "%", "\\"):
            self.assertEqual(self.store.search_articles(query=query)["total"], 0, query)
        self.store._fts_enabled = False
        self.assertEqual(self.store.search_articles(query="Morgan Stanley")["total"], 1)
        self.assertEqual(self.store.search_articles(query="科技行業")["total"], 1)

    def test_attribution_metadata_filters_search_and_facets(self):
        first = article(institution="Morgan Stanley", tickers=["HKEX:700", "600519.SS"],
                        asset_classes=["Equities"], topics=["AI"], author="A. Analyst",
                        publisher="Original Research", provider="Public site")
        copy = article(source_id="research", source_name="Research Feed", url="https://other.example/1",
                       institution="UBS", tickers=["AAPL"], topics=["Policy"], author="Second Author")
        self.store.ingest([first, copy])
        self.assertEqual(self.store.stats()["total"], 1)
        for options in ({"institution": "摩根士丹利", "ticker": "700.HK", "asset_class": "equities", "topic": "AI"},
                        {"institution": "瑞銀", "source_ids": ["research"], "ticker": "AAPL"},
                        {"query": "Second Author"}, {"query": "Original Research"}):
            self.assertEqual(self.store.search_articles(**options)["total"], 1, options)
        self.assertEqual(self.store.search_articles(ticker="700")["total"], 0)
        self.assertEqual(self.store.search_articles(source_ids=["absent"])["total"], 0)
        facets = self.store.facets()
        self.assertEqual(facets["institutions"], ["Morgan Stanley", "UBS"])
        self.assertEqual(facets["tickers"], ["0700.HK", "600519.SH", "AAPL"])
        self.assertEqual({value["id"] for value in facets["sources"]}, {"fed", "research"})
        self.assertEqual((facets["earliest"], facets["latest"]), ("2026-10-13", "2026-10-13"))

    def test_dates_use_utc_include_end_day_and_optionally_unknown_dates(self):
        self.store.ingest([
            article(url="https://x.test/before", title="Before", published_at="2026-10-12T23:59:59Z"),
            article(url="https://x.test/start", title="Start", published_at="2026-10-13T08:00:00+08:00"),
            article(url="https://x.test/end", title="End", published_at="2026-10-13T23:59:59.999Z"),
            article(url="https://x.test/after", title="After", published_at="2026-10-14T00:00:00Z"),
            article(url="https://x.test/unknown", title="Unknown", published_at=None),
        ])
        filters = {"start_date": "2026-10-13", "end_date": "2026-10-13"}
        self.assertEqual(self.store.search_articles(**filters)["total"], 2)
        self.assertEqual(self.store.search_articles(**filters, include_unknown=True)["total"], 3)
        self.assertEqual(self.store.search_articles()["total"], 5)
        with self.assertRaises(ValueError):
            self.store.search_articles(start_date="yesterday")
        for kwargs in ({"limit": -1}, {"offset": -1}, {"limit": True}, {"offset": "0"}):
            with self.assertRaises(ValueError):
                self.store.search_articles(**kwargs)

    def test_invalid_filters_do_not_silently_expand_search(self):
        self.store.ingest([article(), article(url="https://example.com/unknown", title="Undated", published_at=None)])
        for options in ({"ticker": "not a ticker"}, {"ticker": ["NVDA"]}, {"include_unknown": "false"},
                        {"start_date": "2026-10-14", "end_date": "2026-10-13", "include_unknown": True}):
            with self.assertRaises(ValueError):
                self.store.search_articles(**options)
        with self.assertRaises(ValueError):
            self.store.save_search("invalid ticker", {"ticker": "not a ticker"})
        with self.assertRaises(ValueError):
            self.store.save_search("invalid dates", {"start_date": "2026-10-14", "end_date": "2026-10-13"})
        self.assertEqual(self.store.list_saved_searches(), [])
        self.assertEqual(self.store.search_articles(start_date="2026-10-13T07:59:59Z", end_date="2026-10-13")["total"], 1)
        self.assertEqual(self.store.search_articles(end_date="9999-12-31")["total"], 1)

    def test_returning_to_fts_rebuilds_changed_rows_from_fallback_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "switch-build.sqlite3"
            with NewsStore(path) as store:
                if not store._fts_enabled:
                    self.skipTest("This build has no FTS5")
                store.ingest([article(content="uniquepriorword")])
                # Simulate ingestion by a SQLite build without the extension.
                store._fts_enabled = False
                store.ingest([article(content="uniquefreshword")])
                store.ingest([article(url="https://example.com/new", title="New", content="brandnewword")])
                self.assertEqual(store.search_articles(query="uniquefreshword")["total"], 1)
            with NewsStore(path) as reopened:
                self.assertEqual(reopened.search_articles(query="uniquefreshword")["total"], 1)
                self.assertEqual(reopened.search_articles(query="uniquepriorword")["total"], 0)
                self.assertEqual(reopened.search_articles(query="brandnewword")["total"], 1)
                self.assertEqual(reopened.connection.execute("SELECT COUNT(*) FROM article_fts").fetchone()[0], 2)
            with NewsStore(path) as reopened:
                self.assertEqual(reopened.connection.execute("SELECT COUNT(*) FROM article_fts").fetchone()[0], 2)

    def test_hk_five_digit_feed_codes_match_four_digit_market_symbols(self):
        self.store.ingest([article(tickers=["00700.HK"])])
        self.assertEqual(self.store.search_articles(ticker="0700.HK")["total"], 1)
        self.assertEqual(self.store.search_articles(ticker="HKEX:00700")["total"], 1)
        self.assertEqual(self.store.facets()["tickers"], ["0700.HK"])

    def test_version_one_metadata_migration_reindexes_without_changing_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "search-v1.sqlite3"
            with NewsStore(path) as store:
                store.ingest([article(tickers=["0700.HK"])])
                previous = store.export_articles()[0]
                store.mark_analyzed(previous["id"], {"kept": True})
                # Recreate the prior normalization representation and version.
                for table in ("articles", "article_sources"):
                    for row in store.connection.execute(f"SELECT rowid,metadata_json FROM {table}").fetchall():
                        metadata = json.loads(row["metadata_json"])
                        metadata["tickers"] = ["00700.HK"]
                        store.connection.execute(f"UPDATE {table} SET metadata_json=? WHERE rowid=?",
                                                 (json.dumps(metadata), row["rowid"]))
                store._index_article(previous["id"])
                store.connection.execute("UPDATE news_schema SET version=1 WHERE component='search'")
                store.connection.commit()
            with NewsStore(path) as reopened:
                current = reopened.get_article(previous["id"])
                self.assertEqual(current["analysis"], {"kept": True})
                self.assertEqual(current["content_hash"], previous["content_hash"])
                self.assertEqual(current["updated_at"], previous["updated_at"])
                self.assertEqual(reopened.search_articles(ticker="HKEX:700")["total"], 1)
                self.assertEqual(reopened.facets()["tickers"], ["0700.HK"])

    def test_metadata_enrichment_updates_search_without_invalidating_analysis(self):
        self.store.ingest([article()])
        saved = self.store.pending()[0]
        self.store.mark_analyzed(saved["id"], {"summary": "Existing analysis"})
        self.assertEqual(self.store.ingest([article(institution="高盛", topics=["Rates"])])["duplicates"], 1)
        current = self.store.get_article(saved["id"])
        self.assertEqual(current["institution"], "Goldman Sachs")
        self.assertEqual(current["analysis"], {"summary": "Existing analysis"})
        self.assertEqual(self.store.search_articles(institution="高盛", topic="Rates")["total"], 1)
        self.store.ingest([article()])
        self.assertEqual(self.store.search_articles(topic="Rates")["total"], 1)
        self.assertIsNone(self.store.get_article("not-present"))

    def test_saved_search_allowlist_update_delete_and_no_credentials(self):
        identifier = self.store.save_search("AI watch", {"query": "AI", "institution": "摩根士丹利",
                                                      "ticker": "HKEX:700", "include_unknown": True,
                                                      "token": "never-persist-this", "authorization": "secret",
                                                      "client": {"sessionId": "another-secret"}})
        search = self.store.list_saved_searches()[0]
        self.assertEqual(search["filters"], {"query": "AI", "institution": "Morgan Stanley", "ticker": "0700.HK",
                                            "include_unknown": True})
        self.assertNotIn("secret", json.dumps(search))
        self.assertEqual(self.store.save_search("AI watch", {"topic": "Policy"}), identifier)
        self.assertEqual(len(self.store.list_saved_searches()), 1)
        for query in ("Bearer secret-value", "api_key=secret-value", "ift__synthetic_sensitive_value",
                      "token=synthetic-sensitive-value", "mcp_token:synthetic-sensitive-value",
                      "refresh_token=synthetic-sensitive-value", "ｉｆｔ__synthetic_sensitive_value"):
            with self.assertRaises(ValueError):
                self.store.save_search("secret", {"query": query})
        self.assertTrue(self.store.delete_search(identifier))
        self.assertFalse(self.store.delete_search(identifier))
        self.assertEqual(self.store.list_saved_searches(), [])

    def test_legacy_schema_migration_preserves_analysis_id_and_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT NOT NULL, metadata_json TEXT NOT NULL,
                  fingerprint TEXT, content_hash TEXT NOT NULL, published_sort TEXT, first_seen TEXT NOT NULL,
                  last_seen TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                  analysis_json TEXT, analyzed_at TEXT, error TEXT);
                CREATE TABLE article_sources(source_id TEXT NOT NULL, canonical_url TEXT NOT NULL,
                  article_id TEXT NOT NULL REFERENCES articles(id), metadata_json TEXT NOT NULL,
                  raw_metadata_json TEXT NOT NULL, content_hash TEXT NOT NULL, first_seen TEXT NOT NULL,
                  last_seen TEXT NOT NULL, PRIMARY KEY(source_id, canonical_url));
            """)
            metadata = article(source_id="investat", source_name="Investat / 摩根士丹利", external_id="old", url="")
            serialized = json.dumps(metadata)
            connection.execute("INSERT INTO articles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               ("retained-id", "investat:old", serialized, "fingerprint", "hash", "2026-10-13T08:00:00+00:00",
                                "first", "last", "updated", "analyzed", '{"verified":true}', "analyzed", None))
            connection.execute("INSERT INTO article_sources VALUES(?,?,?,?,?,?,?,?)",
                               ("investat", "investat:old", "retained-id", serialized, serialized, "hash", "first", "last"))
            connection.commit()
            connection.close()
            for _ in range(2):
                with NewsStore(path) as store:
                    saved = store.get_article("retained-id")
                    self.assertEqual(saved["analysis"], {"verified": True})
                    self.assertEqual(saved["content_hash"], "hash")
                    self.assertEqual(saved["updated_at"], "updated")
                    self.assertEqual(saved["institution"], "Morgan Stanley")
                    self.assertEqual(saved["sources"][0]["raw_metadata"], metadata)
                    self.assertEqual(store.pending(), [])
                    self.assertEqual(store.search_articles(query="摩根士丹利")["total"], 1)
                    self.assertEqual(store.search_articles(institution="Morgan Stanley")["total"], 1)


class EntityNormalizationTests(unittest.TestCase):
    def test_aliases_and_explicit_market_qualifiers_are_conservative(self):
        self.assertEqual(canonical_institution("摩根士丹利"), "Morgan Stanley")
        self.assertEqual(canonical_institution("Unlisted Research Team"), "Unlisted Research Team")
        cases = {"hkex:700": "0700.HK", "700.HK": "0700.HK", "00700.HK": "0700.HK", "9988.HK": "9988.HK",
                 "HKEX:09988": "9988.HK", "12345.HK": "12345.HK",
                 "600519.SS": "600519.SH", "SSE:600519": "600519.SH", "SZSE:000001": "000001.SZ",
                 "000001": "000001", "700": "700", "$aapl": "AAPL", "BRK.B": "BRK.B"}
        for source, expected in cases.items():
            self.assertEqual(normalize_ticker(source), expected)
        self.assertEqual(normalize_ticker({"symbol": "700", "exchange": "HKEX"}), "0700.HK")
        self.assertEqual(normalize_ticker("Bearer synthetic-sensitive-value"), "")

    def test_structured_metadata_accepts_only_safe_named_values(self):
        metadata = normalize_metadata({"institution": {"name": "摩根士丹利", "token": "secret"},
                                       "authors": [{"name": "Alice", "sessionId": "secret"}, "Bob"],
                                       "tickers": [{"symbol": "700", "exchange": "HKEX", "token": "secret"}],
                                       "topics": ["AI", {"debug": "secret"}],
                                       "provider": "Bearer sensitive-value"})
        self.assertEqual(metadata["author"], "Alice; Bob")
        self.assertEqual(metadata["tickers"], ["0700.HK"])
        self.assertEqual(metadata["institution_original"], "摩根士丹利")
        self.assertNotIn("secret", json.dumps(metadata))
        self.assertEqual(metadata["provider"], "")


if __name__ == "__main__":
    unittest.main()
