"""Canonical-table extraction never relaxes the existing observation validators."""
import copy
import unittest

from src.research_quality import build_research_brief
from src.research_tables import extract_table_observations


HEADER = ("FINRA Statistics1 (shown in $ millions)\n"
          "| Month/Year | Debit Balances in Customers' Securities Margin Accounts "
          "| Free Credit Balances in Customers' Cash Accounts "
          "| Free Credit Balances in Customers' Securities Margin Accounts\n")
TABLE = HEADER + "| Aug-26 | 1,453,832 | 207,641 | 217,499\n| Jul-26 | 1,417,225 | 205,132 | 217,305\n"


def source(content=TABLE, **updates):
    return {"id": "S1", "title": "FINRA margin statistics", "publisher": "FINRA",
            "url": "https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics",
            "source_type": "official", "content_scope": "official_evidence",
            "content": content, "observation_date": "2026-08-31", **updates}


class TableObservationTests(unittest.TestCase):
    def test_official_august_and_july_rows_validate_and_compare_without_ai(self):
        sources = [source("Introductory text.\n" + TABLE + "Historical Data\n")]
        original = copy.deepcopy(sources)
        observations = extract_table_observations(sources, "2026-10-14")
        self.assertEqual([(row["period"], row["value"]) for row in observations],
                         [("2026-08", "1453832"), ("2026-07", "1417225")])
        self.assertTrue(all(row["unit"] == "$ millions" for row in observations))
        self.assertTrue(all(row["evidence_text"] in sources[0]["content"] for row in observations))
        self.assertEqual(sources, original)
        brief = build_research_brief("Crowding and leverage", sources, observations=observations, as_of="2026-10-14")
        self.assertEqual(brief["warnings"], [])
        self.assertEqual(next(req for req in brief["requirements"] if req["id"] == "margin")["status"], "fulfilled")
        self.assertEqual(brief["comparisons"][0]["change"], "36607")
        self.assertEqual(brief["comparisons"][0]["from_period"], "2026-07")
        self.assertEqual(brief["comparisons"][0]["to_period"], "2026-08")
        self.assertFalse(brief["ready"])  # Margin totals never prove sector/fund concentration.

    def test_only_the_canonical_debit_column_is_selected(self):
        observations = extract_table_observations([source(HEADER + "| Aug-26 | 20 | 999 | 888\n")], "2026-10-14")
        self.assertEqual([row["value"] for row in observations], ["20"])
        self.assertNotIn("999", [row["value"] for row in observations])

    def test_untrusted_hosts_and_nonofficial_labels_are_rejected(self):
        for changes in ({"url": "https://www.finra.org.evil.example/data"},
                        {"url": "https://user:pass@www.finra.org/data"},
                        {"url": "http://www.finra.org/data"},
                        {"url": "https://www.finra.org:8443/data"},
                        {"source_type": "manual"}, {"source_type": "rss"}):
            with self.subTest(changes=changes):
                self.assertEqual(extract_table_observations([source(**changes)], "2026-10-14"), [])

    def test_changed_unit_or_column_order_is_not_assumed_equivalent(self):
        for content in (TABLE.replace("$ millions", "$ billions"),
                        TABLE.replace("Month/Year", "Publication date"),
                        TABLE.replace("Customers' Cash Accounts", "Institutional Cash Accounts"),
                        TABLE.replace("Debit Balances", "Net Balances")):
            self.assertEqual(extract_table_observations([source(content)], "2026-10-14"), [])

    def test_incomplete_rows_malformed_commas_and_unknown_months_are_rejected(self):
        for line in ("| Aug-26 | 100 | 20\n", "| Aug-26 | 1,45,832 | 20 | 30\n",
                     "| Xxx-26 | 100 | 20 | 30\n", "| Aug-26 | unavailable | 20 | 30\n"):
            self.assertEqual(extract_table_observations([source(HEADER + line)], "2026-10-14"), [])

    def test_future_months_are_not_observations_and_july_does_not_use_august_date(self):
        sources = [source()]
        observations = extract_table_observations(sources, "2026-08-15")
        self.assertEqual([row["period"] for row in observations], ["2026-07"])
        self.assertIn("Jul-26", observations[0]["evidence_text"])
        checked = build_research_brief("Crowding and leverage", sources, observations=observations, as_of="2026-08-15")
        self.assertEqual(len(checked["observations"]), 1)

    def test_multiple_tables_are_ambiguous_and_not_merged(self):
        self.assertEqual(extract_table_observations([source(TABLE + "\n" + TABLE)], "2026-10-14"), [])

    def test_exact_table_date_alias_does_not_allow_swapping_august_and_july_values(self):
        sources = [source()]
        observations = extract_table_observations(sources, "2026-10-14")
        observations[1]["value"] = "1453832"
        brief = build_research_brief("Crowding and leverage", sources, observations=observations, as_of="2026-10-14")
        self.assertEqual(len(brief["observations"]), 1)
        self.assertIn("ambiguous_period_value", [warning["code"] for warning in brief["warnings"]])

    def test_invalid_cutoff_missing_source_id_and_nonlist_inputs_return_no_rows(self):
        self.assertEqual(extract_table_observations([source()], None), [])
        self.assertEqual(extract_table_observations([source()], "invalid"), [])
        self.assertEqual(extract_table_observations([source(id=None)], "2026-10-14"), [])
        self.assertEqual(extract_table_observations({}, "2026-10-14"), [])


if __name__ == "__main__":
    unittest.main()
