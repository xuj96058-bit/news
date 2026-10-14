"""The writing/review workflow uses synthetic evidence and mocked providers."""

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from requests.exceptions import ReadTimeout

from src.analysis_errors import ProviderAPIError, ProviderConfigurationError
from src.llm_providers import PROVIDERS
from src.research import MAX_RESPONSE_CHARS, ResearchError, prepare_sources
from src.research_workflow import generate_research_workflow


KEY = "synthetic-workflow-key"
FIELDS = ("executive_findings", "chart_data_interpretation", "differences_and_alternatives",
          "conditional_mechanisms", "watchlist", "limitations")


def sources():
    return prepare_sources([{"id": "synthetic-story", "title": "Synthetic financing report",
        "source_id": "synthetic-rss", "source_name": "Synthetic publisher",
        "url": "https://example.org/story", "published_at": "2026-10-13T07:00:00Z",
        "content_scope": "summary", "summary": "Gross financing was 20 billion in September.", "sources": []}])


def draft(**overrides):
    result = {"title": "Synthetic draft", **{field: [{"text": "來源 S1 只提供摘要，尚需核對資料。", "source_ids": ["S1"]}]
                                             for field in FIELDS}}
    result["executive_findings"] = [{"text": "Gross financing was 20 billion in September.", "source_ids": ["S1"]}]
    result.update(overrides)
    return result


def review(data=None, **overrides):
    result = {"draft": draft() if data is None else data, "findings": [], "summary": "已比對輸入快照，仍需人工核對。"}
    result.update(overrides)
    return result


class Stream:
    def __init__(self, payload, *, raw=False, finish_reason="stop"):
        self.content = payload if raw else json.dumps(payload, ensure_ascii=False)
        self.finish_reason = finish_reason
        self.closed = False

    def __iter__(self):
        for offset in range(0, len(self.content), 79):
            yield SimpleNamespace(choices=[SimpleNamespace(finish_reason=None,
                delta=SimpleNamespace(content=self.content[offset:offset + 79]))])
        yield SimpleNamespace(choices=[SimpleNamespace(finish_reason=self.finish_reason,
            delta=SimpleNamespace(content=None))])

    def close(self):
        self.closed = True


class InterruptedStream(Stream):
    def __init__(self, payload, error):
        super().__init__(payload)
        self.error = error

    def __iter__(self):
        yield SimpleNamespace(choices=[SimpleNamespace(finish_reason=None,
            delta=SimpleNamespace(content=self.content[:97]))])
        raise self.error


def client_with(*responses):
    client = MagicMock()
    client.chat.completions.create.side_effect = list(responses)
    return client


class ResearchWorkflowTests(unittest.TestCase):
    def generate(self, client, **kwargs):
        return generate_research_workflow("Industry crowding and US leverage", sources(), api_key=KEY,
                                         as_of="2026-10-14", client=client, **kwargs)

    def test_writer_saved_before_independent_reviewer_and_no_callback_mutation(self):
        writer_stream, review_stream = Stream(draft()), Stream(review(draft(title="Reviewed draft")))
        client = client_with(writer_stream, review_stream)
        saved = []

        def save(result):
            self.assertEqual(client.chat.completions.create.call_count, 1)
            self.assertEqual(result["metadata"]["workflow_stage"], "writer_saved")
            self.assertEqual(result["metadata"]["review_status"], "pending")
            saved.append(result)
            result["title"] = "Callback local mutation"

        result = self.generate(client, on_draft=save)
        self.assertEqual(result["title"], "Reviewed draft")
        self.assertEqual(saved[0]["title"], "Callback local mutation")
        self.assertEqual(result["metadata"]["workflow_stage"], "review_complete")
        self.assertEqual(result["metadata"]["review_status"], "reviewed")
        self.assertFalse(result["metadata"]["manually_verified"])
        self.assertIn("來源 S1", result["body"])
        self.assertIn("[S1]", result["body"])
        self.assertTrue(writer_stream.closed and review_stream.closed)
        calls = client.chat.completions.create.call_args_list
        self.assertNotEqual(calls[0].kwargs["messages"][0]["content"], calls[1].kwargs["messages"][0]["content"])
        writer_user = json.loads(calls[0].kwargs["messages"][1]["content"])
        review_user = json.loads(calls[1].kwargs["messages"][1]["content"])
        self.assertEqual(writer_user["sources"], review_user["sources"])
        self.assertEqual(review_user["writer_draft"]["title"], "Synthetic draft")
        self.assertNotIn(KEY, str(calls))

    def test_storage_failure_does_not_start_second_paid_request(self):
        client = client_with(Stream(draft()), Stream(review()))

        def failed_save(_result):
            raise OSError("Synthetic database write failed")

        with self.assertRaisesRegex(OSError, "database write failed"):
            self.generate(client, on_draft=failed_save)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_missing_unknown_ids_and_numbers_save_editable_draft_instead_of_blocking(self):
        writer = draft(executive_findings=[{"text": "An unsupported 999 billion claim.", "source_ids": ["S1", "S999"]}],
                       limitations=[{"text": "Missing source IDs."}])
        saved = []
        result = self.generate(client_with(Stream(writer), RuntimeError("secret provider response " + KEY)), on_draft=saved.append)
        codes = {warning["code"] for warning in saved[0]["metadata"]["validation_warnings"]}
        self.assertTrue({"unknown_source_ids", "missing_source_ids", "untraceable_numbers"}.issubset(codes))
        self.assertIn("999 billion claim", saved[0]["body"])
        self.assertNotIn("[S999]", saved[0]["body"])
        self.assertEqual(saved[0]["metadata"]["review_status"], "needs_review")
        self.assertEqual(result["metadata"]["review_status"], "review_failed")
        self.assertIn("An unsupported 999 billion claim", result["body"])
        self.assertNotIn(KEY, json.dumps(result))

    def test_links_not_in_evidence_are_removed_and_inline_citations_cannot_forge_appendix(self):
        writer = draft(chart_data_interpretation=[{"text": "來源 S1 沒有圖表。[S999] [外部](javascript:alert(1)) https://unknown.example/data <a href='https://unknown.example/a'>bad</a>", "source_ids": ["S1"]}])
        result = self.generate(client_with(Stream(writer), Stream(review(writer))))
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertNotIn("javascript:", result["body"])
        self.assertNotIn("https://unknown.example", result["body"])
        self.assertNotIn("[S999]", result["body"])
        self.assertIn("https://example.org/story", result["body"])
        self.assertIn("來源 S1 沒有圖表", result["body"])

    def test_malformed_writer_json_is_preserved_before_review_failure(self):
        raw = '{"title":"Actual useful writing", "executive_findings": "incomplete JSON'
        saved = []
        result = self.generate(client_with(Stream(raw, raw=True), Stream({"wrong": "shape"})), on_draft=saved.append)
        self.assertEqual(saved[0]["metadata"]["raw_draft"], raw)
        self.assertIsNone(saved[0]["metadata"]["structured_draft"])
        self.assertIn("    " + raw, saved[0]["body"])
        self.assertIn("Actual useful writing", result["body"])
        self.assertEqual(result["metadata"]["review_status"], "review_failed")

    def test_malformed_but_available_writer_can_be_restructured_by_reviewer(self):
        saved = []
        result = self.generate(client_with(Stream("Draft actually returned", raw=True), Stream(review())), on_draft=saved.append)
        self.assertEqual(saved[0]["metadata"]["review_status"], "needs_review")
        self.assertEqual(result["metadata"]["review_status"], "reviewed")
        self.assertNotIn("raw_draft", result["metadata"])

    def test_truncated_writer_is_saved_as_inert_raw_draft_before_review(self):
        first = Stream(draft(), finish_reason='length')
        client = client_with(first, RuntimeError('Synthetic failed review'))
        saved = []

        def persist(result):
            self.assertEqual(client.chat.completions.create.call_count, 1)
            saved.append(result)

        result = self.generate(client, on_draft=persist)
        self.assertTrue(first.closed)
        self.assertEqual(saved[0]['metadata']['review_status'], 'needs_review')
        self.assertTrue(saved[0]['metadata']['writer_response_incomplete'])
        self.assertEqual(saved[0]['metadata']['raw_draft'], first.content)
        self.assertIsNone(saved[0]['metadata']['structured_draft'])
        self.assertIn('    ' + first.content, saved[0]['body'])
        self.assertEqual(result['metadata']['review_status'], 'review_failed')
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_partial_writer_transport_failure_saves_only_arrived_text(self):
        first = InterruptedStream(draft(), ReadTimeout('Provider body secret ' + KEY))
        saved = []
        result = self.generate(client_with(first, Stream(review())), on_draft=saved.append)
        self.assertTrue(first.closed)
        self.assertEqual(saved[0]['metadata']['raw_draft'], first.content[:97])
        self.assertEqual(saved[0]['metadata']['validation_warnings'][0]['code'], 'incomplete_response')
        self.assertNotIn(KEY, json.dumps(saved[0]))
        self.assertNotIn('Provider body secret', json.dumps(saved[0]))
        self.assertEqual(result['metadata']['review_status'], 'reviewed')

    def test_oversized_writer_raw_capture_is_bounded_and_saved(self):
        first = Stream('partial writing ' + 'x' * (MAX_RESPONSE_CHARS + 100), raw=True)
        saved = []
        result = self.generate(client_with(first, RuntimeError('Review unavailable')), on_draft=saved.append)
        self.assertTrue(first.closed)
        self.assertEqual(len(saved[0]['metadata']['raw_draft']), MAX_RESPONSE_CHARS)
        self.assertEqual(result['metadata']['review_status'], 'review_failed')

    def test_partial_review_never_replaces_saved_writer_even_if_json_is_complete(self):
        for second in (Stream(review(draft(title='Must not publish')), finish_reason='length'),
                       InterruptedStream(review(draft(title='Must not publish')), RuntimeError('Provider secret ' + KEY))):
            with self.subTest(kind=type(second).__name__):
                saved = []
                result = self.generate(client_with(Stream(draft()), second), on_draft=saved.append)
                self.assertTrue(second.closed)
                self.assertEqual(result['title'], saved[0]['title'])
                self.assertEqual(result['metadata']['review_status'], 'review_failed')
                self.assertNotIn('Must not publish', result['body'])
                self.assertNotIn(KEY, json.dumps(result))

    def test_review_findings_remain_visible_and_do_not_claim_verified_facts(self):
        findings = [{"section": "limitations", "paragraph": 1, "severity": "warning",
                     "message": "需要人工核對持倉明細。", "source_ids": ["S1"]}]
        result = self.generate(client_with(Stream(draft()), Stream(review(findings=findings))))
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertEqual(result["metadata"]["review_findings"], findings)
        self.assertIn(findings[0]["message"], result["body"])
        self.assertIn("不能取代人工查證", result["body"])

    def test_review_timeout_preserves_writer_and_does_not_retry(self):
        client = client_with(Stream(draft()), ReadTimeout("Synthetic raw timeout " + KEY))
        saved = []
        result = self.generate(client, on_draft=saved.append)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(result["metadata"]["review_status"], "review_failed")
        self.assertIn("逾時", result["metadata"]["review_error"])
        self.assertIn("Gross financing was 20", result["body"])
        self.assertNotIn(KEY, result["metadata"]["review_error"])

    def test_late_writer_validation_does_not_publish_or_start_review(self):
        from src import research_workflow
        clock = [0.0]
        validate = research_workflow._structured_draft

        def expired_validation(*args):
            result = validate(*args)
            clock[0] = 181.0
            return result

        client = client_with(Stream(draft()), Stream(review()))
        saved = MagicMock()
        with patch('src.research_workflow.time.monotonic', side_effect=lambda: clock[0]), \
                patch('src.research_workflow._structured_draft', side_effect=expired_validation):
            with self.assertRaises(ResearchError):
                self.generate(client, on_draft=saved)
        saved.assert_not_called()
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_reviewer_has_fresh_stage_budget_and_late_review_keeps_saved_writer(self):
        from src import research_workflow
        clock = [0.0]
        validate = research_workflow._structured_draft
        calls = [0]

        def stage_time(*args):
            result = validate(*args)
            calls[0] += 1
            clock[0] = 170.0 if calls[0] == 1 else 351.0
            return result

        client = client_with(Stream(draft()), Stream(review()))
        saved = []
        with patch('src.research_workflow.time.monotonic', side_effect=lambda: clock[0]), \
                patch('src.research_workflow._structured_draft', side_effect=stage_time):
            result = self.generate(client, on_draft=saved.append)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(len(saved), 1)
        self.assertEqual(result['metadata']['review_status'], 'review_failed')
        self.assertIn('逾時', result['metadata']['review_error'])

    def test_review_malformed_output_and_unknown_finding_ids_keep_writer(self):
        invalid = review(findings=[{"section": "limitations", "paragraph": 1, "severity": "warning",
                     "message": "Unsupported review link", "source_ids": ["S999"]}])
        for output in ("not json", json.dumps(invalid)):
            with self.subTest(output=output):
                result = self.generate(client_with(Stream(draft()), Stream(output, raw=True)))
                self.assertEqual(result["metadata"]["review_status"], "review_failed")

    def test_safe_writer_errors_and_invalid_credentials_never_reach_model(self):
        for error, expected in ((ReadTimeout("timeout " + KEY), ResearchError),
                                (RuntimeError("provider body " + KEY), ProviderAPIError)):
            with self.subTest(error=type(error).__name__):
                client = client_with(error)
                with self.assertRaises(expected) as caught:
                    self.generate(client)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertEqual(client.chat.completions.create.call_count, 1)
        client = client_with(Stream(draft()))
        with self.assertRaises(ProviderConfigurationError):
            generate_research_workflow("topic", sources(), api_key="", as_of="2026-10-14", client=client)
        client.chat.completions.create.assert_not_called()

    def test_progress_has_only_stages_counts_and_registers_stream_cleanup(self):
        events, cancels = [], []
        first, second = Stream(draft()), Stream(review())
        self.generate(client_with(first, second), on_progress=lambda stage, count: events.append((stage, count)), register_cancel=cancels.append)
        stages = [stage for stage, _ in events]
        self.assertEqual(stages[0], "preparing")
        self.assertEqual(stages[-1], "ready")
        self.assertIn("reviewing", stages)
        self.assertTrue(all(stage == "reviewing" for stage in stages[stages.index("reviewing"):-1]))
        self.assertTrue(all(isinstance(count, int) for _stage, count in events))
        self.assertNotIn(KEY, str(events))
        self.assertEqual(len(cancels), 2)
        self.assertTrue(first.closed and second.closed)

    def test_owned_sdk_clients_use_selected_endpoint_and_are_closed(self):
        client = client_with(Stream(draft()), Stream(review()))
        with patch("src.research_workflow.OpenAI", return_value=client) as factory:
            generate_research_workflow("topic", sources(), api_key=KEY, as_of="2026-10-14")
        self.assertEqual(factory.call_args.kwargs["base_url"], PROVIDERS["deepseek"].base_url)
        self.assertEqual(factory.call_args.kwargs["api_key"], KEY)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        client.close.assert_called_once()
        for call in client.chat.completions.create.call_args_list:
            self.assertEqual(call.kwargs["response_format"], {"type": "json_object"})
            self.assertEqual(call.kwargs["extra_body"], {"thinking": {"type": "disabled"}})

    def test_other_provider_keys_cannot_be_used_by_injected_sdk_client(self):
        # Real SDK construction initializes no network connection.
        from openai import OpenAI
        client = OpenAI(api_key="synthetic-other-provider-key", base_url=PROVIDERS["openai"].base_url)
        try:
            with self.assertRaises(ProviderConfigurationError):
                self.generate(client)
        finally:
            client.close()

    def test_native_claude_adapter_is_used_for_both_stages(self):
        def response(payload):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(payload)))])
        with patch("src.research_workflow.claude_completion", side_effect=[response(draft()), response(review())]) as native:
            result = self.generate(None, provider="anthropic", model="claude-sonnet-5")
        self.assertEqual(result["metadata"]["provider"], "anthropic")
        self.assertEqual(native.call_count, 2)
        for call in native.call_args_list:
            self.assertEqual(call.kwargs["api_key"], KEY)
            self.assertNotIn(KEY, call.kwargs["user"])

    def test_sources_are_not_mutated_and_unselected_database_keys_never_enter_prompt(self):
        evidence = sources()
        evidence[0]["raw_metadata"] = {"api_key": "synthetic-db-secret"}
        previous = copy.deepcopy(evidence)
        client = client_with(Stream(draft()), Stream(review()))
        generate_research_workflow("topic", evidence, api_key=KEY, as_of="2026-10-14", client=client)
        self.assertEqual(evidence, previous)
        self.assertNotIn("synthetic-db-secret", str(client.chat.completions.create.call_args_list))


if __name__ == "__main__":
    unittest.main()
