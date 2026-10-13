import tempfile
import unittest
from pathlib import Path

from src.news_store import NewsStore, canonicalize_url


def article(**overrides):
    result = {
        "source_id": "fed", "source_name": "Federal Reserve", "source_kind": "official",
        "region": "US", "title": "Federal Reserve announces policy decision",
        "url": "https://example.com/decision?id=1", "published_at": "2026-10-13T08:00:00Z",
        "summary": "The central bank kept rates unchanged.", "content": "", "content_scope": "summary",
        "external_id": "one",
    }
    result.update(overrides)
    return result


class NewsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nested" / "news.sqlite3"
        self.store = NewsStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_canonical_url_preserves_meaningful_query(self):
        self.assertEqual(
            canonicalize_url("HTTPS://Example.COM:443/news?id=12&utm_source=x&ref=bank&fbclid=1#top"),
            "https://example.com/news?id=12&ref=bank",
        )
        self.assertNotEqual(canonicalize_url("https://x.test/?id=1"), canonicalize_url("https://x.test/?id=2"))

    def test_unchanged_repeated_run_does_not_reanalyze(self):
        self.assertEqual(self.store.ingest([article()]), {"added": 1, "updated": 0, "duplicates": 0})
        record = self.store.pending()[0]
        self.store.mark_analyzed(record["id"], {"event": "decision"})
        self.assertEqual(self.store.ingest([article(url=article()["url"] + "&utm_source=email#top")])["duplicates"], 1)
        self.assertEqual(self.store.pending(), [])
        with NewsStore(self.path) as reopened:
            self.assertEqual(reopened.stats()["analyzed"], 1)
            self.assertEqual(reopened.export_articles()[0]["id"], record["id"])

    def test_exact_cross_source_duplicate_keeps_attribution_and_original_metadata(self):
        original = article(extra_field={"feed": "unchanged"})
        syndicated = article(source_id="bbc", source_name="BBC", source_kind="media", url="https://bbc.test/policy")
        result = self.store.ingest([original, syndicated])
        self.assertEqual(result, {"added": 1, "updated": 0, "duplicates": 1})
        record = self.store.export_articles()[0]
        self.assertEqual(len(record["sources"]), 2)
        fed = next(s for s in record["sources"] if s["source_id"] == "fed")
        self.assertEqual(fed["raw_metadata"]["extra_field"], {"feed": "unchanged"})
        self.store.mark_analyzed(record["id"], {})
        self.assertEqual(self.store.ingest([syndicated, original])["duplicates"], 2)
        self.assertEqual(self.store.pending(), [])

    def test_changed_article_invalidates_result_and_preserves_full_content(self):
        self.store.ingest([article()])
        record = self.store.pending()[0]
        self.store.mark_analyzed(record["id"], {"outdated": True})
        large_content = "Policy text. " * 500
        self.assertEqual(self.store.ingest([article(content=large_content, content_scope="feed_content")])["updated"], 1)
        updated = self.store.pending()[0]
        self.assertEqual(updated["id"], record["id"])
        self.assertIsNone(updated["analysis"])
        self.assertEqual(updated["content"], large_content)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.mark_analyzed(record["id"], {"stale": True}, expected_content_hash=record["content_hash"])
        self.store.mark_analyzed(updated["id"], {}, expected_content_hash=updated["content_hash"])
        self.assertEqual(self.store.pending(), [])

    def test_cross_source_changes_do_not_alternate_analysis_on_every_run(self):
        first = article()
        second = article(source_id="bbc", url="https://bbc.test/policy")
        self.store.ingest([first, second])
        changed = dict(second, summary="The central bank raised rates in a corrected report.")
        self.assertEqual(self.store.ingest([first, changed])["updated"], 1)
        self.store.mark_analyzed(self.store.pending()[0]["id"], {})
        self.assertEqual(self.store.ingest([changed, first])["duplicates"], 2)
        self.assertEqual(self.store.pending(), [])

    def test_same_headline_different_body_or_day_is_not_merged(self):
        records = [
            article(),
            article(url="https://other.test/different", summary="The central bank raised rates."),
            article(url="https://other.test/tomorrow", published_at="2026-10-14T08:00:00Z"),
            article(url="https://other.test/empty1", summary=""),
            article(url="https://other.test/empty2", summary=""),
        ]
        self.assertEqual(self.store.ingest(records)["added"], 5)

    def test_failed_analysis_is_retryable_and_error_is_redacted(self):
        self.store.ingest([article()])
        record = self.store.pending()[0]
        self.store.mark_error(record["id"], "api_key=secretvalue Bearer sensitive sk-test_secret")
        retry = self.store.pending()[0]
        self.assertEqual(retry["status"], "error")
        for secret in ("secretvalue", "sensitive", "sk-test_secret"):
            self.assertNotIn(secret, retry["error"])
        self.store.ingest([article()])
        self.assertEqual(len(self.store.pending()), 1)
        self.store.mark_analyzed(record["id"], {})
        self.assertIsNone(self.store.export_articles()[0]["error"])

    def test_recent_dated_first_and_original_dates_retained(self):
        self.store.ingest([
            article(url="https://x.test/a", published_at=None),
            article(url="https://x.test/b", published_at="Tue, 13 Oct 2026 08:00:00 +0000"),
            article(url="https://x.test/c", summary="Another update", published_at="2026-10-13T17:00:00+08:00"),
        ])
        records = self.store.pending()
        self.assertEqual([r["url"] for r in records], ["https://x.test/c", "https://x.test/b", "https://x.test/a"])
        self.assertEqual(records[1]["published_at"], "Tue, 13 Oct 2026 08:00:00 +0000")
        self.assertEqual(self.store.pending(limit=0), [])

    def test_empty_fields_do_not_erase_previous_body(self):
        self.store.ingest([article(content="Full release content", content_scope="feed_content")])
        record = self.store.pending()[0]
        self.store.mark_analyzed(record["id"], {})
        self.assertEqual(self.store.ingest([article(content=None, summary="")])["duplicates"], 1)
        self.assertEqual(self.store.export_articles()[0]["content"], "Full release content")
        self.assertEqual(self.store.export_articles()[0]["content_scope"], "feed_content")
        self.assertEqual(self.store.pending(), [])

    def test_new_source_same_url_without_body_preserves_analyzed_content(self):
        self.store.ingest([article(content="Full release content", content_scope="feed_content")])
        record = self.store.pending()[0]
        self.store.mark_analyzed(record["id"], {})
        partial = article(source_id="second", content=None, summary="", content_scope="title_only")
        self.assertEqual(self.store.ingest([partial])["duplicates"], 1)
        self.assertEqual(self.store.export_articles()[0]["content"], "Full release content")
        self.assertEqual(self.store.pending(), [])

    def test_metadata_only_headlines_do_not_starve_older_text(self):
        self.store.ingest([
            article(url="https://x.test/useful", published_at="2026-10-10T08:00:00Z"),
            article(url="https://x.test/title-only", published_at="2026-10-13T08:00:00Z",
                    summary="", content_scope="title_only"),
            article(url="https://x.test/repeated-title", published_at="2026-10-12T08:00:00Z",
                    summary=article()["title"]),
            article(url="https://x.test/empty", published_at="2026-10-11T08:00:00Z",
                    summary="   ", content=""),
        ])
        self.assertEqual(self.store.pending(limit=1)[0]["url"], "https://x.test/title-only")
        selected = self.store.pending(limit=1, require_text=True)
        self.assertEqual([row["url"] for row in selected], ["https://x.test/useful"])
        self.assertEqual(self.store.pending(limit=0, require_text=True), [])

    def test_unattempted_article_precedes_newer_failed_retry(self):
        self.store.ingest([
            article(url="https://x.test/older", summary="An older usable report.",
                    published_at="2026-10-10T08:00:00Z"),
            article(url="https://x.test/newer", summary="A newer report whose analysis fails.",
                    published_at="2026-10-13T08:00:00Z"),
        ])
        newest = self.store.pending(limit=1, require_text=True)[0]
        self.assertEqual(newest["url"], "https://x.test/newer")
        self.store.mark_error(newest["id"], "TimeoutError")
        next_article = self.store.pending(limit=1, require_text=True)[0]
        self.assertEqual(next_article["url"], "https://x.test/older")
        self.store.mark_analyzed(next_article["id"], {})
        self.assertEqual(self.store.pending(limit=1, require_text=True)[0]["id"], newest["id"])

    def test_filing_notice_is_not_analyzable_even_with_summary(self):
        self.store.ingest([
            article(url="https://x.test/actual-news", published_at="2026-10-10T08:00:00Z"),
            article(url="https://www.sec.gov/Archives/example", source_id="sec-company",
                    title="8-K - Company Inc.", summary="Filing Date: 2026-10-13; Size: 58 KB",
                    content_scope="filing_notice", published_at="2026-10-13T08:00:00Z"),
        ])
        self.assertEqual(self.store.pending(limit=1)[0]["source_id"], "sec-company")
        self.assertEqual(self.store.pending(limit=1, require_text=True)[0]["url"], "https://x.test/actual-news")

    def test_batch_rolls_back_on_invalid_article(self):
        with self.assertRaises(ValueError):
            self.store.ingest([article(), article(url="not-a-url")])
        self.assertEqual(self.store.stats()["total"], 0)


if __name__ == "__main__":
    unittest.main()
