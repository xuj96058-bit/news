import json
import unittest
from decimal import Decimal

from src.chartbook import ChartbookStore, chart_evidence
from src.research import (_number_values, _validated_sources, format_sources_appendix,
                          generate_research_report, prepare_evidence, prepare_sources)
from src.research_store import ResearchStore
from tests.test_chartbook import payload as chart_payload
from tests.test_research import article, fake_client, payload as report_payload


class ChartReportProvenanceTests(unittest.TestCase):
    def test_chart_and_linked_source_hash_survive_validation_report_and_persistence(self):
        with ChartbookStore(":memory:") as charts, ResearchStore(":memory:") as reports:
            chart = charts.create_chart(chart_payload(source_article_id="b" * 64, source_content_hash="a" * 64))
            evidence = chart_evidence(chart)
            sources = prepare_evidence([], [evidence])
            checked = _validated_sources(sources)[0]
            for key in ("chart_id", "chart_content_hash", "source_article_id", "source_content_hash"):
                self.assertEqual(checked[key], evidence[key])
            self.assertIn(chart["content_hash"], format_sources_appendix(sources))
            client, _ = fake_client(report_payload(executive_findings="The chart records 12 units. [S1]"))
            generated = generate_research_report("Earnings", sources, api_key="synthetic-key",
                                                as_of="2026-10-16", client=client)
            saved = reports.create_report(topic="Earnings", sources=sources, provider="deepseek",
                                          model="deepseek-chat", as_of="2026-10-16", **generated)
            self.assertEqual(saved["sources"][0]["chart_content_hash"], chart["content_hash"])
            self.assertEqual(saved["sources"][0]["source_content_hash"], "a" * 64)
            self.assertIn(chart["content_hash"], saved["body"])

    def test_provenance_allowlist_never_forwards_secrets_or_nonhash_objects(self):
        evidence = {"title": "Chart", "content": "Measured value was 12.", "source_type": "manual",
                    "chart_id": "ift__synthetic_sensitive_value", "chart_content_hash": {"token": "secret"},
                    "source_content_hash": "not-a-hash", "source_article_id": "safe-article-id",
                    "sessionId": "secret", "token": "secret", "image_sha256": "B" * 64}
        sources = prepare_evidence([], [evidence])
        self.assertNotIn("secret", json.dumps(sources))
        self.assertNotIn("sensitive", json.dumps(sources))
        self.assertNotIn("chart_id", sources[0])
        self.assertNotIn("chart_content_hash", sources[0])
        self.assertEqual(sources[0]["image_sha256"], "b" * 64)
        # Existing snapshots pass through the same allowlist on later use.
        sources[0]["source_content_hash"] = {"token": "secret"}
        self.assertNotIn("source_content_hash", _validated_sources(sources)[0])
        news = prepare_sources([article(content_hash="c" * 64)])
        self.assertEqual(news[0]["article_content_hash"], "c" * 64)

    def test_hashes_ids_and_url_digits_are_not_numeric_observations(self):
        text = ("Measured revenue was 12.5.\n"
                "圖表識別碼：730002aaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                "圖表快照 SHA-256：" + "9876543210" * 6 + "1234\n"
                "關聯文章快照 SHA-256：" + "abcdef7654321" * 5 + "\n"
                "來源：https://example.com/730003?revision=730004\n"
                "UUID: 12345678-1234-1234-1234-123456789012\n"
                "Hash: 730005abcdefabcd730005abcdefabcd730005abcdefabcd730005abcdefabcd\n"
                "Identifier: Article730006\n")
        self.assertEqual(_number_values(text), {Decimal("12.5")})

    def test_url_or_hash_match_cannot_suppress_unsupported_number_warning(self):
        evidence = {"title": "Source data", "source_type": "manual", "content":
                    "Measured revenue was 12 units.\n"
                    "圖表識別碼：730002aaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                    "來源：https://example.com/730002\n"}
        client, _ = fake_client(report_payload(executive_findings="Revenue was 730002 units. [S1]"))
        report = generate_research_report("Revenue", prepare_evidence([], [evidence]), api_key="synthetic-key",
                                         as_of="2026-10-16", client=client)
        self.assertTrue(report["metadata"]["review_warnings"])


if __name__ == "__main__":
    unittest.main()
