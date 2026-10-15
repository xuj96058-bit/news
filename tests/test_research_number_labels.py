"""Index-label digits are not evidence for independent numerical assertions."""
from decimal import Decimal
import unittest

from src.argumentative_workflow import _checked_essay
from src.research import _number_values
from test_argumentation import essay


class NumberLabelTests(unittest.TestCase):
    def test_complete_known_labels_translate_without_quantitative_claims(self):
        for text in ("S&P500", "S&P 500 Index", "Standard & Poor's 500",
                     "標普500指數", "标普 500", "標準普爾500", "标准普尔500",
                     "Russell 1000", "羅素1000指數", "罗素1000", "Ｓ＆Ｐ５００"):
            with self.subTest(text=text):
                self.assertEqual(_number_values(text), set())
        self.assertEqual(_number_values("標普500年初至今增長20%。"), {Decimal("20")})

    def test_name_does_not_support_separate_percentage_or_price(self):
        source_numbers = _number_values("S&P500 earnings improved.")
        for claim in ("盈利500%", "價格500", "標普500盈利增長500%", "S&P500 price 500 USD"):
            with self.subTest(claim=claim):
                self.assertIn(Decimal("500"), _number_values(claim))
                self.assertFalse(_number_values(claim).issubset(source_numbers))
        self.assertEqual(_number_values("S&P500盈利500%，價格500美元。"), {Decimal("500")})

    def test_unknown_or_extended_labels_retain_whole_number(self):
        for text, number in (("S&P5000", "5000"), ("標普5000", "5000"),
                             ("Russell1001", "1001"), ("未知500", "500"),
                             ("ABC500", "500"), ("S&P50", "50")):
            with self.subTest(text=text):
                self.assertEqual(_number_values(text), {Decimal(number)})

    def test_decimal_thousands_percentage_and_quantity_suffix_are_not_masked(self):
        cases = [("S&P500.25", "500.25"), ("標普500,000", "500000"),
                 ("S&P500%", "500"), ("標普５００％", "500"),
                 ("標普500倍", "500"), ("S&P500 USD", "500"),
                 ("S&P500 million", "500"), ("標普500美元", "500"),
                 ("標普500點", "500"), ("S&P500 bps", "500"),
                 ("標普500家公司", "500"), ("標普500億元", "500")]
        for text, number in cases:
            with self.subTest(text=text):
                self.assertEqual(_number_values(text), {Decimal(number)})

    def test_punctuation_is_not_confused_with_numeric_extension(self):
        self.assertEqual(_number_values("S&P500, earnings rose 20%."), {Decimal("20")})
        self.assertEqual(_number_values("標普500。盈利增長20%。"), {Decimal("20")})
        self.assertEqual(_number_values("S&P500. Growth was 20%."), {Decimal("20")})

    def test_comma_delimited_numbers_survive_without_splitting_thousands(self):
        self.assertEqual(_number_values("資本開支，2026至2028年占85%至90%。"),
                         {Decimal("2026"), Decimal("2028"), Decimal("85"), Decimal("90")})
        self.assertEqual(_number_values("價格,500元；成本，1,500.25美元。"),
                         {Decimal("500"), Decimal("1500.25")})
        self.assertEqual(_number_values("ABC1,234,567.89"), {Decimal("1234567.89")})
        self.assertTrue(self.check_paragraph("標普500，盈利500%。", "S&P500 earnings improved."))

    def test_dates_and_month_translation_remain_traceable(self):
        self.assertEqual(_number_values("2028年資本開支"), {Decimal("2028")})
        self.assertFalse(_number_values("2028年資本開支").issubset(_number_values("2026年S&P500")))
        self.assertIn(Decimal("10"), _number_values("October 2026"))

    def check_paragraph(self, claim, evidence):
        draft = essay()
        draft["arguments"][0]["evidence"][0] = {"text": claim, "source_ids": ["S1"]}
        sources = [{"id": "S1", "content": evidence}, {"id": "S2", "content": "Costs constrained margins."}]
        _, warnings = _checked_essay(draft, sources, None)
        return [row for row in warnings if row.get("code") == "untraceable_numbers"
                and row.get("section") == "arguments.1.evidence" and row.get("paragraph") == 1]

    def test_paragraph_check_accepts_name_translation_but_rejects_laundered_value(self):
        self.assertEqual(self.check_paragraph("標普500盈利增長20%。", "S&P500 earnings grew 20%."), [])
        self.assertEqual(self.check_paragraph("S&P500 earnings grew 20%.", "標普500盈利增長20%。"), [])
        for claim in ("盈利500%", "價格500美元", "標普500盈利增長500%", "2028年資本開支上升"):
            with self.subTest(claim=claim):
                self.assertTrue(self.check_paragraph(claim, "S&P500 earnings grew 20% in 2026."))


if __name__ == "__main__":
    unittest.main()
