import unittest

from src.notification_language import prepare_payload, traditional_text


class NotificationLanguageTests(unittest.TestCase):
    def test_traditional_news_keeps_financial_numbers_and_symbols(self):
        result = traditional_text("美国经济数据：营收增长 3.5%，NVDA $150；Fed holds rates.")
        self.assertIn("美國經濟數據", result)
        self.assertIn("營收增長 3.5%", result)
        self.assertIn("NVDA $150；Fed holds rates.", result)

    def test_converts_text_and_attachment_source_without_mutating_input(self):
        original = {"title": "机构观点", "text": "市场发展", "summary": "业绩预期",
                    "url": "https://example.com/新闻?q=经济", "content_scope": "partial_text"}
        result = prepare_payload(original)
        self.assertEqual(result["title"], "機構觀點")
        self.assertEqual(result["text"], "市場發展")
        self.assertEqual(result["summary"], "業績預期")
        self.assertEqual(original["text"], "市场发展")
        self.assertEqual(result["url"], original["url"])
        self.assertEqual(result["content_scope"], "partial_text")

    def test_embedded_url_is_not_rewritten(self):
        text = "详情 https://example.com/新闻?q=经济 阅读原文"
        self.assertEqual(traditional_text(text), "詳情 https://example.com/新闻?q=经济 閱讀原文")


if __name__ == "__main__":
    unittest.main()
