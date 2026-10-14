import importlib.util
from html.parser import HTMLParser
from io import BytesIO
import unittest
from unittest.mock import patch

from src.research_export import ResearchExportError, export_html, export_markdown, export_pdf


class ParsedHTML(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.links = []
        self.text = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs))

    def handle_data(self, data):
        self.text.append(data)


def edited_report():
    return {
        "title": "半導體研究報告",
        "version": 3,
        "as_of": "2026-10-14 10:30 HKT",
        "topic": "台灣半導體",
        "body": (
            "## 使用者修訂觀點\n\n"
            "EDITED-ONLY：**毛利率展望仍需驗證**，保留保守情境。[S1]\n\n"
            "- 追蹤資本支出\n- 注意供應鏈風險\n\n"
            "| 情境 | 觀察 |\n| --- | --- |\n| 基準 | 需求持平 |\n"
            "| 風險 | 需求放緩 |\n\n"
            "## 來源附錄\n\n"
            "[S1] [來源一](https://example.org/research?x=1&y=2)\n"
        ),
        "sources": [{"title": "SHOULD-NOT-REGENERATE", "url": "https://example.org/new-source"}],
        "provider": "unused-provider",
        "model": "unused-model",
        "api_key": "sk-private-metadata-only",
        "config": {"token": "nested-private-metadata-only"},
    }


def generated_appendix_report():
    # Exercise the real generator's local source preparation and appendix format,
    # without constructing a model client or reading any environment credentials.
    with patch("dotenv.load_dotenv"):
        from src.research import format_sources_appendix, prepare_sources
    sources = prepare_sources([{
        "id": 17, "source_id": "test-rss", "title": "來源研究",
        "source_name": "Test publisher", "summary": "供應鏈展望仍需核對。",
        "content_scope": "summary", "url": "https://example.org/source?view=full",
        "sources": [{"source_id": "test-mirror", "title": "來源研究",
                     "source_name": "Test mirror", "url": "https://mirror.example.org/source"}],
    }])
    return {"title": "來源附錄匯出測試", "body": format_sources_appendix(sources), "sources": sources}


def raw_fallback_report():
    """Use the actual workflow's inert raw-draft wrapper, with no model call."""
    from src.research_workflow import _body, _warning
    source_report = generated_appendix_report()
    raw = (
        '[unprovided Markdown](https://unprovided.example/markdown)\n'
        '\n'
        '![unprovided image](https://unprovided.example/image.png)\n'
        '<a href="https://unprovided.example/html">raw HTML link</a>\n'
        '<img src="https://unprovided.example/html-image.png" onerror="alert(1)">\n'
        '<script>alert("raw fallback")</script>\n'
        '```\n'
        '[after closing fence](https://unprovided.example/after-fence)\n'
        '```\n'
        '<https://unprovided.example/autolink>\n'
        'https://unprovided.example/plain-url\n'
    )
    return {'title': '原始模型草稿', 'version': 2,
            'metadata': {'review_status': 'review_failed'}, 'sources': source_report['sources'],
            'body': _body('原始模型草稿', None, source_report['sources'], '2026-10-14',
                          stage='review_failed', warnings=[_warning('invalid_schema')], raw=raw)}, raw


class ResearchExportTests(unittest.TestCase):
    def test_gap_mode_label_survives_manual_title_and_body_replacement(self):
        report = edited_report()
        report['metadata'] = {'report_mode': 'gap', 'review_status': 'needs_review', 'quality_status': 'evidence_gap'}
        self.assertIn('報告類型：資料缺口簡報', export_markdown(report).decode('utf-8'))
        self.assertIn('報告類型：資料缺口簡報', export_html(report).decode('utf-8'))

    def test_raw_fallback_indented_markdown_and_fences_remain_inert_in_html(self):
        report, raw = raw_fallback_report()
        exported = export_html(report).decode('utf-8')
        parsed = ParsedHTML(exported)
        self.assertIn('pre', parsed.tags)
        self.assertIn('code', parsed.tags)
        self.assertNotIn('script', parsed.tags)
        self.assertNotIn('img', parsed.tags)
        destinations = {link['href'] for link in parsed.links}
        self.assertEqual(destinations, {'https://example.org/source?view=full',
                                       'https://mirror.example.org/source'})
        text = ''.join(parsed.text)
        for line in raw.splitlines():
            if line.strip():
                self.assertIn(line, text)

    def test_exports_label_review_state_and_later_manual_edits_without_blocking(self):
        report = edited_report()
        report['metadata'] = {'review_status': 'review_failed'}
        self.assertIn('審稿未完成，初稿保留', export_markdown(report).decode('utf-8'))
        self.assertIn('審稿未完成，初稿保留', export_html(report).decode('utf-8'))
        report['metadata'] = {'review_status': 'reviewed', 'reviewed_version': 2}
        self.assertIn('人工修改後待核對（AI審稿基於v2）', export_markdown(report).decode('utf-8'))
        self.assertIn('人工修改後待核對（AI審稿基於v2）', export_html(report).decode('utf-8'))

    def test_markdown_preserves_the_latest_body_and_only_display_metadata(self):
        report = edited_report()
        exported = export_markdown(report).decode("utf-8-sig")
        self.assertTrue(exported.endswith(report["body"]))
        self.assertIn("版本：3", exported)
        self.assertIn("資料截至：2026-10-14 10:30 HKT", exported)
        self.assertEqual(exported.count("來源附錄"), 1)
        for excluded in ("SHOULD-NOT-REGENERATE", "sk-private", "nested-private", "unused-provider", "unused-model"):
            self.assertNotIn(excluded, exported)

    def test_html_supports_reading_structure_and_source_links(self):
        report = edited_report()
        exported = export_html(report).decode("utf-8")
        parsed = ParsedHTML(exported)
        self.assertIn("EDITED-ONLY", "".join(parsed.text))
        for tag in ("h2", "b", "ul", "li", "table", "thead", "tbody", "th", "td"):
            self.assertIn(tag, parsed.tags)
        self.assertEqual(parsed.links[0]["href"], "https://example.org/research?x=1&y=2")
        self.assertEqual(exported.count("來源附錄"), 1)
        self.assertNotIn("SHOULD-NOT-REGENERATE", exported)
        self.assertNotIn("sk-private", exported)
        self.assertNotIn("nested-private", exported)

    def test_html_escapes_raw_markup_and_disables_unsafe_links_and_images(self):
        report = {
            "title": '<script>alert("title")</script>',
            "body": (
                '<script>alert("body")</script>\n\n'
                '<img src=x onerror=alert(1)>\n\n'
                '[unsafe](javascript:alert(1)) [data](data:text/html,evil) '
                '[file](file:///C:/secret.txt) [credential](https://u:secret@example.org)\n\n'
                '![remote picture](https://example.org/tracker.png)\n\n'
                '[safe](https://example.org/path) '
                '[attribute](https://example.org/\"onclick=\"evil)\n'
            ),
        }
        exported = export_html(report).decode("utf-8")
        parsed = ParsedHTML(exported)
        self.assertNotIn("script", parsed.tags)
        self.assertNotIn("img", parsed.tags)
        self.assertNotIn("iframe", parsed.tags)
        self.assertIn('<script>alert("body")</script>', "".join(parsed.text))
        self.assertIn("remote picture", "".join(parsed.text))
        self.assertTrue(parsed.links)
        self.assertTrue(all(link["href"].startswith("https://") for link in parsed.links))
        self.assertTrue(all("onclick" not in link for link in parsed.links))
        self.assertTrue(all("secret" not in link["href"] for link in parsed.links))
        self.assertIn("Content-Security-Policy", exported)

    def test_generator_appendix_source_links_remain_live_in_html(self):
        report = generated_appendix_report()
        self.assertIn("[原始連結](<https://example.org/source?view=full>)", report["body"])
        parsed = ParsedHTML(export_html(report).decode("utf-8"))
        destinations = {link["href"] for link in parsed.links}
        self.assertIn("https://example.org/source?view=full", destinations)
        self.assertIn("https://mirror.example.org/source", destinations)

    def test_angle_wrapped_links_still_reject_unsafe_destinations(self):
        report = {"body": "[safe](<https://example.org/source>) "
                          "[credential](<https://user:secret@example.org/source>) "
                          "[script](<javascript:alert>) "
                          "[nested](<<https://example.org/nested>>)", "title": "安全連結"}
        parsed = ParsedHTML(export_html(report).decode("utf-8"))
        self.assertEqual([link["href"] for link in parsed.links], ["https://example.org/source"])
        self.assertIn("credential", "".join(parsed.text))
        self.assertIn("script", "".join(parsed.text))

    def test_incomplete_edited_markdown_does_not_break_text_exports(self):
        report = {"title": "修訂草稿", "body": "**未關閉\n[未完成](javascript:\n\n```python\n<draft> & 中文"}
        self.assertIn("未完成", export_markdown(report).decode("utf-8"))
        parsed = ParsedHTML(export_html(report).decode("utf-8"))
        self.assertIn("<draft> & 中文", "".join(parsed.text))
        self.assertNotIn("draft", parsed.tags)

    def test_non_text_body_returns_a_safe_error(self):
        for export in (export_markdown, export_html, export_pdf):
            with self.subTest(export=export.__name__), self.assertRaises(ResearchExportError):
                export({"body": {"api_key": "secret"}})

    def test_missing_pdf_library_is_a_safe_actionable_error(self):
        real_import = __import__

        def missing_reportlab(name, *args, **kwargs):
            if name.startswith("reportlab"):
                raise ImportError("private environment or credential details")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing_reportlab):
            with self.assertRaisesRegex(ResearchExportError, "reportlab>=4.2.0") as caught:
                export_pdf(edited_report())
        self.assertNotIn("private", str(caught.exception))


@unittest.skipUnless(importlib.util.find_spec("reportlab") and importlib.util.find_spec("pypdf"),
                     "PDF verification requires reportlab and pypdf")
class ResearchPDFTests(unittest.TestCase):
    def reader(self, report):
        from pypdf import PdfReader
        data = export_pdf(report)
        self.assertTrue(data.startswith(b"%PDF-"))
        return PdfReader(BytesIO(data))

    def test_raw_fallback_indented_markdown_has_no_unprovided_pdf_link_annotations(self):
        report, raw = raw_fallback_report()
        reader = self.reader(report)
        destinations = {annotation.get_object()['/A']['/URI']
                        for page in reader.pages for annotation in page.get('/Annots', [])
                        if annotation.get_object().get('/A', {}).get('/S') == '/URI'}
        self.assertEqual(destinations, {'https://example.org/source?view=full',
                                       'https://mirror.example.org/source'})
        text = '\n'.join(page.extract_text() for page in reader.pages)
        self.assertIn('[unprovided Markdown](https://unprovided.example/markdown)', text)
        self.assertIn('https://unprovided.example/after-fence', text)
        self.assertIn('<script>alert("raw fallback")</script>', text)
        self.assertIn('審稿未完成，初稿保留', text)

    def test_pdf_has_editable_body_chinese_sources_and_no_credential_metadata(self):
        reader = self.reader(edited_report())
        self.assertGreaterEqual(len(reader.pages), 1)
        text = "\n".join(page.extract_text() for page in reader.pages)
        self.assertIn("EDITED-ONLY", text)
        self.assertIn("使用者修訂觀點", text)
        self.assertIn("毛利率展望仍需驗證", text)
        self.assertIn("供應鏈風險", text)
        self.assertIn("需求放緩", text)
        self.assertEqual(text.count("來源附錄"), 1)
        self.assertIn("第 1 頁", text)
        self.assertIn("版本：3", text)
        serialized_metadata = str(reader.metadata)
        self.assertNotIn("sk-private", serialized_metadata)
        self.assertNotIn("nested-private", serialized_metadata)
        self.assertNotIn("unused-model", serialized_metadata)
        self.assertNotIn("SHOULD-NOT-REGENERATE", text)

    def test_generator_appendix_source_links_remain_live_in_pdf(self):
        reader = self.reader(generated_appendix_report())
        destinations = {annotation.get_object()["/A"]["/URI"]
                        for page in reader.pages for annotation in page.get("/Annots", [])
                        if annotation.get_object().get("/A", {}).get("/S") == "/URI"}
        self.assertIn("https://example.org/source?view=full", destinations)
        self.assertIn("https://mirror.example.org/source", destinations)

    def test_pdf_pages_long_edited_tables_and_incomplete_markdown(self):
        report = {"title": "跨頁測試", "version": 7,
                  "body": "## 長表格\n\n| 觀察 | 備註 |\n| --- | --- |\n"
                          + "| 中文長段落 " + "需求仍待驗證。" * 450 + " | 大型編輯儲存格 |\n"
                          + "\n## 修訂草稿\n\n" + ("未關閉的 **標記 <script> 仍為文字。\n\n" * 70)}
        reader = self.reader(report)
        self.assertGreater(len(reader.pages), 1)
        texts = [page.extract_text() for page in reader.pages]
        self.assertIn("大型編輯儲存格", "\n".join(texts))
        self.assertIn("修訂草稿", "\n".join(texts))
        for index, text in enumerate(texts, 1):
            self.assertIn(f"第 {index} 頁", text)

    def test_portable_pdf_fallback_can_export_traditional_chinese(self):
        from reportlab.pdfbase import pdfmetrics
        # Force the local-font lookup down the portable CID path, independently
        # of fonts already registered by another test.
        with patch("src.research_export.Path.is_file", return_value=False), \
             patch.object(pdfmetrics, "getRegisteredFontNames", return_value=[]):
            reader = self.reader({"title": "繁體中文研究", "body": "使用者修訂：供應鏈風險與需求展望。"})
        text = "\n".join(page.extract_text() for page in reader.pages)
        self.assertIn("繁體中文研究", text)
        self.assertIn("供應鏈風險與需求展望", text)
        cid_fonts = [font.get_object() for font in reader.pages[0]["/Resources"]["/Font"].values()
                     if font.get_object().get("/Subtype") == "/Type0"]
        self.assertTrue(cid_fonts)
        self.assertTrue(all(font["/Encoding"] == "/UniCNS-UCS2-H" for font in cid_fonts))


if __name__ == "__main__":
    unittest.main()
