"""Offline regressions for relevance, complete-source preservation and URL safety."""
import copy
from io import BytesIO
import socket
import unittest
from unittest.mock import Mock, patch

from src.research_quality import rank_articles, select_relevant_passages
from src.research_retrieval import MAX_BYTES, RetrievalError, _PublicHTTPSConnection, enrich_sources


TOPIC = "行業擁擠度與美股槓桿集中度"


def source(identifier="S1", **changes):
    return {"id": identifier, "title": "FINRA margin debt balances for US equity accounts",
            "publisher": "FINRA", "url": "https://www.finra.org/margin-statistics",
            "content": "FINRA margin debt summary.", "content_scope": "summary",
            "origins": [{"source_id": "finra_news", "url": "https://www.finra.org/margin-statistics"}],
            "retrieved_at": "2026-10-14T00:00:00+00:00", **changes}


class Response:
    def __init__(self, body=b"", status=200, headers=None, url=None, history=()):
        self.body, self.status_code = body, status
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.url, self.history, self.closed = url, history, False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


def article_html():
    return ("<nav>Margin debt menu only</nav><main><p>" + "Unrelated front matter. " * 900 +
            "</p><h2>FINRA margin debt for all margin accounts (USD million)</h2>"
            "<table><tr><th>Month</th><th>Margin debt USD million</th></tr>"
            "<tr><td>2026-07</td><td>100</td></tr><tr><td>2026-08</td><td>120</td></tr></table>"
            "<p>These margin balances refer to US equity accounts, not sector holdings.</p></main>").encode()


class ResearchSelectionTests(unittest.TestCase):
    def test_general_news_does_not_match_financial_leverage_topic(self):
        articles = [
            {"title": "Pope Leo health news", "content": "A popular news report for US readers"},
            {"title": "How to book a flight with points", "content": "Industry travel tips for US stock investors"},
            {"title": "Meta expands AI capital spending", "content": "Technology sector demand grows"},
            {"title": "FINRA margin debt rises", "content": "US securities debit balances"},
        ]
        self.assertEqual([item["article"] for item in rank_articles(articles, TOPIC)], [articles[-1]])

    def test_duplicate_event_retains_attribution_without_repeating_the_story(self):
        original = {"title": "FINRA margin debt rises in August 2026", "url": "https://www.finra.org/data?table=margin&utm_source=feed"}
        duplicate = {"title": "Margin debt data release", "url": "https://www.finra.org/data?table=margin#stats"}
        other_table = {"title": "FINRA margin debt historical comparison", "url": "https://www.finra.org/data?table=history"}
        articles = [original, duplicate, other_table]
        saved = copy.deepcopy(articles)
        ranked = rank_articles(articles, TOPIC)
        self.assertEqual(len(ranked), 2)
        merged = next(item for item in ranked if "duplicate_articles" in item)
        self.assertEqual(len(merged["duplicate_articles"]), 1)
        self.assertEqual(articles, saved)

    def test_exact_syndicated_headline_is_one_event_across_publishers(self):
        articles = [{"title": "FINRA margin debt climbs in August 2026", "publisher": "A"},
                    {"title": "FINRA margin debt climbs in August 2026", "publisher": "B"}]
        self.assertEqual(len(rank_articles(articles, TOPIC)), 1)

    def test_source_diversity_breaks_near_score_ties(self):
        articles = [{"title": "Margin debt leverage", "publisher": "A", "url": "https://www.finra.org/1"},
                    {"title": "Margin debt leverage", "publisher": "A", "url": "https://www.finra.org/2", "event_id": "second"},
                    {"title": "Margin debt leverage", "publisher": "B", "url": "https://www.sec.gov/3", "event_id": "third"}]
        ranked = rank_articles(articles, "Margin debt leverage", deduplicate=False)
        self.assertEqual([item["article"]["publisher"] for item in ranked], ["A", "B", "A"])

    def test_passage_selection_keeps_late_evidence_and_table_header(self):
        content = "General front matter. " * 1000 + "\n\nFINRA margin debt USD million\nMonth | Margin debt USD million\n2026-07 | 100\n2026-08 | 120"
        chosen = select_relevant_passages(content, TOPIC, max_chars=3000)
        self.assertIn("2026-08 | 120", chosen["content"])
        self.assertIn("Month | Margin debt USD million", chosen["content"])
        self.assertNotIn("General front matter", chosen["content"])
        self.assertGreater(chosen["spans"][0]["start"], 12000)
        self.assertTrue(chosen["content_truncated"])
        for span in chosen["spans"]:
            self.assertIn(content.strip()[span["start"]:span["end"]], chosen["content"])

    def test_late_table_row_keeps_original_column_header(self):
        content = "Month | Population | Holdings percent\n" + "2026-08 | Other category | 1\n" * 200
        content += "2026-08 | Technology sector institutional holdings | 18\n"
        selected = select_relevant_passages(content, TOPIC, max_chars=1800)
        self.assertIn("Month | Population | Holdings percent", selected["content"])
        self.assertIn("Technology sector institutional holdings | 18", selected["content"])
        self.assertLessEqual(len(selected["content"]), 1800)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.lookup = patch("src.research_retrieval.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
        self.lookup.start()
        self.addCleanup(self.lookup.stop)

    def test_capture_keeps_source_ids_originals_and_selects_late_table(self):
        articles = [source()]
        original = copy.deepcopy(articles)
        response = Response(article_html())
        transport = Mock(return_value=response)
        enriched, outcomes = enrich_sources(articles, topic=TOPIC, get=transport)
        self.assertEqual(articles, original)
        self.assertEqual(enriched[0]["id"], "S1")
        self.assertEqual(enriched[0]["original_snapshot"], original[0])
        self.assertEqual(enriched[0]["origins"], original[0]["origins"])
        self.assertIn("2026-08 | 120", enriched[0]["content"])
        self.assertNotIn("menu only", enriched[0]["content"])
        self.assertEqual(enriched[0]["content_scope"], "html_excerpt")
        self.assertEqual(outcomes[0]["status"], "ok")
        self.assertTrue(response.closed)
        self.assertFalse(transport.call_args.kwargs["allow_redirects"])
        self.assertEqual(transport.call_args.kwargs["timeout"], (3, 8))

    def test_failed_capture_does_not_replace_supplied_text_or_dates(self):
        original = source()
        transport = Mock(side_effect=RuntimeError("secret transport details"))
        enriched, outcomes = enrich_sources([original], topic=TOPIC, get=transport)
        for key in ("content", "content_scope", "id", "retrieved_at", "origins"):
            self.assertEqual(enriched[0][key], original[key])
        self.assertEqual(outcomes, [{"source_id": "S1", "status": "error", "reason": "fetch_failed"}])
        self.assertNotIn("secret", repr(enriched) + repr(outcomes))

    def test_provided_and_full_text_sources_are_preserved_without_network(self):
        transport = Mock()
        originals = [source("S1", content_scope="provided_text"), source("S2", content_scope="full_text"),
                     source("S3", content_scope="official_evidence")]
        enriched, outcomes = enrich_sources(originals, topic=TOPIC, get=transport)
        self.assertEqual(enriched, originals)
        self.assertTrue(all(item["status"] == "preserved" for item in outcomes))
        transport.assert_not_called()

    def test_private_credential_nonhttps_and_unlisted_urls_never_reach_transport(self):
        for url in ("https://127.0.0.1/secret", "https://[::1]/secret", "https://localhost/secret",
                    "https://www.sec.gov@127.0.0.1/", "https://www.sec.gov.evil.example/",
                    "https://internal.example/", "http://www.finra.org/data", "https://www.sec.gov:8443/data",
                    "https://user:pass@www.sec.gov/data", "https://www.sec.gov/data?api_key=secret"):
            with self.subTest(url=url):
                transport = Mock()
                enriched, outcomes = enrich_sources([source(url=url)], topic=TOPIC, get=transport)
                transport.assert_not_called()
                self.assertEqual(outcomes[0]["status"], "error")
                self.assertEqual(enriched[0]["content"], source()["content"])

    def test_allowed_hostname_resolving_private_is_rejected(self):
        with patch("src.research_retrieval.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))]):
            transport = Mock()
            _, outcomes = enrich_sources([source()], topic=TOPIC, get=transport)
            transport.assert_not_called()
            self.assertEqual(outcomes[0]["reason"], "unsafe_address")

    def test_redirect_to_private_host_is_not_followed(self):
        response = Response(status=302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
        transport = Mock(return_value=response)
        enriched, outcomes = enrich_sources([source()], topic=TOPIC, get=transport)
        self.assertEqual(transport.call_count, 1)
        self.assertTrue(response.closed)
        self.assertEqual(outcomes[0]["status"], "error")
        self.assertEqual(enriched[0]["content"], source()["content"])

    def test_public_relative_redirect_is_checked_and_captured(self):
        redirect = Response(status=302, headers={"Location": "/rules-guidance/margin"})
        document = Response(article_html())
        transport = Mock(side_effect=[redirect, document])
        enriched, outcomes = enrich_sources([source()], topic=TOPIC, get=transport)
        self.assertEqual(outcomes[0]["status"], "ok")
        self.assertEqual(enriched[0]["url"], "https://www.finra.org/rules-guidance/margin")
        self.assertTrue(redirect.closed and document.closed)

    def test_oversized_response_and_irrelevant_page_preserve_original(self):
        for response in (Response(b"", headers={"Content-Length": str(MAX_BYTES + 1)}),
                         Response(("<article><p>Holiday travel tips. " * 100 + "</p></article>").encode())):
            _, outcomes = enrich_sources([source()], topic=TOPIC, get=Mock(return_value=response))
            self.assertEqual(outcomes[0]["status"], "error")
            self.assertTrue(response.closed)

    def test_fetch_limit_and_missing_url_have_explicit_outcomes(self):
        articles = [source("S1"), source("S2", url=None, title="SEC private fund borrowing"),
                    source("S3", url="https://www.sec.gov/pf", title="Private funds leverage concentration")]
        transport = Mock(return_value=Response(article_html()))
        _, outcomes = enrich_sources(articles, topic=TOPIC, max_fetches=1, get=transport)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(outcomes[1]["reason"], "missing_url")
        self.assertEqual(sum(item.get("reason") == "fetch_budget" for item in outcomes), 1)
        self.assertEqual(sum(item["status"] == "ok" for item in outcomes), 1)

    def test_invalid_fetch_limit_rejected(self):
        for limit in (-1, 13, True, 1.5):
            with self.assertRaises(ValueError):
                enrich_sources([source()], topic=TOPIC, max_fetches=limit)

    def test_pdf_selects_relevant_last_page_instead_of_front_matter(self):
        from reportlab.pdfgen.canvas import Canvas
        buffer = BytesIO()
        canvas = Canvas(buffer)
        for index in range(3):
            canvas.drawString(40, 700, "Unrelated introduction page " + str(index + 1))
            canvas.showPage()
        canvas.drawString(40, 700, "FINRA margin debt for all margin accounts in USD million")
        canvas.drawString(40, 675, "2026-07 margin debt 100 USD million")
        canvas.drawString(40, 650, "2026-08 margin debt 120 USD million")
        canvas.save()
        response = Response(buffer.getvalue(), headers={"Content-Type": "application/pdf"})
        enriched, outcomes = enrich_sources([source(url="https://www.finra.org/data.pdf")], topic=TOPIC, get=Mock(return_value=response))
        self.assertEqual(outcomes[0]["status"], "ok")
        self.assertEqual(enriched[0]["content_scope"], "pdf_excerpt")
        self.assertIn("2026-08 margin debt 120 USD million", enriched[0]["content"])
        self.assertNotIn("Unrelated introduction", enriched[0]["content"])

    def test_transport_connects_validated_address_without_second_dns_lookup(self):
        transport = _PublicHTTPSConnection("www.sec.gov", 443, timeout=3)
        connection = Mock()
        with patch("src.research_retrieval.socket.socket", return_value=connection), patch(
                "src.research_retrieval.socket.getaddrinfo", return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]) as lookup:
            self.assertIs(transport._new_conn(), connection)
            self.assertEqual(lookup.call_count, 1)
            connection.connect.assert_called_once_with(("8.8.8.8", 443))

    def test_transport_rejects_private_resolution_before_socket_creation(self):
        transport = _PublicHTTPSConnection("www.sec.gov", 443, timeout=3)
        with patch("src.research_retrieval.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443))]), patch(
                    "src.research_retrieval.socket.socket") as make_socket:
            with self.assertRaises(RetrievalError):
                transport._new_conn()
            make_socket.assert_not_called()

    def test_enrichment_respects_existing_total_report_character_budget(self):
        originals = [source("S1", content=("Margin debt history. " * 500)[:10000])]
        originals.extend(source("S" + str(index), content="Preserved evidence. " * 500,
                                content_scope="provided_text", title="Provided margin data " + str(index))
                         for index in range(2, 19))
        # Fill the remaining allowance without changing supplied/full evidence.
        originals[-1]["content"] += " " * (180000 - sum(len(item["content"]) for item in originals))
        document = Response(("<article>" + "<p>FINRA margin debt in US equity accounts.</p>" * 600 + "</article>").encode())
        enriched, outcomes = enrich_sources(originals, topic=TOPIC, get=Mock(return_value=document))
        self.assertLessEqual(sum(len(item["content"]) for item in enriched), 180000)
        self.assertEqual(enriched[1:], originals[1:])
        self.assertIn(outcomes[0]["status"], {"ok", "error"})

    def test_chinese_daily_market_topic_can_retrieve_english_financial_news(self):
        financial = source(title="Treasury yields fall as investors await inflation report",
                           url="https://www.cnbc.com/markets/treasury-yields.html")
        unrelated = [source("S2", title="How to book a flight with points", content="Tips for stock investors on holiday."),
                     source("S3", title="Pope Leo health news", content="Today's important change for financial market readers.")]
        html = ("<article><p>Treasury yields fell as investors awaited the consumer inflation report. "
                "Interest rates and bond prices moved as trading volumes increased.</p></article>").encode()
        transport = Mock(return_value=Response(html))
        enriched, outcomes = enrich_sources([financial, *unrelated], topic="今日金融市場的重要變化與影響", get=transport)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(outcomes[0]["status"], "ok")
        self.assertEqual(enriched[0]["content_scope"], "html_excerpt")
        self.assertTrue(all(item.get("reason") == "unrelated" for item in outcomes[1:]))

    def test_daily_market_fallback_does_not_override_a_specific_leverage_topic(self):
        transport = Mock()
        articles = [source(title="Treasury yields and inflation", content="Bond markets moved.")]
        _, outcomes = enrich_sources(articles, topic="今日金融市場槓桿集中度", get=transport)
        transport.assert_not_called()
        self.assertEqual(outcomes[0]["reason"], "unrelated")

    def test_daily_market_fallback_keeps_chinese_financial_signals(self):
        financial = source(title="央行維持政策利率", content="央行最新利率公告。")
        html = ("<article><p>央行維持政策利率，債券市場與股市反應分化。"
                "本次利率公告提供政策決策的背景，仍應對照原始數據與完整公告，並區分既有利率與未來預測。"
                "財報與股票市場變化屬於不同觀察，不直接構成因果證據。</p></article>").encode()
        enriched, outcomes = enrich_sources([financial], topic="今日金融市場的重要變化與影響", get=Mock(return_value=Response(html)))
        self.assertEqual(outcomes[0]["status"], "ok")
        self.assertIn("央行維持政策利率", enriched[0]["content"])

    def test_selected_contiguous_passage_remains_an_exact_observation_anchor(self):
        from src.research_quality import build_research_brief
        excerpt = "Survey A: US retail widget sales in 2026-08 were 20 USD million."
        html = ("<article><p>" + "Front matter. " * 1600 + "</p><p>" + excerpt + "</p></article>").encode()
        articles = [source(title="Widget sales", content="US retail widget sales summary.")]
        enriched, outcomes = enrich_sources(articles, topic="Widget sales", get=Mock(return_value=Response(html)))
        self.assertEqual(outcomes[0]["status"], "ok")
        observations = [{"source_id": "S1", "requirement_id": "generic_data", "metric": "widget sales",
                         "value": "20", "unit": "USD million", "period": "2026-08", "scope": "US retail",
                         "group": "Survey A", "evidence_text": excerpt}]
        brief = build_research_brief("Widget sales", enriched, observations=observations, as_of="2026-10-14")
        self.assertEqual(len(brief["observations"]), 1, brief["warnings"])


if __name__ == "__main__":
    unittest.main()
