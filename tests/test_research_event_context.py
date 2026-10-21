"""Research history and live search remain bounded, cited and opt-in."""

import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from src.analysis_errors import ModelResponseError, ProviderConfigurationError
from src.research import MAX_SOURCES, MAX_TOTAL_SOURCE_CHARS, prepare_sources
from src.research_context import merge_search_sources, previous_research_context
from src.research_store import ResearchStore
from src.research_workflow import generate_research_workflow
from test_argumentation import essay, plan


def sources():
    return prepare_sources([{"id": identifier, "title": title, "content": content,
        "source_name": "Test", "content_scope": "provided_text", "published_at": "2026-10-14T01:00:00Z"}
        for identifier, title, content in [(1, "Orders", "Orders improved in the disclosed business."),
            (2, "Costs", "Costs constrained margins in the disclosed business."),
            (3, "Travel", "An unrelated travel promotion.")]])


class Stream:
    def __init__(self, value):
        self.text = json.dumps(value)

    def __iter__(self):
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=self.text), finish_reason=None)])
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")])

    def close(self):
        pass


def review(draft=None):
    return {"draft": draft or essay(), "findings": [], "summary": "Compared the supplied sources.",
            "quality": {name: 3 for name in ("relevance", "evidence", "reasoning", "readability")} | {"verdict": "pass"}}


def supplemental(url="https://example.test/filing", **overrides):
    return {"sources": [{"id": "S1", "title": "Delivery disclosure", "url": url,
            "content": "Delivery improved in the same business.", "publisher": "Search summary",
            "source_type": "official", "content_scope": "full_text", **overrides}],
            "usage": {"input_tokens": 40, "total_tokens": 100, "api_key": "must-not-save"}, "warnings": []}


def previous():
    return {"id": "old-report", "title": "Order recovery", "body": "Earlier hypothesis [S1]: demand may recover.",
            "as_of": "2026-10-13", "version": 2, "metadata": {"api_key": "must-not-save"}}


def changed_draft(status="strengthen", ids=None):
    draft = essay()
    draft["research_change"] = {"status": status,
        "new_information": {"text": "Orders improved while cost constraints persist.", "source_ids": ids or ["S1", "S2"]},
        "reason": {"text": "The demand hypothesis gains support but profit remains conditional on costs.", "source_ids": ["S1", "S2"]}}
    return draft


class ResearchEventContextTests(unittest.TestCase):
    def client(self, *responses):
        client = MagicMock()
        client.chat.completions.create.side_effect = [Stream(value) if isinstance(value, dict) else value for value in responses]
        return client

    def generate(self, client, **kwargs):
        return generate_research_workflow("Orders and profits", sources(), api_key="analysis-secret",
            as_of="2026-10-14", report_mode="argumentative", retrieve_sources=False, client=client, **kwargs)

    def test_default_does_not_search_or_add_history(self):
        lookup = MagicMock()
        result = self.generate(self.client(plan(), essay(), review()), searcher=lookup)
        lookup.assert_not_called()
        self.assertNotIn("previous_research_summary", result["metadata"])
        self.assertNotIn("evidence_search", result["metadata"])
        self.assertEqual(result["metadata"]["api_call_count"], 3)

    def test_search_replans_with_locally_owned_ids_and_keeps_usage(self):
        second = plan()
        second["source_ids"].append("S4")
        second["arguments"][0]["source_ids"].append("S4")
        second.update(answer_scope="full", missing_evidence=[])
        client = self.client(plan(), second, essay(), review())
        lookup = MagicMock(return_value=supplemental())
        result = self.generate(client, search_enabled=True, search_api_key="search-secret", searcher=lookup)
        self.assertEqual(lookup.call_count, 1)
        packets = [json.loads(call.kwargs["messages"][1]["content"]) for call in client.chat.completions.create.call_args_list]
        added = packets[1]["sources"][-1]
        self.assertEqual(added["id"], "S4")
        self.assertEqual(added["source_type"], "search")
        self.assertEqual(added["content_scope"], "search_summary")
        self.assertFalse(added["independent_evidence"])
        self.assertEqual(packets[1]["sources"][0]["content"], sources()[0]["content"])
        self.assertEqual(result["metadata"]["api_call_count"], 5)
        entry = result["metadata"]["evidence_search"]["rounds"][0]
        self.assertEqual(entry["source_ids"], ["S4"])
        self.assertEqual(entry["usage"], {"input_tokens": 40, "total_tokens": 100})
        for secret in ("analysis-secret", "search-secret", "must-not-save"):
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret, json.dumps(packets))

    def test_search_citations_cannot_become_verified_by_reviewer_scores(self):
        second = plan(); second["source_ids"].append("S4")
        second["arguments"][0]["source_ids"].append("S4")
        second.update(answer_scope="full", missing_evidence=[])
        draft = essay(); draft["thesis"][0]["source_ids"].append("S4")
        response = supplemental()
        response["warnings"] = ["搜尋引用缺少有效位置或網址，該引用未作為證據保存。"]
        result = self.generate(self.client(plan(), second, draft, review(draft)), search_enabled=True,
            search_api_key="key", searcher=MagicMock(return_value=response))
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertTrue(any(w["code"] == "search_summary_not_original" for w in result["metadata"]["validation_warnings"]))
        self.assertIn(response["warnings"][0], result["metadata"]["evidence_search"]["warnings"])

    def test_search_summary_alone_cannot_close_evidence_gap(self):
        second = plan(); second["source_ids"].append("S4")
        second["arguments"][0]["source_ids"] = ["S4"]
        second.update(answer_scope="full", missing_evidence=[])
        result = self.generate(self.client(plan(), second, essay(), review()), search_enabled=True,
            search_api_key="key", searcher=MagicMock(return_value=supplemental()))
        self.assertEqual(result["metadata"]["argument_plan"]["answer_scope"], "narrowed")
        self.assertTrue(any("原始發布" in gap for gap in result["metadata"]["argument_plan"]["missing_evidence"]))

    def test_at_most_two_distinct_gap_searches_and_repeated_query_stops(self):
        second = plan(); second["missing_evidence"] = ["Delivery timetable."]
        third = plan(); third["missing_evidence"] = ["Cost breakdown."]
        lookup = MagicMock(side_effect=[supplemental(), supplemental("https://example.test/second")])
        result = self.generate(self.client(plan(), second, third, essay(), review()),
            search_enabled=True, search_api_key="key", max_search_rounds=2, max_search_turns=1, searcher=lookup)
        self.assertEqual(lookup.call_count, 2)
        self.assertTrue(all(call.kwargs["max_turns"] == 1 for call in lookup.call_args_list))
        self.assertEqual(result["metadata"]["api_call_count"], 7)
        repeated = MagicMock(return_value=supplemental())
        self.generate(self.client(plan(), plan(), essay(), review()), search_enabled=True,
            search_api_key="key", max_search_rounds=2, searcher=repeated)
        self.assertEqual(repeated.call_count, 1)

    def test_no_gap_means_no_search_even_when_enabled(self):
        ready = plan(); ready.update(answer_scope="full", missing_evidence=[])
        lookup = MagicMock()
        self.generate(self.client(ready, essay(), review()), search_enabled=True, search_api_key="key", searcher=lookup)
        lookup.assert_not_called()

    def test_failed_search_retains_report_and_sanitizes_error_usage(self):
        error = ModelResponseError("truncated")
        error.usage = {"total_tokens": 90, "api_key": "raw-secret", "input_tokens_details": {"cached_tokens": 5, "raw": "bad"}}
        lookup = MagicMock(side_effect=error)
        result = self.generate(self.client(plan(), essay(), review()), search_enabled=True, search_api_key="key", searcher=lookup)
        entry = result["metadata"]["evidence_search"]["rounds"][0]
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["usage"], {"total_tokens": 90, "input_tokens_details": {"cached_tokens": 5}})
        self.assertNotIn("raw-secret", json.dumps(result))
        self.assertIn("Order", result["body"])
        self.assertEqual(result["metadata"]["api_call_count"], 4)

    def test_full_plan_with_unresolved_gap_cannot_pass_after_unsuccessful_search(self):
        full_with_gap = plan()
        full_with_gap["answer_scope"] = "full"
        invalid_replan = copy.deepcopy(full_with_gap)
        invalid_replan["source_ids"].append("S999")
        cases = [
            ("error", MagicMock(side_effect=RuntimeError("private provider error")),
             [full_with_gap, essay(), review()]),
            ("no_new_sources", MagicMock(return_value={"sources": [], "usage": {}, "warnings": []}),
             [full_with_gap, essay(), review()]),
            ("replan_failed", MagicMock(return_value=supplemental()),
             [full_with_gap, invalid_replan, essay(), review()]),
        ]
        for status, lookup, replies in cases:
            with self.subTest(status=status):
                result = self.generate(self.client(*replies), search_enabled=True,
                    search_api_key="key", searcher=lookup)
                metadata = result["metadata"]
                self.assertEqual(metadata["evidence_search"]["rounds"][0]["status"], status)
                self.assertEqual(metadata["argument_plan"]["answer_scope"], "full")
                self.assertTrue(metadata["argument_plan"]["missing_evidence"])
                self.assertEqual(metadata["review_status"], "needs_review")
                self.assertEqual(metadata["quality_status"], "needs_review")
                self.assertTrue(any("未補齊" in finding["message"] for finding in metadata["review_findings"]))

    def test_invalid_replan_retains_previous_plan_and_search_audit(self):
        bad = plan(); bad["source_ids"].append("S999")
        result = self.generate(self.client(plan(), bad, essay(), review()), search_enabled=True,
            search_api_key="key", searcher=MagicMock(return_value=supplemental()))
        self.assertEqual(result["metadata"]["argument_plan"], plan())
        entry = result["metadata"]["evidence_search"]["rounds"][0]
        self.assertEqual(entry["status"], "replan_failed")
        self.assertEqual(entry["sources"][0]["id"], "S4")
        self.assertNotIn("S999", json.dumps(result))

    def test_dated_future_search_source_is_excluded(self):
        result = self.generate(self.client(plan(), essay(), review()), search_enabled=True,
            search_api_key="key", searcher=MagicMock(return_value=supplemental(publication_date="2026-10-15")))
        self.assertEqual(result["metadata"]["evidence_search"]["rounds"][0]["status"], "no_new_sources")
        self.assertEqual([source["id"] for source in result["sources"]], ["S1", "S2"])

    def test_history_is_bounded_separate_hypothesis_and_comparison_is_cited(self):
        old = previous(); old["body"] += "x" * 14000
        old["sources"] = [{"id": "S999", "content": "untrusted old fact"}]
        draft = changed_draft()
        client = self.client(plan(), draft, review(draft))
        result = self.generate(client, previous_research=old)
        for call in client.chat.completions.create.call_args_list:
            packet = json.loads(call.kwargs["messages"][1]["content"])
            context = packet["previous_research"]
            self.assertIn("H1", context["body"])
            self.assertNotIn("S1", context["body"])
            self.assertLessEqual(len(context["body"]), 12000)
            self.assertTrue(context["body_truncated"])
            self.assertNotIn("sources", context)
            self.assertNotIn("untrusted old fact", json.dumps(packet))
        self.assertIn("判斷變化：加強", result["body"])
        self.assertEqual(result["metadata"]["research_change"]["status"], "strengthen")
        with ResearchStore(":memory:") as store:
            saved = store.create_report(topic="Orders", title=result["title"], body=result["body"],
                sources=result["sources"], provider="deepseek", model="deepseek-flash", as_of="2026-10-14", metadata=result["metadata"])
            self.assertEqual(saved["metadata"]["previous_research_summary"]["id"], "old-report")
            self.assertEqual(saved["metadata"]["research_change"]["status"], "strengthen")

    def test_bad_historical_citations_cannot_confirm_prior_judgement(self):
        draft = changed_draft(ids=["H1", "S999"])
        result = self.generate(self.client(plan(), draft, review(draft)), previous_research=previous())
        self.assertEqual(result["metadata"]["research_change"]["status"], "insufficient")
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertNotIn("S999", result["body"])
        self.assertTrue(any(w["code"] == "invalid_research_change" for w in result["metadata"]["validation_warnings"]))

    def test_history_numeric_claims_are_checked_against_current_citations(self):
        draft = changed_draft()
        draft["research_change"]["reason"]["text"] = "Revenue rose 987 percent."
        result = self.generate(self.client(plan(), draft, review(draft)), previous_research=previous())
        self.assertTrue(any(w["code"] == "untraceable_numbers" and w["section"] == "research_change.reason"
                            for w in result["metadata"]["validation_warnings"]))
        self.assertEqual(result["metadata"]["review_status"], "needs_review")

    def test_missing_comparison_preserves_essay_with_insufficient_status(self):
        result = self.generate(self.client(plan(), essay(), review()), previous_research=previous())
        self.assertIn("Orders", result["body"])
        self.assertEqual(result["metadata"]["research_change"]["status"], "insufficient")

    def test_nontext_change_status_preserves_essay_with_warning(self):
        for invalid in ([], {}):
            draft = changed_draft(); draft["research_change"]["status"] = invalid
            result = self.generate(self.client(plan(), draft, review(draft)), previous_research=previous())
            self.assertIn("Orders", result["body"])
            self.assertEqual(result["metadata"]["research_change"]["status"], "insufficient")

    def test_valid_insufficient_plan_saves_gap_notice_without_invented_sources(self):
        insufficient = plan()
        insufficient.update(answer_scope="insufficient", source_ids=[], arguments=[])
        client = self.client(insufficient)
        saved = []
        result = self.generate(client, previous_research=previous(), on_draft=saved.append)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(len(saved), 1)
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["metadata"]["research_change"]["status"], "insufficient")
        self.assertEqual(result["metadata"]["workflow_stage"], "insufficient_evidence")
        self.assertIn("現有材料不足", result["body"])

    def test_invalid_options_fail_before_paid_request_and_legacy_is_explicit(self):
        for options in ({"search_enabled": True}, {"max_search_rounds": 3}, {"max_search_turns": 0},
                        {"previous_research": {**previous(), "as_of": "2026-10-15"}}):
            client = self.client()
            with self.subTest(options=options), self.assertRaises((ValueError, ProviderConfigurationError)):
                self.generate(client, **options)
            client.chat.completions.create.assert_not_called()
        with self.assertRaisesRegex(ValueError, "議論文"):
            generate_research_workflow("Orders", sources(), api_key="key", as_of="2026-10-14", previous_research=previous())

    def test_source_merge_deduplicates_and_enforces_existing_budgets(self):
        original = sources(); original[0]["url"] = "https://example.test/filing"
        unchanged, added = merge_search_sources(original, supplemental()["sources"])
        self.assertEqual(added, [])
        self.assertEqual(len(unchanged), len(original))
        filled = [dict(original[0], id=f"S{n+1}", url=f"https://example.test/{n}", content="x" * 4500) for n in range(MAX_SOURCES)]
        merged, added = merge_search_sources(filled, supplemental()["sources"])
        self.assertEqual(added, [])
        self.assertLessEqual(sum(len(s["content"]) for s in merged), MAX_TOTAL_SOURCE_CHARS)


if __name__ == "__main__":
    unittest.main()
