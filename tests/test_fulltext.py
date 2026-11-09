"""Tests for optional full-text enrichment."""

import os
import unittest
from unittest.mock import patch

from src.fulltext import enrich_item, fulltext_enabled


class FulltextTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(fulltext_enabled())
            item = {"url": "https://example.com/a", "content": "short", "summary": "s"}
            self.assertEqual(enrich_item(item), item)

    def test_skips_when_content_already_long(self):
        with patch.dict(os.environ, {"NEWS_FETCH_FULLTEXT": "1"}):
            item = {"url": "https://example.com/a", "content": "x" * 500, "summary": "s"}
            self.assertEqual(enrich_item(item), item)

    @patch("src.fulltext.extract_fulltext", return_value="Extracted full article text that is long enough to be useful.")
    def test_enriches_when_enabled(self, _mock):
        with patch.dict(os.environ, {"NEWS_FETCH_FULLTEXT": "1"}):
            item = {"url": "https://example.com/a", "content": "", "summary": "only summary", "title": "T"}
            result = enrich_item(item)
            self.assertEqual(result["content_scope"], "fulltext_trafilatura")
            self.assertIn("Extracted full article", result["content"])


if __name__ == "__main__":
    unittest.main()
