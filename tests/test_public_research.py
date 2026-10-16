from dataclasses import replace
from datetime import datetime, timezone
import html
import json
import unittest
from unittest.mock import Mock

from src.feeds import FeedFetchError, Source, fetch_source, load_sources
from src.public_research import parse_public_research, parse_hkex_announcements, parse_hkex_json
from src.news_store import NewsStore
from src.rss_pipeline import collect_sources
from src.source_health import health_summary


def source(format):
    urls = {"apollo_html": "https://www.apollo.com/daily-spark",
            "ubs_html": "https://www.ubs.com/global/en/wealthmanagement/insights.html",
            "blackrock_html": "https://www.blackrock.com/corporate/insights",
            "jpm_html": "https://am.jpmorgan.com/wr/en/guide-to-the-markets/",
            "hkex_announcements": "https://www1.hkexnews.hk/search/titleSearchServlet.do"}
    return Source(format, format, urls[format], "research", "global", format,
                  timezone="Asia/Hong_Kong" if format == "hkex_announcements" else "UTC")


def attribute(name, value):
    return f'<div {name}="{html.escape(json.dumps(value))}"></div>'


HKEX_ROW = '''<tr><td class="release-time"><span>Release Time: </span>16/10/2026 19:17</td>
<td class="stock-short-code"><span>Stock Code: </span>00700</td>
<td class="stock-short-name"><span>Stock Short Name: </span>TENCENT</td>
<td><div class="headline">Announcements and Notices - [Results]</div><div class="doc-link">
<a href="/listedco/listconews/sehk/2026/1016/report.pdf">Quarterly results</a></div></td></tr>'''


class PublicCollectorTests(unittest.TestCase):
    def test_apollo_structured_carousel_deduplicates_and_text_only_posts_are_kept(self):
        record = {"blogId": "article-1", "title": "Rates", "detailLink": "/wealth/insights-news/insights/daily-spark/rates",
                  "blogDate": "2026-10-16", "description": "Short introduction", "content": "<p>Actual supplied body.</p>",
                  "aboutTheAuthor": {"firstName": "Torsten", "lastName": "Slok"}, "eyebrowText": "Monetary Policy"}
        body = attribute("data-itemDetails", record) * 2 + '''<div class="blog-detail">
        <p class="blog-detail-info-date">October 15, 2026</p>
        <a class="blog-detail-info-title" href="/wealth/insights-news/insights/daily-spark/papers">Papers</a>
        <div class="blog-detail-content-fragments">Text-only research post.</div></div>'''
        items = parse_public_research(source("apollo_html"), body.encode())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["author"], "Torsten Slok")
        self.assertEqual(items[0]["institution"], "Apollo")
        self.assertEqual(items[0]["content_scope"], "partial_text")
        self.assertEqual(items[1]["published_at"], "2026-10-15T00:00:00+00:00")

    def test_ubs_episode_url_is_real_audio_and_summary_is_not_transcript(self):
        data = {"@type": "PodcastSeries", "hasPart": [{"@type": "PodcastEpisode", "name": "House View",
                "datePublished": "2026-10-15T09:00:00Z", "description": "The actual description.",
                "associatedMedia": {"contentUrl": "https://www.ubs.com/content/dam/podcasts/episode.mp3"}}]}
        body = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
        item = parse_public_research(source("ubs_html"), body.encode())[0]
        self.assertEqual(item["content"], "")
        self.assertEqual(item["content_scope"], "summary")
        self.assertTrue(item["url"].endswith("episode.mp3"))

    def test_blackrock_uses_card_metadata_not_navigation_links(self):
        data = {"articles": [{"title": "Outlook", "link": "https://www.blackrock.com/corporate/insights/outlook",
                "publicationDate": 1790553600000, "summary": "A publisher-supplied summary.",
                "authors": [{"label": "BlackRock Investment Institute"}]}]}
        item = parse_public_research(source("blackrock_html"), attribute("componentprops", data).encode())[0]
        self.assertEqual(item["published_at"], "2026-09-28T00:00:00+00:00")
        self.assertEqual(item["institution"], "BlackRock")

    def test_jpm_links_are_documents_without_fabricated_dates_or_bodies(self):
        body = b'''<a href="https://am.jpmorgan.com/content/guide-to-the-markets/us.pdf"><h2>United States</h2>Read more</a>
        <a href="https://evil.test/guide-to-the-markets/fake.pdf"><h2>Fake</h2></a>
        <a href="/guide-to-the-markets/">Hub</a>'''
        items = parse_public_research(source("jpm_html"), body)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Guide to the Markets — United States")
        self.assertEqual(items[0]["content_scope"], "document_link")
        self.assertIsNone(items[0]["published_at"])
        self.assertEqual(items[0]["summary"], "")

    def test_hkex_returns_issuer_ticker_local_time_and_explicit_truncation(self):
        batch = parse_hkex_announcements(source("hkex_announcements"), ('<div>Total records found: 1,230</div><table>' + HKEX_ROW + '</table>').encode())
        self.assertEqual(batch[0]["tickers"], ["00700.HK"])
        self.assertEqual(batch[0]["publisher"], "TENCENT")
        self.assertEqual(batch[0]["published_at"], "2026-10-16T11:17:00+00:00")
        self.assertEqual(batch[0]["content_scope"], "announcement_metadata")
        self.assertEqual(batch.diagnostics["upstream_limited"], 1229)
        self.assertTrue(batch.diagnostics["gap"])

    def test_schema_failures_are_not_successful_empty_results(self):
        for format in ("apollo_html", "ubs_html", "blackrock_html", "jpm_html"):
            with self.subTest(format=format), self.assertRaises(FeedFetchError):
                parse_public_research(source(format), b'<html>Access denied</html>')
        with self.assertRaises(FeedFetchError):
            parse_hkex_announcements(source("hkex_announcements"), b'<html>Access denied</html>')
        self.assertEqual(len(parse_hkex_announcements(source("hkex_announcements"), b'Total records found: 0')), 0)

    def test_hkex_fetch_uses_official_loadmore_get_and_bounded_dates(self):
        session = Mock()
        response = Mock(status_code=200)
        response.iter_content.return_value = [b'{"result":"[]","recordCnt":0,"loadedRecord":0,"hasNextRow":false}']
        session.get.return_value = response
        batch = fetch_source(source("hkex_announcements"), session=session)
        payload = session.get.call_args.kwargs["params"]
        start, end = (datetime.strptime(payload[k], "%Y%m%d") for k in ("fromDate", "toDate"))
        self.assertEqual((end - start).days, 6)
        self.assertEqual(payload["rowRange"], "500")
        self.assertEqual(batch.diagnostics["window_from"], payload["fromDate"])
        response.close.assert_called()
        session.post.assert_not_called()

    def test_hkex_official_json_schema_and_failures(self):
        records = [{"DATE_TIME": "16/10/2026 19:17", "STOCK_CODE": "00700", "STOCK_NAME": "TENCENT",
                    "TITLE": "Quarterly results", "FILE_LINK": "/listedco/listconews/sehk/2026/1016/report.pdf",
                    "LONG_TEXT": "Results"}]
        data = {"result": json.dumps(records), "loadedRecord": 1, "recordCnt": 100, "hasNextRow": True}
        batch = parse_hkex_json(source("hkex_announcements"), json.dumps(data).encode())
        self.assertEqual(batch[0]["publisher"], "TENCENT")
        self.assertEqual(batch.diagnostics["upstream_limited"], 99)
        for body in (b'Access denied', b'{"result":"[]"}', b'{"result":"[]","recordCnt":2,"loadedRecord":1}'):
            with self.subTest(body=body), self.assertRaises(FeedFetchError):
                parse_hkex_json(source("hkex_announcements"), body)

    def test_upstream_gap_reaches_report_and_persistent_health(self):
        src = source("hkex_announcements")
        batch = parse_hkex_announcements(src, ('Total records found: 10<table>' + HKEX_ROW + '</table>').encode())
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        with NewsStore(":memory:") as store:
            report = collect_sources([src], store, fetcher=lambda *a, **k: batch, now=now)
            self.assertEqual(report["sources"][0]["status"], "partial")
            self.assertEqual(report["failed_sources"], 1)
            health = health_summary(store, now=now)[0]
            self.assertEqual(health["gap_runs"], 1)
            self.assertEqual(health["usable_text_ratio"], 0)
            self.assertEqual(health["added"], 1)
            saved = store.export_articles()[0]
            self.assertEqual(saved["publisher"], "TENCENT")
            self.assertIn("0700.HK", saved["tickers"])


if __name__ == "__main__":
    unittest.main()
