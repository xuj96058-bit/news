"""Offline regressions for bounded original-document discovery and provenance."""
import copy
from hashlib import sha256
import json
import socket
import time
import unittest
from unittest.mock import Mock, patch

import requests

from src.research_discovery import supplement_sources
from src.sec_universe import SecConfigError


TOPIC = "AI capital expenditure cash flow"
REQUIREMENTS = [{"id": "R1", "question_id": "Q1", "description": "capital expenditure and cash flow",
                 "kind": "numeric", "critical": True}]


def source(**changes):
    return {"id": "S3", "title": "AI capital expenditure cash flow", "publisher": "Supplied source",
            "url": "https://www.microsoft.com/investor/report", "source_type": "manual",
            "content_scope": "provided_text", "content": "AI capital expenditure cash flow is estimated at 100.",
            "publication_date": "2026-10-14", "origins": [], "custom_lineage": {"saved": True}, **changes}


def page(*, publication="2026-10-10", text=None, links=""):
    metadata = f'<meta property="article:published_time" content="{publication}">' if publication else ""
    content = text or ("Capital expenditure and cash flow for AI operations total 200. "
                       "Cash flow is company-wide; the document does not isolate project returns. ")
    return (f"<html><head>{metadata}<title>AI capital expenditure cash flow report</title></head>"
            f"<body><main>{content}{links}</main></body></html>").encode()


class Response:
    def __init__(self, body=b"", *, status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = {"Content-Type": "text/html", **(headers or {})}
        self.history, self.url, self.closed = (), None, False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        lookup = patch("src.research_retrieval.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
        lookup.start()
        self.addCleanup(lookup.stop)

    def run_discovery(self, sources=None, **kwargs):
        return supplement_sources([source()] if sources is None else sources, topic=TOPIC,
                                  requirements=REQUIREMENTS, as_of="2026-10-14", **kwargs)

    def test_provided_text_is_preserved_and_new_document_gets_next_id(self):
        original = source()
        saved = copy.deepcopy(original)
        body = page()
        outcome = self.run_discovery([original], get=Mock(return_value=Response(body)))
        self.assertEqual(original, saved)
        self.assertEqual(outcome["sources"][0], saved)
        self.assertEqual(outcome["added_count"], 1)
        added = outcome["sources"][1]
        self.assertEqual(added["id"], "S4")
        self.assertEqual(added["publication_date"], "2026-10-10")
        self.assertEqual(added["retrieval"]["parent_source_ids"], ["S3"])
        self.assertEqual(added["retrieval"]["requirement_ids"], ["R1"])
        self.assertEqual(added["retrieval"]["document_sha256"], sha256(body).hexdigest())
        self.assertEqual(added["retrieval"]["as_of_status"], "eligible")
        self.assertEqual(added["verification_status"], "not_independently_verified")
        self.assertIn("200", added["content"])

    def test_unknown_original_date_does_not_inherit_republication_date(self):
        result = self.run_discovery(get=Mock(return_value=Response(page(publication=None))))
        added = result["sources"][-1]
        self.assertIsNone(added["publication_date"])
        self.assertIsNone(added["published_at"])
        self.assertEqual(added["retrieval"]["publication_date_basis"], "unknown")
        self.assertEqual(added["retrieval"]["as_of_status"], "publication_unknown")

    def test_manual_official_label_does_not_skip_reading_original_url(self):
        original = source()
        original.update(content_scope="official_evidence", source_type="official")
        result = self.run_discovery([original], get=Mock(return_value=Response(page())))
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(result["sources"][0], original)
        self.assertEqual(result["sources"][-1]["content_scope"], "html_excerpt")

    def test_later_publication_is_excluded(self):
        result = self.run_discovery(get=Mock(return_value=Response(page(publication="2026-10-15"))))
        self.assertEqual(result["sources"], [source()])
        self.assertEqual(result["added_count"], 0)
        self.assertEqual(result["outcomes"][0]["reason"], "published_after_as_of")

    def test_conflicting_dates_remain_unknown_and_any_later_date_excludes(self):
        body = page().replace(b"</head>", b'<meta itemprop="datePublished" content="2026-10-09"></head>')
        result = self.run_discovery(get=Mock(return_value=Response(body)))
        self.assertIsNone(result["sources"][-1]["publication_date"])
        body = body.replace(b"2026-10-09", b"2026-10-16")
        result = self.run_discovery(get=Mock(return_value=Response(body)))
        self.assertEqual(result["added_count"], 0)

    def test_json_ld_date_is_read_but_modified_date_is_not_publication(self):
        body = page(publication=None).replace(b"</head>", b'<script type="application/ld+json">'
                 b'{"@graph":[{"datePublished":"2026-10-11","dateModified":"2026-10-12"}]}</script></head>')
        result = self.run_discovery(get=Mock(return_value=Response(body)))
        self.assertEqual(result["sources"][-1]["publication_date"], "2026-10-11")
        self.assertEqual(result["sources"][-1]["retrieval"]["document_modified_date"], "2026-10-12")

    def test_later_revision_is_excluded_even_with_an_earlier_publication_date(self):
        body = page().replace(b"</head>", b'<meta itemprop="dateModified" content="2026-10-15"></head>')
        result = self.run_discovery(get=Mock(return_value=Response(body)))
        self.assertEqual(result["added_count"], 0)
        self.assertEqual(result["outcomes"][0]["reason"], "modified_after_as_of")

    def test_real_original_link_has_priority_over_summary_page(self):
        original = source(url="https://www.cnbc.com/ai", content=source()["content"] +
                          " Original document: https://investor.nvidia.com/financial-report.html")
        transport = Mock(return_value=Response(page()))
        result = self.run_discovery([original], get=transport, max_fetches=1)
        self.assertEqual(transport.call_args.args[0], "https://investor.nvidia.com/financial-report.html")
        self.assertEqual(result["outcomes"][0]["why"], "source_body_link")
        self.assertTrue(any(item.get("reason") == "fetch_budget" for item in result["outcomes"]))

    def test_page_links_follow_only_actual_relevant_primary_documents_one_level(self):
        original = source(url="https://www.cnbc.com/ai")
        bridge = page(links='<a href="https://www.microsoft.com/actual-report">Capital expenditure cash flow report</a>'
                      '<a href="https://www.microsoft.com/about">About us</a>'
                      '<a href="https://evil.example/report">Capital expenditure cash flow</a>')
        document = page(text="Capital expenditure cash flow official filing. " * 8,
                        links='<a href="/third-hop">Capital expenditure cash flow third hop</a>')
        transport = Mock(side_effect=[Response(bridge), Response(document)])
        result = self.run_discovery([original], get=transport)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(result["added_count"], 2)
        self.assertEqual(result["sources"][1]["source_type"], "manual")
        self.assertEqual(result["sources"][2]["source_type"], "official")
        self.assertEqual(result["sources"][2]["url"], "https://www.microsoft.com/actual-report")

    def test_attempted_urls_are_not_fetched_again(self):
        first = self.run_discovery(get=Mock(return_value=Response(page())))
        transport = Mock()
        second = self.run_discovery(first["sources"], get=transport, attempted_urls=first["attempted_urls"])
        transport.assert_not_called()
        self.assertEqual(second["sources"], first["sources"])

    def test_already_read_full_document_is_not_fetched_again(self):
        transport = Mock()
        result = self.run_discovery([source(content_scope="html_full_text")], get=transport)
        transport.assert_not_called()
        self.assertEqual(result["added_count"], 0)

    def test_duplicate_documents_do_not_count_as_independent_sources(self):
        original = source(content=source()["content"] + " https://investor.nvidia.com/report.html")
        result = self.run_discovery([original], get=Mock(return_value=Response(page())))
        self.assertEqual(result["added_count"], 1)
        self.assertTrue(any(item.get("reason") == "duplicate_document" for item in result["outcomes"]))

    def test_network_failure_is_not_reported_as_non_disclosure(self):
        result = self.run_discovery(get=Mock(side_effect=RuntimeError("secret response contents")))
        self.assertEqual(result["sources"], [source()])
        self.assertEqual(result["outcomes"][0]["reason"], "fetch_failed")
        self.assertNotIn("secret", repr(result))
        self.assertEqual(result["outcomes"][0]["requirement_ids"], ["R1"])

    def test_deadline_and_fetch_budget_prevent_requests(self):
        for options, reason in (({"max_fetches": 0}, "fetch_budget"), ({"deadline": time.monotonic() - 1}, "deadline")):
            with self.subTest(reason=reason):
                transport = Mock()
                result = self.run_discovery(get=transport, **options)
                transport.assert_not_called()
                self.assertEqual(result["outcomes"][0]["reason"], reason)

    def test_timeout_is_recorded_and_remaining_deadline_restricts_transport(self):
        transport = Mock(side_effect=requests.Timeout())
        result = self.run_discovery(get=transport, deadline=time.monotonic() + 1)
        self.assertEqual(result["outcomes"][0]["reason"], "timeout")
        self.assertLessEqual(max(transport.call_args.kwargs["timeout"]), 0.5)

    def test_private_redirect_and_credential_urls_never_reach_private_transport(self):
        transport = Mock(return_value=Response(status=302, headers={"Location": "https://127.0.0.1/private"}))
        result = self.run_discovery(get=transport)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(result["added_count"], 0)
        transport = Mock()
        result = self.run_discovery([source(url="https://www.microsoft.com/report?api_key=private-secret")], get=transport)
        transport.assert_not_called()
        self.assertNotIn("private-secret", repr(result["outcomes"]))

    def test_hong_kong_and_mainland_official_documents_are_supported(self):
        for url in ("https://www1.hkexnews.hk/listedco/listconews/sehk/2026/report.pdf",
                    "https://static.cninfo.com.cn/finalpage/2026-10-10/report.PDF"):
            with self.subTest(url=url):
                result = self.run_discovery([source(url=url)], get=Mock(return_value=Response(page())))
                self.assertEqual(result["added_count"], 1)
                self.assertEqual(result["sources"][-1]["source_type"], "official")

    def test_unrelated_policy_topic_does_not_load_financial_catalog_or_sec(self):
        transport = Mock()
        result = supplement_sources([], topic="Microsoft AI regulation consultation status", requirements=[
            {"id": "R2", "description": "AI regulation effective date and legal scope", "kind": "document"}],
            as_of="2026-10-14", get=transport)
        transport.assert_not_called()
        self.assertEqual(result["outcomes"][0]["reason"], "no_known_public_links")

    def test_sec_filing_links_are_derived_only_from_valid_api_metadata(self):
        directory = {"cik": 789019, "filings": {"recent": {
            "form": ["10-Q", "10-Q", "8-K"], "filingDate": ["2026-10-15", "2026-07-30", "2026-07-28"],
            "accessionNumber": ["0000789019-26-000020", "0000789019-26-000019", "0000789019-26-000018"],
            "primaryDocument": ["future.htm", "msft10q.htm", "msft8k.htm"]}}}
        transport = Mock(side_effect=[Response(json.dumps(directory).encode(), headers={"Content-Type": "application/json"}),
                                     Response(page(publication=None))])
        with patch("src.research_discovery.sec_user_agent", return_value="News research contact@example.org"):
            result = supplement_sources([], topic="Microsoft capital expenditure cash flow", requirements=REQUIREMENTS,
                                        as_of="2026-10-14", get=transport, max_fetches=2)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(transport.call_args_list[0].args[0], "https://data.sec.gov/submissions/CIK0000789019.json")
        self.assertEqual(transport.call_args_list[1].args[0], "https://www.sec.gov/Archives/edgar/data/789019/000078901926000019/msft10q.htm")
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(result["sources"][0]["publication_date"], "2026-07-30")
        self.assertEqual(result["sources"][0]["retrieval"]["publication_date_basis"], "sec_filing_metadata")

    def test_sec_contact_missing_is_explicit_without_network(self):
        transport = Mock()
        with patch("src.research_discovery.sec_user_agent", side_effect=SecConfigError("contact needed")):
            result = supplement_sources([], topic="Microsoft capital expenditure cash flow", requirements=REQUIREMENTS,
                                        as_of="2026-10-14", get=transport)
        transport.assert_not_called()
        self.assertEqual(result["outcomes"][0]["reason"], "sec_contact_not_configured")

    def test_invalid_sec_filename_cannot_create_a_url(self):
        directory = {"cik": 789019, "filings": {"recent": {"form": ["10-Q"], "filingDate": ["2026-07-30"],
            "accessionNumber": ["0000789019-26-000019"], "primaryDocument": ["../../secret.htm"]}}}
        transport = Mock(return_value=Response(json.dumps(directory).encode()))
        with patch("src.research_discovery.sec_user_agent", return_value="News research contact@example.org"):
            result = supplement_sources([], topic="Microsoft capital expenditure cash flow", requirements=REQUIREMENTS,
                                        as_of="2026-10-14", get=transport)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(result["added_count"], 0)
        self.assertEqual(result["outcomes"][0]["reason"], "no_eligible_recent_filings")

    def test_no_requirement_means_no_discovery_and_invalid_budget_is_rejected(self):
        transport = Mock()
        supplement_sources([source()], topic=TOPIC, requirements=[], as_of="2026-10-14", get=transport)
        transport.assert_not_called()
        for value in (-1, 13, True, 1.5):
            with self.assertRaises(ValueError):
                self.run_discovery(max_fetches=value)


if __name__ == "__main__":
    unittest.main()
