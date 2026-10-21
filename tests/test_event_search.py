"""Synthetic xAI Responses results; no network access or paid calls."""

import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from openai import OpenAI

from src.analysis_errors import ModelResponseError, ProviderAPIError, ProviderConfigurationError
from src.event_search import search_event_evidence


KEY = "synthetic-search-key"


def result(paragraphs=None, *, status="completed"):
    paragraphs = paragraphs or [("Orders improved; margin remains uncertain.", "https://issuer.example/results")]
    text, annotations = "", []
    for index, (claim, url) in enumerate(paragraphs, 1):
        text += claim
        start = len(text)
        text += f"[[{index}]]({url})"
        annotations.append({"type": "url_citation", "url": url, "title": str(index),
                            "start_index": start, "end_index": len(text)})
        text += "\n\n"
    return {"status": status, "error": None,
            "output": [{"type": "web_search_call", "status": "completed"},
                       {"type": "message", "status": "completed", "content": [
                           {"type": "output_text", "text": text, "annotations": annotations}]}],
            "usage": {"input_tokens": 300, "output_tokens": 80, "total_tokens": 380,
                      "output_tokens_details": {"reasoning_tokens": 40},
                      "server_side_tool_usage_details": {
                          "web_search_calls": 1, "x_search_calls": 1,
                          "x_posts_fetched": 4, "x_users_fetched": 0}}}


def fake_client(payload=None):
    client = SimpleNamespace(responses=SimpleNamespace(create=MagicMock(return_value=payload or result())))
    return client


class EventSearchTests(unittest.TestCase):
    def test_supported_limits_endpoint_and_tool_choices(self):
        client = fake_client()
        with patch("src.event_search.OpenAI", return_value=client) as constructor:
            client.close = MagicMock()
            found = search_event_evidence("查找訂單與毛利原始公告", api_key="  " + KEY + "  ")
        constructor.assert_called_once_with(api_key=KEY, base_url="https://api.x.ai/v1",
                                           max_retries=0, timeout=60)
        request = client.responses.create.call_args.kwargs
        self.assertEqual(request["tools"], [{"type": "web_search"}])
        self.assertEqual(request["extra_body"], {"max_turns": 2})
        self.assertEqual(request["max_output_tokens"], 1500)
        self.assertFalse(request["parallel_tool_calls"])
        self.assertFalse(request["store"])
        self.assertEqual(request["tool_choice"], "required")
        self.assertNotIn("max_tool_calls", request)
        self.assertNotIn(KEY, json.dumps(request))
        self.assertEqual(found["sources"][0]["content_scope"], "search_summary")
        self.assertFalse(found["sources"][0]["independently_verified"])
        self.assertIsNone(found["sources"][0]["published_at"])
        self.assertNotIn(KEY, json.dumps(found))
        client.close.assert_called_once()

    def test_optional_x_and_actual_usage_are_preserved(self):
        client = fake_client()
        found = search_event_evidence("Find original release", api_key=KEY, include_x=True,
                                      max_turns=1, max_output_tokens=700, client=client)
        self.assertEqual(client.responses.create.call_args.kwargs["tools"],
                         [{"type": "web_search"}, {"type": "x_search"}])
        self.assertEqual(found["usage"]["server_side_tool_usage_details"]["x_posts_fetched"], 4)
        self.assertEqual(found["usage"]["output_tokens_details"]["reasoning_tokens"], 40)

    def test_each_source_only_contains_its_cited_paragraph(self):
        payload = result([("Orders improved.", "https://issuer.example/orders"),
                          ("Margins fell.", "https://issuer.example/margins")])
        payload["output"][1]["content"][0]["annotations"].append(
            copy.deepcopy(payload["output"][1]["content"][0]["annotations"][0]))
        found = search_event_evidence("Find filings", api_key=KEY, client=fake_client(payload))
        self.assertEqual(len(found["sources"]), 2)
        self.assertEqual(found["sources"][0]["content"], "Orders improved.")
        self.assertEqual(found["sources"][1]["content"], "Margins fell.")
        self.assertTrue(all(not source["independent_evidence"] for source in found["sources"]))

    def test_generated_urls_or_encountered_url_lists_are_not_evidence(self):
        payload = result()
        payload["output"][1]["content"][0]["annotations"] = []
        payload["citations"] = ["https://issuer.example/results"]
        found = search_event_evidence("Find filings", api_key=KEY, client=fake_client(payload))
        self.assertEqual(found["sources"], [])
        self.assertTrue(any("未將生成文字" in warning for warning in found["warnings"]))

    def test_bad_positions_and_unsafe_urls_are_not_evidence(self):
        for values in ({"start_index": None}, {"start_index": -1}, {"end_index": 99999},
                       {"start_index": True}, {"url": "javascript:alert(1)"},
                       {"url": "https://user:password@example.com/path"}):
            payload = result()
            payload["output"][1]["content"][0]["annotations"][0].update(values)
            with self.subTest(values=values):
                found = search_event_evidence("Find filings", api_key=KEY, client=fake_client(payload))
                self.assertEqual(found["sources"], [])

    def test_inline_urls_do_not_authorize_additional_sources(self):
        payload = result([("Orders improved; see [untrusted](https://invented.example) or https://invented.example/a.",
                           "https://issuer.example/results?api_key=secret&year=2026#secret")])
        found = search_event_evidence("Find filings", api_key=KEY, client=fake_client(payload))
        self.assertEqual(len(found["sources"]), 1)
        self.assertEqual(found["sources"][0]["url"], "https://issuer.example/results?year=2026")
        self.assertNotIn("https://invented", found["sources"][0]["content"])
        self.assertNotIn("secret", json.dumps(found))

    def test_source_and_text_budgets(self):
        payload = result([("Synthetic evidence " + "x" * 3500, f"https://issuer.example/{i}") for i in range(8)])
        found = search_event_evidence("Find filings", api_key=KEY, client=fake_client(payload))
        self.assertEqual(len(found["sources"]), 6)
        self.assertLessEqual(sum(len(s["content"]) for s in found["sources"]), 18000)

    def test_incomplete_and_other_invalid_results_raise_safe_errors(self):
        for status in ("incomplete", "failed", "in_progress", None):
            with self.subTest(status=status), self.assertRaises(ModelResponseError) as caught:
                search_event_evidence("Find filings", api_key=KEY, client=fake_client(result(status=status)))
            self.assertEqual(caught.exception.usage["input_tokens"], 300)
        payload = result()
        payload["error"] = {"message": KEY}
        with self.assertRaises(ModelResponseError) as caught:
            search_event_evidence("Find filings", api_key=KEY, client=fake_client(payload))
        self.assertNotIn(KEY, str(caught.exception))

    def test_message_incomplete_and_oversized_text_rejected(self):
        payload = result()
        payload["output"][1]["status"] = "incomplete"
        with self.assertRaises(ModelResponseError):
            search_event_evidence("Find filings", api_key=KEY, client=fake_client(payload))
        payload = result([("x" * 48001, "https://issuer.example/results")])
        with self.assertRaises(ModelResponseError):
            search_event_evidence("Find filings", api_key=KEY, client=fake_client(payload))

    def test_provider_error_is_sanitized_and_not_retried(self):
        client = fake_client()
        error = RuntimeError("Authorization: " + KEY)
        error.status_code = 429
        client.responses.create.side_effect = error
        with self.assertRaises(ProviderAPIError) as caught:
            search_event_evidence("Find filings", api_key=KEY, client=client)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(client.responses.create.call_count, 1)

    def test_validation_happens_before_any_request(self):
        invalid = [{"query": ""}, {"query": "x" * 1501}, {"query": None},
                   {"api_key": ""}, {"api_key": "abc\nsecret"},
                   {"model": "gpt-synthetic"}, {"include_x": "yes"},
                   {"max_turns": 0}, {"max_turns": 3}, {"max_turns": True},
                   {"max_output_tokens": 0}, {"max_output_tokens": 2001},
                   {"max_output_tokens": True}]
        for override in invalid:
            client = fake_client()
            kwargs = {"query": "Find filings", "api_key": KEY, "client": client, **override}
            with self.subTest(override=override), self.assertRaises(ValueError):
                search_event_evidence(**kwargs)
            client.responses.create.assert_not_called()

    def test_injected_real_client_cannot_route_key_to_another_endpoint(self):
        for endpoint, key in (("https://other.example/v1", KEY), ("https://api.x.ai/v1", "other-key")):
            client = OpenAI(api_key=key, base_url=endpoint)
            try:
                with self.assertRaises(ProviderConfigurationError):
                    search_event_evidence("Find filings", api_key=KEY, client=client)
            finally:
                client.close()

    def test_missing_usage_remains_unknown_and_arbitrary_metadata_is_not_saved(self):
        payload = result()
        payload["usage"] = {"input_tokens": True, "output_tokens": -1,
                            "request_headers": KEY, "server_side_tool_usage_details": {
                                "x_posts_fetched": "4", "secret": KEY}}
        found = search_event_evidence("Find filings", api_key=KEY, client=fake_client(payload))
        self.assertEqual(found["usage"], {})

    def test_sdk_object_response_shape(self):
        def objectify(value):
            if isinstance(value, dict):
                return SimpleNamespace(**{k: objectify(v) for k, v in value.items()})
            return [objectify(item) for item in value] if isinstance(value, list) else value
        found = search_event_evidence("Find filings", api_key=KEY,
                                      client=fake_client(objectify(result())))
        self.assertEqual(len(found["sources"]), 1)
        self.assertEqual(found["usage"]["input_tokens"], 300)


if __name__ == "__main__":
    unittest.main()
