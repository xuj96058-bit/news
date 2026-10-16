import io
import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from src.chartbook import ChartbookStore, parse_chart_csv, chart_evidence
from src.research import prepare_evidence


def payload(**extra):
    return {"title": "公司盈利", "publisher": "Official", "source_url": "https://example.com/report",
            "metric": "Revenue", "unit": "USD million", "method": "source_data",
            "points": [{"date": "2026-01-01", "value": 12}, {"date": "2026-04-01", "value": 15}], **extra}


class ChartbookTests(unittest.TestCase):
    def test_csv_quotes_series_and_finite_values(self):
        rows = parse_chart_csv('date,value,series\n2026-01-01,12,"A, B"\n2026-04-01,15,"A, B"')
        self.assertEqual(rows[0]["series"], "A, B")
        for bad in ("NaN", "inf", "", "12%"):
            with self.assertRaises(ValueError):
                parse_chart_csv("date,value\n2026-01-01," + bad)
        with self.assertRaises(ValueError):
            parse_chart_csv("date,value\n2026-01-01,1\n2026-01-01,2")

    def test_persistence_provenance_and_report_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chart.db"
            with ChartbookStore(path) as store:
                chart = store.create_chart(payload())
                self.assertEqual(chart["observation_end"], "2026-04-01")
                self.assertEqual(chart["verification"], "unverified")
            with ChartbookStore(path) as store:
                chart = store.get_chart(chart["id"])
                evidence = chart_evidence(chart)
                self.assertIn("2026-04-01 | 數值 | 15 | USD million", evidence["content"])
                sources = prepare_evidence([], [evidence])
                self.assertEqual(sources[0]["source_type"], "manual")
                self.assertEqual(json.loads(store.export_json(chart["id"]))["content_hash"], chart["content_hash"])
                self.assertEqual(store.list_charts("盈利")["total"], 1)
                store.archive_chart(chart["id"])
                self.assertEqual(store.list_charts()["total"], 0)
                self.assertIsNotNone(store.get_chart(chart["id"]))

    def test_image_does_not_invent_numeric_evidence(self):
        image = io.BytesIO()
        Image.new("RGB", (20, 20), "white").save(image, "PNG")
        with ChartbookStore(":memory:") as store:
            chart = store.create_chart(payload(points=[], observation_start="2026-01-01", observation_end="2026-04-01"), image.getvalue())
            self.assertEqual(chart["kind"], "image")
            self.assertIn("未提取數值", chart_evidence(chart)["content"])
            self.assertEqual(store.get_chart(chart["id"], include_image=True)["image_bytes"], image.getvalue())
            with self.assertRaises(ValueError):
                store.create_chart(payload(points=[], observation_start="2026-01-01", observation_end="2026-04-01"), b'<svg></svg>')

    def test_invalid_provenance_and_period(self):
        with ChartbookStore(":memory:") as store:
            for values in ({"source_url": "javascript:alert(1)"}, {"source_url": "https://user:pass@example.com"},
                           {"unit": ""}, {"observation_start": "2025-01-01"}):
                with self.assertRaises(ValueError):
                    store.create_chart(payload(**values))

    def test_csv_rejects_ambiguous_columns_and_malformed_quotes(self):
        for text in ("date,value\n2026-01-01,1,000", "date,value,value\n2026-01-01,12,99",
                     "date,value,series\n2026-01-01,12", 'date,value\n2026-01-01,"12',
                     "date,value,extra\n2026-01-01,12,unused"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_chart_csv(text)
        for value in ("10000000000000001", "1e-400", "1.234567890123456789"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_chart_csv("date,value\n2026-01-01," + value)

    def test_numeric_evidence_preserves_precision_and_snapshot_hash(self):
        with ChartbookStore(":memory:") as store:
            chart = store.create_chart(payload(points=[
                {"date": "2026-01-01", "value": 1234567.89},
                {"date": "2026-02-01", "value": 0.00000123},
                {"date": "2026-03-01", "value": -9876543.21}],
                source_article_id="article-id", source_content_hash="a" * 64))
            source = prepare_evidence([], [chart_evidence(chart)])[0]
            for value in ("1234567.89", "0.00000123", "-9876543.21", chart["content_hash"], "a" * 64):
                self.assertIn(value, source["content"])
            self.assertIn("尚未人工核對", source["content"])

    def test_source_url_cannot_persist_or_export_credentials(self):
        with ChartbookStore(":memory:") as store:
            chart = store.create_chart(payload(source_url="https://example.com/report?id=12&token=secret-value#session-secret"))
            self.assertEqual(chart["source_url"], "https://example.com/report?id=12")
            self.assertNotIn("secret", store.export_json(chart["id"]))
            for url in ("https://@example.com/report", "https://example.com\\@evil.example/report",
                        'https://example.com/<script>'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    store.create_chart(payload(source_url=url))

    def test_truncated_and_animated_images_are_rejected(self):
        first = Image.new("RGB", (20, 20), "white")
        second = Image.new("RGB", (20, 20), "black")
        jpeg, animated = io.BytesIO(), io.BytesIO()
        first.save(jpeg, "JPEG")
        first.save(animated, "PNG", save_all=True, append_images=[second], duration=100, loop=0)
        with ChartbookStore(":memory:") as store:
            for data in (jpeg.getvalue()[:-2], animated.getvalue()):
                with self.subTest(length=len(data)), self.assertRaises(ValueError):
                    store.create_chart(payload(points=[], observation_start="2026-01-01", observation_end="2026-04-01"), data)

    def test_hash_changes_with_data_metadata_or_image(self):
        with ChartbookStore(":memory:") as store:
            first = store.create_chart(payload())
            same = store.create_chart(payload())
            changed = store.create_chart(payload(notes="Revised explanation"))
            self.assertEqual(first["content_hash"], same["content_hash"])
            self.assertNotEqual(first["content_hash"], changed["content_hash"])
            for kwargs in ({"limit": True}, {"offset": True}):
                with self.assertRaises(ValueError):
                    store.list_charts(**kwargs)


if __name__ == "__main__":
    unittest.main()
