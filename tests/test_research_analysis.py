"""One preparation call with anchored extraction; no live model or news service."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src.analysis_errors import ModelResponseError, ProviderAPIError, ProviderConfigurationError
from src.research import prepare_evidence, ResearchError
from src.research_analysis import extract_research_observations
from src.research_export import export_html, export_markdown, export_pdf
from src.research_store import ResearchStore
from src.research_workflow import generate_research_workflow


def sources():
    return prepare_evidence([], [{"source_type": "manual", "title": "Battery production",
        "content": "Battery production for US factories: 2026-08 output 20 GWh; 2026-09 output 25 GWh.",
        "observation_date": "2026-09", "publisher": "Synthetic factory"}])


def row(**changes):
    result = {"source_id": "S1", "requirement_id": "generic_data", "metric": "output",
              "value": "20", "unit": "GWh", "period": "2026-08", "scope": "US factories",
              "group": "", "evidence_text": sources()[0]["content"]}
    result.update(changes)
    return result


class Stream:
    def __init__(self, payload, *, raw=False):
        self.raw = payload if raw else json.dumps(payload, ensure_ascii=False)
        self.closed = False

    def __iter__(self):
        yield SimpleNamespace(choices=[SimpleNamespace(finish_reason=None,
            delta=SimpleNamespace(content=self.raw))])
        yield SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", delta=SimpleNamespace(content=None))])

    def close(self):
        self.closed = True


class ResearchAnalysisTests(unittest.TestCase):
    def extract(self, payload, **kwargs):
        stream = Stream(payload)
        client = MagicMock()
        client.chat.completions.create.return_value = stream
        result = extract_research_observations("Battery production", sources(), api_key="synthetic-key",
            as_of="2026-10-14", client=client, **kwargs)
        self.assertTrue(stream.closed)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        return result, client

    def test_two_anchored_rows_calculated_by_program(self):
        result, client = self.extract({"observations": [row(), row(period="2026-09", value="25")]})
        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(result["comparisons"][0]["change"], "5")
        self.assertEqual(result["comparisons"][0]["percent_change"], "25")
        self.assertFalse(result["preparation"]["manually_verified"])
        request = client.chat.completions.create.call_args.kwargs
        self.assertNotIn("synthetic-key", json.dumps(request))
        packet = json.loads(request["messages"][1]["content"])
        self.assertEqual(packet["sources"][0]["id"], "S1")
        self.assertIn("requirements", packet)

    def test_fabricated_source_numbers_and_anchors_do_not_fill_gaps(self):
        result, _ = self.extract({"observations": [row(source_id="S99"), row(value="999"),
            row(evidence_text="A statement absent from the source") ]})
        self.assertFalse(result["ready"])
        self.assertEqual(result["observations"], [])
        self.assertTrue(result["warnings"])

    def test_future_period_and_bool_value_rejected(self):
        result, _ = self.extract({"observations": [row(period="2027-08"), row(value=True)]})
        self.assertEqual(result["preparation"]["accepted_count"], 0)

    def test_empty_extraction_is_valid_gap_result(self):
        result, _ = self.extract({"observations": []})
        self.assertFalse(result["ready"])
        self.assertEqual(result["preparation"]["suggested_count"], 0)

    def test_post_validation_deadline_does_not_publish_late_preparation(self):
        from src.research_quality import build_research_brief
        calls = 0
        clock = [0.0]

        def validate(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = build_research_brief(*args, **kwargs)
            if calls == 2:
                clock[0] = 181.0
            return result

        with patch("src.research_analysis.time.monotonic", side_effect=lambda: clock[0]), \
             patch("src.research_analysis.build_research_brief", side_effect=validate):
            with self.assertRaises(ResearchError) as caught:
                self.extract({"observations": []})
        self.assertEqual(caught.exception.reason, "generation_timeout")

    def test_untrusted_shape_rejected_without_echoing_model_text(self):
        with self.assertRaises(ModelResponseError) as caught:
            self.extract({"observations": [], "secret": "synthetic-key"})
        self.assertNotIn("synthetic-key", str(caught.exception))
        with self.assertRaises(ModelResponseError):
            self.extract({"observations": [{}] * 81})

    def test_owned_client_closed_and_only_selected_endpoint_used(self):
        client = MagicMock()
        client.chat.completions.create.return_value = Stream({"observations": []})
        with patch("src.research_analysis.OpenAI", return_value=client) as factory:
            result = extract_research_observations("Battery production", sources(), api_key="synthetic-key",
                provider="gemini", model="gemini-3.8-flash", as_of="2026-10-14")
        self.assertEqual(factory.call_args.kwargs["base_url"], "https://generativelanguage.googleapis.com/v1beta/openai/")
        client.close.assert_called_once()
        self.assertEqual(result["observations"], [])

    def test_provider_error_body_not_disclosed(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("raw-error synthetic-key")
        with self.assertRaises(ProviderAPIError) as caught:
            extract_research_observations("Battery production", sources(), api_key="synthetic-key",
                as_of="2026-10-14", client=client)
        self.assertNotIn("raw-error", str(caught.exception))

    def test_bad_dates_and_missing_key_stop_before_network(self):
        client = MagicMock()
        for changes, exception in [({"api_key": ""}, ProviderConfigurationError),
                                   ({"as_of": "invalid"}, ResearchError)]:
            arguments = {"api_key": "synthetic-key", "as_of": "2026-10-14", **changes}
            with self.assertRaises(exception):
                extract_research_observations("Battery production", sources(), client=client, **arguments)
        client.chat.completions.create.assert_not_called()

    def test_prepare_write_review_save_edit_and_export_with_three_synthetic_calls(self):
        snapshot = sources()
        fields = ("executive_findings", "chart_data_interpretation", "differences_and_alternatives",
                  "conditional_mechanisms", "watchlist", "limitations")
        writer = {"title": "Battery production comparison", **{field: [
            {"text": "Battery production: 2026-08 output 20 GWh; 2026-09 output 25 GWh.",
             "source_ids": ["S1"]}] for field in fields}}
        reviewer = {"draft": writer, "findings": [], "summary": "Compared supplied snapshots only."}
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            Stream({"observations": [row(), row(value="25", period="2026-09")]}),
            Stream(writer), Stream(reviewer)]
        brief = extract_research_observations("Battery production", snapshot, api_key="synthetic-key",
            as_of="2026-10-14", client=client)
        saved_writer = []
        with ResearchStore(":memory:") as store:
            def save_initial(draft):
                self.assertEqual(client.chat.completions.create.call_count, 2)
                saved_writer.append(store.create_report(topic="Battery production", title=draft["title"],
                    body=draft["body"], sources=snapshot, provider="deepseek", model="deepseek-v4-pro",
                    as_of="2026-10-14", metadata=draft["metadata"]))

            result = generate_research_workflow("Battery production", snapshot, api_key="synthetic-key",
                as_of="2026-10-14", client=client, research_brief=brief, report_mode="research", on_draft=save_initial)
            current = store.save_review(saved_writer[0]["id"], title=result["title"], body=result["body"],
                expected_version=1, metadata=result["metadata"])
            self.assertEqual(current["version"], 2)
            self.assertEqual(current["metadata"]["research_brief"]["comparisons"][0]["change"], "5")
            self.assertIn("GWh", export_html(current).decode("utf-8"))
            self.assertIn("2026-08", export_markdown(current).decode("utf-8"))
            self.assertTrue(export_pdf(current).startswith(b"%PDF-"))
            edited = store.save_revision(current["id"], title="My title", body="My full replacement.", expected_version=2)
            self.assertEqual(edited["version"], 3)
            self.assertIn("2026-08", edited["sources"][0]["content"])
            self.assertEqual(edited["metadata"]["research_brief"]["observations"][1]["value"], "25")
            self.assertEqual(len(store.list_revisions(current["id"])), 3)
        self.assertEqual(client.chat.completions.create.call_count, 3)


if __name__ == "__main__":
    unittest.main()
