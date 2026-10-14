"""Research planning tests use only synthetic source excerpts."""
import copy
import json
import unittest

from src.research_quality import build_research_brief, rank_articles


AS_OF = "2026-10-14"


def source(content, identifier="S1", **extra):
    return {"id": identifier, "title": "Synthetic table", "content": content, **extra}


def row(**extra):
    return {"source_id": "S1", "requirement_id": "generic_data", "metric": "widget sales",
            "value": "20", "unit": "USD million", "period": "2026-08", "scope": "US retail",
            "evidence_text": "Survey A: US retail widget sales in 2026-08 were 20 USD million.",
            "group": "Survey A", **extra}


def brief(rows, sources=None, topic="Widget sales", **kwargs):
    sources = sources or [source("\n".join(item["evidence_text"] for item in rows))]
    return build_research_brief(topic, sources, observations=rows, as_of=kwargs.get("as_of", AS_OF))


class RankingTests(unittest.TestCase):
    def test_unrelated_excluded_and_original_articles_unchanged(self):
        articles = [{"id": "1", "title": "Local weather"}, {"id": "2", "title": "AI earnings report"},
                    {"id": "3", "summary": "Artificial intelligence company profits"}]
        original = copy.deepcopy(articles)
        ranked = rank_articles(articles, "AI earnings")
        self.assertEqual([item["article"]["id"] for item in ranked], ["2", "3"])
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        self.assertEqual(articles, original)
        self.assertIs(ranked[0]["article"], articles[1])

    def test_ai_does_not_match_english_substrings(self):
        self.assertEqual(rank_articles([{"title": "He said the retail chair was repaired"}], "AI"), [])

    def test_traditional_simplified_and_english_concepts_match(self):
        articles = [{"title": "行業擁擠交易與槓桿持倉"}, {"title": "Sector crowding and leveraged holdings"}]
        self.assertEqual(len(rank_articles(articles, "行业拥挤交易与杠杆")), 2)
        self.assertEqual(len(rank_articles(articles, "行業擁擠與槓桿")), 2)

    def test_keyword_supplement_and_stable_zero_score_opt_in(self):
        articles = [{"title": "weather"}, {"title": "chips"}, {"title": "another weather"}]
        ranked = rank_articles(articles, "AI", "semiconductors", include_unrelated=True)
        self.assertEqual([item["article"] for item in ranked], [articles[1], articles[0], articles[2]])


class EvidenceCompletenessTests(unittest.TestCase):
    def test_catalog_only_recommended_for_relevant_profile(self):
        generic = build_research_brief("AI company earnings", [], as_of=AS_OF)
        specific = build_research_brief("美股行业拥挤与杠杆", [], as_of=AS_OF)
        self.assertEqual(generic["profile"]["key"], "generic")
        self.assertEqual(generic["recommended_catalog_keys"], [])
        self.assertEqual(specific["recommended_catalog_keys"], ["sector_pack", "finra", "sec_pf"])
        self.assertFalse(specific["ready"])
        japan = build_research_brief("日本銀行槓桿", [], as_of=AS_OF)
        self.assertEqual(japan["profile"]["key"], "generic")
        self.assertEqual(japan["recommended_catalog_keys"], [])

    def test_names_and_missing_text_are_never_fulfilled(self):
        sources = [source("FINRA margin balances missing. SEC Private Fund Statistics unavailable. Sector holdings not provided.",
                          title="FINRA SEC PF Sector institutional investor positioning")]
        result = build_research_brief("Crowding and leverage", sources, as_of=AS_OF)
        self.assertFalse(result["ready"])
        self.assertTrue(all(item["status"] != "fulfilled" for item in result["requirements"]))

    def test_sector_flows_do_not_satisfy_positioning(self):
        item = row(requirement_id="sector_positioning", metric="sector fund flows", scope="technology sector",
                   evidence_text="Survey A: technology sector fund flows in 2026-08 were 20 USD million.")
        result = brief([item], topic="sector crowding and leverage")
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["requirements"][0]["status"], "candidate")
        self.assertEqual(result["warnings"][0]["code"], "wrong_evidence_kind")

    def test_sector_index_or_etf_weights_are_not_investor_positioning(self):
        for metric, scope in (("Technology sector index weights", "US equity fund holdings investors"),
                              ("Technology sector holdings", "SPY ETF index portfolio")):
            quote = f"{metric} for {scope} in 2026-08 were 20 percent."
            item = row(requirement_id="sector_positioning", metric=metric, scope=scope, unit="percent", evidence_text=quote)
            item.pop("group")
            result = brief([item], topic="crowding and leverage")
            self.assertEqual(result["observations"], [])

    def test_margin_label_cannot_turn_cpi_into_finra_balances(self):
        item = row(requirement_id="margin", metric="CPI", evidence_text="Survey A FINRA: US retail CPI in 2026-08 was 20 USD million.")
        result = brief([item], topic="crowding and leverage")
        self.assertEqual(result["observations"], [])

    def test_missing_quote_cannot_be_validated_using_another_number(self):
        item = row(evidence_text="Survey A: US retail widget sales in 2026-08 missing; sample count 20 USD million.")
        result = brief([item])
        self.assertFalse(result["ready"])
        self.assertEqual(result["warnings"][0]["code"], "missing_data_excerpt")

    def test_fund_borrowing_alone_does_not_prove_concentration(self):
        excerpt = "SEC Private Fund Statistics: qualifying hedge funds borrowing in 2026Q2 was 20 USD million; top 25 concentration was 55 percent."
        item = row(requirement_id="fund_borrowing", metric="borrowing", period="2026Q2", scope="qualifying hedge funds", evidence_text=excerpt)
        item.pop("group")
        result = brief([item], topic="crowding and leverage")
        fund = next(req for req in result["requirements"] if req["id"] == "fund_borrowing")
        self.assertEqual(len(result["observations"]), 1)
        self.assertEqual(fund["status"], "candidate")

    def test_generic_private_fund_source_cannot_satisfy_sec_requirement(self):
        excerpt = "Sector private fund data in 2026Q2: qualifying hedge funds borrowing was 20 USD million."
        item = row(requirement_id="fund_borrowing", metric="borrowing", period="2026Q2", scope="qualifying hedge funds", evidence_text=excerpt)
        item.pop("group")
        result = brief([item], topic="crowding and leverage")
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["warnings"][0]["code"], "wrong_evidence_kind")

    def test_single_borrowing_concentration_metric_does_not_prove_borrowing_amount(self):
        excerpt = "SEC Private Fund Statistics in 2026Q2: qualifying hedge funds borrowing concentration was 20 percent."
        item = row(requirement_id="fund_borrowing", metric="borrowing concentration", period="2026Q2", scope="qualifying hedge funds", unit="percent", evidence_text=excerpt)
        item.pop("group")
        result = brief([item], topic="crowding and leverage")
        fund = next(req for req in result["requirements"] if req["id"] == "fund_borrowing")
        self.assertEqual(fund["status"], "candidate")

    def test_complete_profile_requires_direct_records_and_compatible_history(self):
        sector = "Technology sector institutional positioning in 2026-08 was 18 percent."
        margin = "FINRA margin debt for all margin accounts: Jul-26 100 USD million; Aug-26 120 USD million."
        funds = "SEC Private Fund Statistics in 2026Q2: qualifying hedge funds borrowing was 300 USD billion; top 25 concentration was 55 percent."
        sources = [source(sector), source(margin, "S2"), source(funds, "S3")]
        observations = [
            row(requirement_id="sector_positioning", metric="institutional positioning", value="18", unit="percent", scope="Technology sector", evidence_text=sector),
            row(source_id="S2", requirement_id="margin", metric="margin debt", value="100", period="2026-07", scope="all margin accounts", evidence_text=margin),
            row(source_id="S2", requirement_id="margin", metric="margin debt", value="120", scope="all margin accounts", evidence_text=margin),
            row(source_id="S3", requirement_id="fund_borrowing", metric="borrowing", value="300", unit="USD billion", period="2026Q2", scope="qualifying hedge funds", evidence_text=funds),
            row(source_id="S3", requirement_id="fund_borrowing", metric="top 25 concentration", value="55", unit="percent", period="2026Q2", scope="qualifying hedge funds", evidence_text=funds),
        ]
        for item in observations:
            item.pop("group")
        result = brief(observations, sources, "Crowding and leverage")
        self.assertTrue(result["ready"], result["warnings"])
        self.assertEqual(result["missing_required"], [])
        self.assertEqual(result["comparisons"][0]["change"], "20")
        self.assertEqual(result["recommended_catalog_keys"], [])


class ObservationValidationTests(unittest.TestCase):
    def test_numeric_record_anchored_and_unknown_keys_dropped(self):
        item = row(value="20.000", api_key="synthetic-secret", unknown={"unsafe": object()})
        result = brief([item])
        self.assertTrue(result["ready"])
        self.assertEqual(result["observations"][0]["value"], "20")
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertNotIn("unknown", result["observations"][0])

    def test_nonfinite_booleans_numeric_types_and_partial_numeric_matches_rejected(self):
        for value in (True, False, 20, float("inf"), "NaN", "Infinity", "1e1000", "2"):
            with self.subTest(value=value):
                result = brief([row(value=value)])
                self.assertFalse(result["observations"])

    def test_dates_and_metric_or_population_labels_are_not_measured_values(self):
        for value in ("20", "2026", "8"):
            item = row(value=value, period="2026-08-20", evidence_text="Survey A: US retail widget sales measured in USD million as of 2026-08-20.")
            self.assertEqual(brief([item])["observations"], [])
        for observation_date in ("August 20, 2026", "20 August 2026", "Aug 20th 2026", "2026年8月20日"):
            item = row(value="20", period="2026-08-20", evidence_text=f"Survey A: US retail widget sales measured in USD million as of {observation_date}.")
            self.assertEqual(brief([item])["observations"], [])
        item = row(value="25", metric="top 25 widget sales", evidence_text="Survey A: US retail top 25 widget sales in USD million as of 2026-08.")
        self.assertEqual(brief([item])["observations"], [])

    def test_number_at_sentence_end_and_decimal_boundaries(self):
        item = row(evidence_text="Survey A: US retail widget sales in 2026-08 measured in USD million were 20.")
        self.assertTrue(brief([item])["ready"])
        item = row(evidence_text="Survey A: US retail widget sales in 2026-08 measured in USD million were 20.5.")
        self.assertEqual(brief([item])["observations"], [])
        item["value"] = "20.5"
        self.assertTrue(brief([item])["ready"])
        item = row(value="25", scope="top 25 US retail firms", evidence_text="Survey A: top 25 US retail firms widget sales in USD million as of 2026-08.")
        self.assertEqual(brief([item])["observations"], [])

    def test_unknown_source_and_future_period_rejected(self):
        result = brief([row(source_id="S999")])
        self.assertEqual(result["warnings"][0]["code"], "unknown_reference")
        result = brief([row(period="2027-01", evidence_text="Survey A: US retail widget sales in 2027-01 were 20 USD million.")])
        self.assertEqual(result["warnings"][0]["code"], "future_observation")
        self.assertFalse(brief([row(period="2026-10")])["observations"])

    def test_quote_must_be_exact_content_not_title(self):
        item = row()
        result = brief([item], [source("Unrelated body", title=item["evidence_text"])])
        self.assertEqual(result["warnings"][0]["code"], "unanchored_excerpt")

    def test_units_period_scope_and_dataset_cannot_be_forged(self):
        for changed in ({"unit": "USD billion"}, {"period": "2026-07"}, {"scope": "European funds"},
                        {"metric": "earnings"}, {"group": "Survey B"}):
            with self.subTest(changed=changed):
                self.assertEqual(brief([row(**changed)])["observations"], [])

    def test_source_observation_date_can_anchor_table_period(self):
        item = row(evidence_text="Survey A: US retail widget sales were 20 USD million.")
        result = brief([item], [source(item["evidence_text"], observation_date="2026-08-31")])
        self.assertTrue(result["ready"])

    def test_source_observation_date_cannot_override_conflicting_quote_period(self):
        item = row(period="2026-09")
        result = brief([item], [source(item["evidence_text"], observation_date="2026-09-30")])
        self.assertEqual(result["observations"], [])

    def test_empty_group_is_treated_as_absent(self):
        result = brief([row(group="")])
        self.assertTrue(result["ready"])
        self.assertNotIn("group", result["observations"][0])

    def test_chinese_numbers_units_and_traditional_simplified_metadata(self):
        item = row(metric="融资余额", value="20", unit="亿元", period="2026-08", scope="证券账户",
                   evidence_text="2026年8月證券賬戶融资余额為20亿元。")
        item.pop("group")
        # The scope must still use the source's actual population label.
        item["scope"] = "證券賬戶"
        result = brief([item], topic="融资余额")
        self.assertTrue(result["ready"], result["warnings"])

    def test_invalid_as_of_rejects_data_with_safe_diagnostic(self):
        result = brief([row()], as_of="not-a-date")
        self.assertFalse(result["ready"])
        self.assertEqual(result["warnings"][0]["code"], "invalid_as_of")


class ComparisonTests(unittest.TestCase):
    def test_period_value_pairing_rejects_swapped_records_in_broad_excerpt(self):
        excerpt = "Survey A: US retail widget sales were 30 USD million in 2026-08 and 25 USD million in 2026-09."
        before, after = row(value="25", evidence_text=excerpt), row(value="30", period="2026-09", evidence_text=excerpt)
        result = brief([before, after])
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["comparisons"], [])
        self.assertFalse(result["ready"])
        before["value"], after["value"] = "30", "25"
        result = brief([before, after])
        self.assertEqual(result["comparisons"][0]["change"], "-5")

    def test_column_header_layout_is_ambiguous_and_not_calculated(self):
        excerpt = "Survey A: US retail widget sales in USD million: 2026-08 | 2026-09\n30 | 25"
        result = brief([row(value="30", evidence_text=excerpt), row(value="25", period="2026-09", evidence_text=excerpt)])
        self.assertEqual(result["observations"], [])

    def test_accounting_basis_cannot_be_removed_from_metric(self):
        gross = row(metric="borrowing", evidence_text="Survey A: US retail gross borrowing in 2026-08 was 20 USD million.")
        net = row(metric="borrowing", period="2026-09", evidence_text="Survey A: US retail net borrowing in 2026-09 was 20 USD million.")
        result = brief([gross, net], topic="borrowing")
        self.assertEqual(result["observations"], [])
        gross["metric"], net["metric"] = "gross borrowing", "net borrowing"
        result = brief([gross, net], topic="borrowing")
        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(result["comparisons"], [])

    def test_canonical_decimal_arithmetic_traceability_and_zero_denominator(self):
        before = row(value="0", period="2026-07", evidence_text="Survey A: US retail widget sales in 2026-07 were 0 USD million.")
        result = brief([before, row()])
        comparison = result["comparisons"][0]
        self.assertEqual(comparison["change"], "20")
        self.assertIsNone(comparison["percent_change"])
        self.assertEqual(comparison["source_ids"], ["S1"])
        self.assertEqual(comparison["operands"][0], {"source_id": "S1", "period": "2026-07", "value": "0"})
        before = row(value="20", period="2026-07", evidence_text="Survey A: US retail widget sales in 2026-07 were 20 USD million.")
        after = row(value="25", evidence_text="Survey A: US retail widget sales in 2026-08 were 25 USD million.")
        comparison = brief([after, before])["comparisons"][0]
        self.assertEqual((comparison["change"], comparison["percent_change"]), ("5", "25"))

    def test_unit_frequency_scope_metric_and_dataset_mismatch_block_comparison(self):
        before = row(period="2026-07", evidence_text="Survey A: US retail widget sales in 2026-07 were 20 USD million.")
        variants = [
            row(unit="USD billion", evidence_text="Survey A: US retail widget sales in 2026-08 were 20 USD billion."),
            row(period="2026Q2", evidence_text="Survey A: US retail widget sales in 2026Q2 were 20 USD million."),
            row(scope="EU retail", evidence_text="Survey A: EU retail widget sales in 2026-08 were 20 USD million."),
            row(metric="widget sales volume", evidence_text="Survey A: US retail widget sales volume in 2026-08 were 20 USD million."),
            row(group="Survey B", evidence_text="Survey B: US retail widget sales in 2026-08 were 20 USD million."),
        ]
        for after in variants:
            with self.subTest(after=after):
                result = brief([before, after])
                self.assertEqual(len(result["observations"]), 2, result["warnings"])
                self.assertEqual(result["comparisons"], [])

    def test_cross_source_needs_quoted_shared_dataset(self):
        before = row(period="2026-07", evidence_text="Survey A: US retail widget sales in 2026-07 were 20 USD million.")
        after = row(source_id="S2")
        sources = [source(before["evidence_text"]), source(after["evidence_text"], "S2")]
        self.assertEqual(brief([before, after], sources)["comparisons"][0]["source_ids"], ["S1", "S2"])
        before.pop("group")
        after.pop("group")
        self.assertEqual(brief([before, after], sources)["comparisons"], [])

    def test_conflicting_same_period_values_block_entire_series(self):
        before = row(period="2026-07", evidence_text="Survey A: US retail widget sales in 2026-07 were 20 USD million.")
        conflict = row(value="25", evidence_text="Survey A: US retail widget sales in 2026-08 were 25 USD million.")
        result = brief([before, row(), conflict])
        self.assertEqual(result["comparisons"], [])
        self.assertEqual(result["warnings"][-1]["code"], "conflicting_values")


if __name__ == "__main__":
    unittest.main()
