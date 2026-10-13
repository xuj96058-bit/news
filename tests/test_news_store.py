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

    def test_investat_id_deduplicates_without_inventing_article_url(self):
        research = article(source_id="investat", source_name="Investat", source_kind="research",
                           external_id="research_123", url=None, content_scope="fragment")
        self.assertEqual(self.store.ingest([research])["added"], 1)
        saved = self.store.pending()[0]
        self.assertEqual(saved["canonical_url"], "investat:research_123")
        self.assertIsNone(saved["url"])
        self.assertIsNone(saved["sources"][0]["raw_metadata"]["url"])
        self.store.mark_analyzed(saved["id"], {"summary": "Completed research analysis"})
        self.assertEqual(self.store.ingest([research])["duplicates"], 1)
        self.assertEqual(self.store.pending(), [])
        with NewsStore(self.path) as reopened:
            same = reopened.export_articles()[0]
            self.assertEqual(same["id"], saved["id"])
            self.assertEqual(same["external_id"], "research_123")
            self.assertEqual(reopened.stats()["total"], 1)

    def test_investat_url_changes_keep_id_and_retained_text(self):
        research = article(source_id="investat", source_name="Investat", source_kind="research",
                           external_id="research_123", url=None, content="Provider body. " * 100,
                           content_scope="fragment")
        self.store.ingest([research])
        saved = self.store.pending()[0]
        self.store.mark_analyzed(saved["id"], {"summary": "Already analyzed"})
        for url in ("https://investat.ai/research/research_123",
                    "https://investat.ai/research/new-path?utm_source=mail"):
            with self.subTest(url=url):
                omitted_text = dict(research, url=url, summary="", content=None,
                                    content_scope="title_only")
                self.assertEqual(self.store.ingest([omitted_text])["duplicates"], 1)
                current = self.store.export_articles()[0]
                self.assertEqual(current["id"], saved["id"])
                self.assertEqual(current["canonical_url"], "investat:research_123")
                self.assertEqual(current["url"], url)
                self.assertEqual(current["content"], research["content"])
                self.assertEqual(current["content_scope"], "fragment")
                self.assertEqual(current["sources"][0]["raw_metadata"]["url"], url)
                self.assertEqual(self.store.pending(), [])
        omitted_url = dict(research, url=None, content=None, summary="", content_scope="title_only")
        self.assertEqual(self.store.ingest([omitted_url])["duplicates"], 1)
        retained = self.store.export_articles()[0]
        self.assertEqual(retained["url"], url)
        self.assertIsNone(retained["sources"][0]["raw_metadata"]["url"])
        self.assertEqual(retained["content"], research["content"])
        self.assertEqual(self.store.pending(), [])
        corrected = dict(research, url="https://investat.ai/research/new-path",
                         title="A corrected research headline")
        self.assertEqual(self.store.ingest([corrected])["updated"], 1)
        current = self.store.pending()[0]
        self.assertEqual(current["id"], saved["id"])
        self.assertEqual(current["url"], corrected["url"])
        self.assertIsNone(current["analysis"])
        self.assertEqual(self.store.stats()["total"], 1)

    def test_investat_shorter_prefix_keeps_richer_analysis_but_changed_text_requeues(self):
        research = article(source_id="investat", source_name="Investat", source_kind="research",
                           external_id="research_123", url=None, content="Provider text. " * 100,
                           content_scope="provider_text")
        self.store.ingest([research])
        saved = self.store.pending()[0]
        self.store.mark_analyzed(saved["id"], {"summary": "Rich text analysis"})
        prefix = dict(research, content=research["content"][:100], content_scope="partial_text")
        self.assertEqual(self.store.ingest([prefix])["duplicates"], 1)
        retained = self.store.export_articles()[0]
        self.assertEqual(retained["content"], research["content"])
        self.assertEqual(retained["content_scope"], "provider_text")
        self.assertEqual(retained["analysis"], {"summary": "Rich text analysis"})
        self.assertEqual(self.store.pending(), [])
        changed = dict(prefix, content="A correction contradicts the earlier report.")
        self.assertEqual(self.store.ingest([changed])["updated"], 1)
        pending = self.store.pending()[0]
        self.assertEqual(pending["id"], saved["id"])
        self.assertEqual(pending["content"], changed["content"])
        self.assertEqual(pending["content_scope"], "partial_text")
        self.assertIsNone(pending["analysis"])

    def test_investat_requires_safe_stable_id_and_valid_real_url_when_supplied(self):
        for identifier in (None, "", "../article", "article?token=value", "a" * 129):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.store.ingest([article(source_id="investat", external_id=identifier, url=None)])
        for url in ("not-a-url", "javascript:alert(1)", "https://token@example.org/article"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.store.ingest([article(source_id="investat", external_id="research_123", url=url)])
        self.assertEqual(self.store.stats()["total"], 0)
        with self.assertRaises(ValueError):
            self.store.ingest([article(source_id="public", url=None)])

    def test_distinct_investat_ids_do_not_merge_identical_research_text(self):
        first = article(source_id="investat", source_name="Investat", source_kind="research",
                        external_id="research_123", url=None)
        second = dict(first, external_id="research_456")
        self.assertEqual(self.store.ingest([first, second]),
                         {"added": 2, "updated": 0, "duplicates": 0})
        saved = self.store.export_articles()
        self.assertEqual({row["canonical_url"] for row in saved},
                         {"investat:research_123", "investat:research_456"})
        self.assertEqual(len({row["id"] for row in saved}), 2)
        self.assertEqual(self.store.ingest([second, first])["duplicates"], 2)
        self.assertEqual(self.store.stats()["total"], 2)


if __name__ == "__main__":
    unittest.main()

