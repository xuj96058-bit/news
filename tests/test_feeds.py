import json
import unittest
from unittest.mock import Mock, patch

import requests

from src.feeds import (
    FeedFetchError, MAX_RESPONSE_BYTES, Source, fetch_source, load_sources,
    parse_feed, parse_hkma, plain_text,
)


SOURCE = Source("test", "Test", "https://example.com/feed", "official", "global")


def rss(item):
    return ('<?xml version="1.0"?><rss version="2.0" '
            'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            '<channel><title>Feed</title>' + item + '</channel></rss>').encode()


class FeedTests(unittest.TestCase):
    def test_rss_summary_date_and_guid(self):
        articles = parse_feed(SOURCE, rss('''<item><title>Inflation &amp; growth</title>
          <link>https://example.com/story</link><guid>story-1</guid>
          <pubDate>Tue, 13 Oct 2026 08:00:00 +0800</pubDate>
          <description><![CDATA[<p>Prices <b>rose</b>.</p><script>secret()</script>]]></description>
          </item>'''))
        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article.title, "Inflation & growth")
        self.assertEqual(article.published_at, "2026-10-13T00:00:00+00:00")
        self.assertEqual(article.summary, "Prices rose.")
        self.assertEqual(article.content_scope, "summary")
        self.assertEqual(article.external_id, "story-1")
        self.assertEqual(article.to_dict()["source_kind"], "official")

    def test_namespaced_content_not_called_fulltext(self):
        article = parse_feed(SOURCE, rss('''<item><title>News</title><link>/story</link>
          <description>Summary</description><content:encoded><![CDATA[
          <p>Paragraph one.</p><style>hidden</style><p>Paragraph two.</p>
          ]]></content:encoded></item>'''))[0]
        self.assertEqual(article.url, "https://example.com/story")
        self.assertEqual(article.content, "Paragraph one. Paragraph two.")
        self.assertEqual(article.content_scope, "feed_content")
        self.assertIsNone(article.published_at)

    def test_atom_prefers_alternate_and_publication_date(self):
        body = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title>
        <entry><id>tag:example.com,2026:1</id><title>Rate decision</title>
        <link rel="self" href="https://example.com/api/1"/>
        <link rel="alternate" href="https://example.com/1"/>
        <published>2026-10-12T09:30:00-04:00</published>
        <updated>2026-10-13T00:00:00Z</updated><content type="html">&lt;p&gt;Details&lt;/p&gt;</content>
        </entry></feed>'''
        article = parse_feed(SOURCE, body)[0]
        self.assertEqual(article.url, "https://example.com/1")
        self.assertEqual(article.published_at, "2026-10-12T13:30:00+00:00")
        self.assertEqual(article.content, "Details")

    def test_local_date_and_missing_date(self):
        china = Source("nbs", "NBS", SOURCE.url, "official", "china", timezone="Asia/Shanghai")
        article = parse_feed(china, rss('''<item><title>GDP</title><link>/gdp</link>
          <pubDate>2026-10-13</pubDate></item>'''))[0]
        self.assertEqual(article.published_at, "2026-10-12T16:00:00+00:00")
        article = parse_feed(china, rss('<item><title>GDP</title><link>/gdp</link></item>'))[0]
        self.assertIsNone(article.published_at)
        self.assertEqual(article.content_scope, "title_only")

    def test_finra_publication_day_uses_source_zone_and_rejects_invalid_days(self):
        source = Source("finra_notices", "FINRA Notices", SOURCE.url, "official", "us",
                        timezone="America/New_York")
        for published, expected in (("Oct 09, 2026", "2026-10-09T04:00:00+00:00"),
                                    ("Jan 09, 2026", "2026-01-09T05:00:00+00:00"),
                                    ("Oct 32, 2026", None)):
            with self.subTest(published=published):
                article = parse_feed(source, rss(f'<item><title>Margin notice</title><link>/notice</link>'
                                                f'<pubDate>{published}</pubDate></item>'))[0]
                self.assertEqual(article.published_at, expected)

    def test_fed_rdf_preserves_release_date_and_summary_scope(self):
        body = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
          xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
          <channel rdf:about="https://example.com/feed"><title>SCOOS</title>
            <link>https://example.com/scoos</link><description>Updates</description>
            <items><rdf:Seq><rdf:li rdf:resource="https://example.com/update"/></rdf:Seq></items></channel>
          <item rdf:about="https://example.com/update"><title>DDP update</title>
            <link>https://example.com/update</link><dc:date>2026-07-16T14:00:00Z</dc:date>
            <description>Update notice; observation periods remain in the data tables.</description></item></rdf:RDF>'''
        article = parse_feed(SOURCE, body)[0]
        self.assertEqual(article.published_at, "2026-07-16T14:00:00+00:00")
        self.assertEqual(article.content_scope, "summary")
        self.assertEqual(article.content, "")

    def test_invalid_entries_skipped(self):
        body = rss('''<item><title>Bad URL</title><link>javascript:alert(1)</link></item>
          <item><title>Credentials</title><link>https://user:secret@example.com/</link></item>
          <item><link>https://example.com/no-title</link></item>
          <item><title>Valid</title><link>https://example.com/good</link></item>''')
        self.assertEqual([a.title for a in parse_feed(SOURCE, body)], ["Valid"])

    def test_reject_html_even_with_rss_tags_inside(self):
        for body in [b'<html><body>Sign in</body></html>',
                     b'<html><rss><channel><item><title>fake</title></item></channel></rss></html>',
                     b'Access denied', b'']:
            with self.subTest(body=body), self.assertRaises(FeedFetchError):
                parse_feed(SOURCE, body)

    def test_tolerate_recoverable_xml_only_with_real_entries(self):
        body = rss('<item><title>Rates & growth</title><link>/story</link></item>')
        self.assertEqual(len(parse_feed(SOURCE, body)), 1)
        with self.assertRaises(FeedFetchError):
            parse_feed(SOURCE, b'<rss version="2.0"><channel>')

    def test_empty_valid_feed(self):
        self.assertEqual(parse_feed(SOURCE, rss('')), [])

    def test_plain_text_excludes_script_and_style(self):
        self.assertEqual(plain_text('<p>A</p><script>alert(1)</script><style>b{}</style><p>B &amp; C</p>'), 'A B & C')

    def test_hkma_documented_schema(self):
        source = Source("hkma", "HKMA", SOURCE.url, "official", "hong_kong", "hkma_json", timezone="Asia/Hong_Kong")
        body = json.dumps({"header": {"success": True}, "result": {"datasize": 1, "records": [
            {"title": "Cargo<sup>x</sup>方案日", "date": "2026-10-05", "link": "https://www.hkma.gov.hk/chi/news/1/"}
        ]}}).encode()
        article = parse_hkma(source, body)[0]
        self.assertEqual(article.title, "Cargox方案日")
        self.assertEqual(article.published_at, "2026-10-04T16:00:00+00:00")
        self.assertEqual(article.content_scope, "title_only")
        self.assertEqual(article.content, "")

    def test_hkma_invalid_response_is_sanitized(self):
        for body in [b'not json secret-token', b'{"header":{"success":false,"err_msg":"secret"}}',
                     b'{"header":{"success":true},"result":{"records":{}}}']:
            with self.subTest(body=body), self.assertRaises(FeedFetchError) as ctx:
                parse_hkma(SOURCE, body)
            self.assertNotIn("secret", str(ctx.exception))

    def test_catalog_includes_regulators_and_state_street_and_is_cwd_independent(self):
        sources = load_sources()
        self.assertEqual(len(sources), 26)
        self.assertTrue(all(source.enabled for source in sources))
        self.assertEqual(next(source for source in sources if source.id == "hkma").format, "hkma_json")
        by_id = {source.id: source for source in sources}
        self.assertEqual(by_id["finra_notices"].timezone, "America/New_York")
        self.assertTrue({"fed_scoos", "fed_z1", "fed_notes", "finra_news", "finra_notices",
                         "sec_press", "cftc_press", "state_street_signals"}.issubset(by_id))

    def test_configuration_rejects_duplicates_and_non_http(self):
        valid = {"id":"test", "name":"Test", "url":"https://example.com/", "kind":"media", "region":"global"}
        for value in [[valid, valid], [dict(valid, url="file:///secret")], {}]:
            with patch("src.feeds.Path.read_text", return_value=json.dumps(value)):
                with self.assertRaises(ValueError):
                    load_sources()


class FetchTests(unittest.TestCase):
    def response(self, body, status=200, headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.iter_content.return_value = [body]
        return response

    def test_fetch_sets_timeout_agent_and_closes_response(self):
        response = self.response(rss('<item><title>T</title><link>/t</link></item>'))
        session = Mock()
        session.get.return_value = response
        self.assertEqual(len(fetch_source(SOURCE, session=session)), 1)
        kwargs = session.get.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 15)
        self.assertTrue(kwargs["stream"])
        self.assertIn("news-finance-reader", kwargs["headers"]["User-Agent"])
        response.close.assert_called()
        session.close.assert_not_called()

    def test_403_no_retry_or_body_leak(self):
        session = Mock()
        session.get.return_value = self.response(b'secret response', 403)
        with self.assertRaisesRegex(FeedFetchError, "^HTTP 403$"):
            fetch_source(SOURCE, session=session)
        self.assertEqual(session.get.call_count, 1)

    @patch("src.feeds.time.sleep")
    def test_timeout_retried_once_and_sanitized(self, sleep):
        session = Mock()
        session.get.side_effect = requests.Timeout("secret-api-key")
        with self.assertRaises(FeedFetchError) as ctx:
            fetch_source(SOURCE, session=session)
        self.assertEqual(session.get.call_count, 2)
        self.assertNotIn("secret", str(ctx.exception))
        sleep.assert_called_once_with(0.5)

    @patch("src.feeds.time.sleep")
    def test_transient_http_retries(self, sleep):
        session = Mock()
        session.get.side_effect = [self.response(b'busy', 503), self.response(rss(''))]
        self.assertEqual(fetch_source(SOURCE, session=session), [])
        self.assertEqual(session.get.call_count, 2)

    def test_response_size_limited_even_without_content_length(self):
        for headers, body in [({"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, b''),
                              ({}, b'x' * (MAX_RESPONSE_BYTES + 1))]:
            session = Mock()
            session.get.return_value = self.response(body, headers=headers)
            with self.subTest(headers=headers), self.assertRaisesRegex(FeedFetchError, "5 MiB"):
                fetch_source(SOURCE, session=session)


if __name__ == "__main__":
    unittest.main()
