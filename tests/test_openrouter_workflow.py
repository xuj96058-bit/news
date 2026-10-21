"""OpenRouter integration with synthetic evidence and local mock transports."""

import inspect
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from openai import OpenAI

# Importing src normally loads .env. These tests never read local credentials.
with patch("dotenv.load_dotenv"):
    from src.analysis_errors import ModelResponseError, ProviderConfigurationError
    from src.analyzer import analyze_news
    from src.event_analysis import analyze_event
    from src.event_store import EventStore
    from src.research import generate_research_report, prepare_sources
    from src.research_analysis import extract_research_observations
    from src.research_workflow import generate_research_workflow
    from src.workbench import organize_comparison
    import test_argumentation as argument_fixtures
    import test_argumentative_workflow as argumentative_fixtures
    import test_event_analysis as event_fixtures
    import test_research as report_fixtures
    import test_research_analysis as observation_fixtures
    import test_research_workflow as workflow_fixtures
    import test_workbench_comparison as workbench_fixtures


KEY = "synthetic-router-credential"
MODELS = ("deepseek/deepseek-v4-pro", "openai/gpt-6.1-sol", "x-ai/grok-4.7",
          "anthropic/claude-sonnet-5", "google/gemini-3.8-flash")
BASE_URL = "https://openrouter.ai/api/v1"
AS_OF = "2026-10-14"


def response(payload, *, finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish_reason, message=SimpleNamespace(content=json.dumps(payload)))])


class OpenRouterWorkflowTests(unittest.TestCase):
    def setUp(self):
        for blocker in (
            patch("dotenv.load_dotenv"),
            patch.dict(os.environ, {}, clear=True),
            patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network call")),
            patch("socket.socket.connect", side_effect=AssertionError("Unexpected network connection")),
        ):
            blocker.start()
            self.addCleanup(blocker.stop)

    def assert_request(self, request, model, *, stream=True, wire=False):
        self.assertEqual(request["model"], model)
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request.get("stream", False), stream)
        self.assertGreaterEqual(request["max_tokens"], 8192)
        for field in ("max_completion_tokens", "temperature", "thinking", "reasoning_effort"):
            self.assertNotIn(field, request)
        if wire:
            self.assertEqual(request["provider"], {"require_parameters": True})
            self.assertNotIn("extra_body", request)
        else:
            self.assertEqual(request["extra_body"], {"provider": {"require_parameters": True}})
        self.assertNotIn(KEY, json.dumps(request))

    def test_sdk_stream_roundtrip_uses_router_endpoint_and_normalized_json_body(self):
        # The installed SDK may use either transport library; both stay local.
        http = __import__("httpx2" if "httpx2" in str(inspect.signature(OpenAI)) else "httpx")
        for model in MODELS:
            with self.subTest(model=model):
                captured = []

                def handle_request(request):
                    self.assertEqual(str(request.url), BASE_URL + "/chat/completions")
                    self.assertEqual(request.headers["authorization"], "Bearer " + KEY)
                    captured.append(json.loads(request.content))
                    common = {"id": "synthetic-router", "object": "chat.completion.chunk", "created": 1, "model": model}
                    events = [
                        {**common, "choices": [{"index": 0, "finish_reason": None, "delta": {}}]},
                        {**common, "choices": [{"index": 0, "finish_reason": None,
                            "delta": {"content": json.dumps(report_fixtures.payload())}}]},
                        {**common, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
                        {**common, "choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}},
                    ]
                    body = ": keep-alive\n\n" + "".join("data: " + json.dumps(event) + "\n\n" for event in events)
                    return http.Response(200, content=(body + "data: [DONE]\n\n").encode(),
                                         headers={"content-type": "text/event-stream"})

                with http.Client(transport=http.MockTransport(handle_request)) as http_client, \
                     OpenAI(api_key=KEY, base_url=BASE_URL, http_client=http_client, max_retries=0) as client:
                    result = generate_research_report("Synthetic topic", prepare_sources([report_fixtures.article()]),
                        api_key=KEY, provider="openrouter", model=model, as_of=AS_OF, client=client)
                self.assertEqual(result["title"], report_fixtures.payload()["title"])
                self.assertEqual(len(captured), 1)
                self.assert_request(captured[0], model, wire=True)

    def test_router_rejects_injected_sdk_client_with_different_key_or_endpoint(self):
        http = __import__("httpx2" if "httpx2" in str(inspect.signature(OpenAI)) else "httpx")
        for endpoint, key in (("https://api.openai.com/v1", KEY), (BASE_URL, "synthetic-other-key")):
            with self.subTest(endpoint=endpoint, same_key=key == KEY), \
                 http.Client(transport=http.MockTransport(lambda _: self.fail("Unexpected request"))) as http_client, \
                 OpenAI(api_key=key, base_url=endpoint, http_client=http_client, max_retries=0) as client:
                with self.assertRaises(ProviderConfigurationError):
                    generate_research_report("Synthetic topic", prepare_sources([report_fixtures.article()]),
                        api_key=KEY, provider="openrouter", model=MODELS[0], as_of=AS_OF, client=client)

    def test_rss_analyzer_uses_router_json_settings_for_all_model_families(self):
        payload = {"extraction": {"event_type": "earnings", "entities": [], "fact": "Synthetic fact",
                   "direction": "neutral", "confidence": 0.8, "reasoning": "Synthetic reason"},
                   "impacts": [], "summary": "Synthetic summary", "overall_confidence": 0.8}
        for model in MODELS:
            with self.subTest(model=model):
                client = MagicMock()
                client.chat.completions.create.return_value = response(payload)
                with patch("src.analyzer.get_client", return_value=client) as factory, \
                     patch("src.analyzer.post_process_impacts", side_effect=lambda result, **_: result):
                    result = analyze_news("Synthetic RSS article", api_key=KEY, provider="openrouter", model=model)
                factory.assert_called_once_with(api_key=KEY, base_url=BASE_URL)
                self.assertEqual(result.model_dump(), payload)
                self.assert_request(client.chat.completions.create.call_args.kwargs, model, stream=False)

    def test_observation_extractor_pins_router_and_keeps_json_stream_timeout(self):
        for model in MODELS:
            with self.subTest(model=model):
                client = MagicMock()
                stream = observation_fixtures.Stream({"observations": [observation_fixtures.row()]})
                client.chat.completions.create.return_value = stream
                with patch("src.research_analysis.OpenAI", return_value=client) as factory:
                    result = extract_research_observations("Battery production", observation_fixtures.sources(),
                        api_key=KEY, provider="openrouter", model=model, as_of=AS_OF)
                factory.assert_called_once_with(api_key=KEY, base_url=BASE_URL, timeout=60.0, max_retries=0)
                self.assertEqual(result["preparation"]["accepted_count"], 1)
                request = client.chat.completions.create.call_args.kwargs
                self.assert_request(request, model)
                self.assertEqual(request["timeout"], 60)
                self.assertTrue(stream.closed)
                client.close.assert_called_once()

    def test_both_report_workflows_route_all_stages_and_keep_timeouts(self):
        for style in ("argumentative", "legacy"):
            for model in MODELS:
                with self.subTest(style=style, model=model):
                    if style == "argumentative":
                        payloads = [argument_fixtures.plan(), argument_fixtures.essay(), argumentative_fixtures.review()]
                        sources = argumentative_fixtures.sources()
                        module = "src.argumentative_workflow.OpenAI"
                    else:
                        payloads = [workflow_fixtures.draft(), workflow_fixtures.review()]
                        sources = workflow_fixtures.sources()
                        module = "src.research_workflow.OpenAI"
                    streams = [workflow_fixtures.Stream(payload) for payload in payloads]
                    client = workflow_fixtures.client_with(*streams)
                    saved = []

                    def save(draft):
                        self.assertEqual(client.chat.completions.create.call_count, len(payloads) - 1)
                        saved.append(draft)

                    with patch(module, return_value=client) as factory:
                        result = generate_research_workflow("Orders and profits", sources,
                            api_key=KEY, provider="openrouter", model=model, as_of=AS_OF,
                            writing_style=style, report_mode=style, retrieve_sources=False, on_draft=save)
                    factory.assert_called_once_with(api_key=KEY, base_url=BASE_URL, timeout=60, max_retries=0)
                    self.assertEqual(client.chat.completions.create.call_count, len(payloads))
                    self.assertEqual(len(saved), 1)
                    self.assertNotEqual(result["metadata"]["review_status"], "review_failed")
                    self.assertEqual(result["metadata"]["provider"], "openrouter")
                    self.assertEqual(result["metadata"]["model"], model)
                    for call in client.chat.completions.create.call_args_list:
                        self.assert_request(call.kwargs, model)
                        self.assertGreater(call.kwargs["timeout"], 0)
                        self.assertLessEqual(call.kwargs["timeout"], 60)
                    self.assertTrue(all(stream.closed for stream in streams))
                    client.close.assert_called_once()
                    self.assertNotIn(KEY, json.dumps(result))

    def test_workbench_comparison_uses_router_and_validates_source_quotes(self):
        article = workbench_fixtures.article("a")
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.return_value = response({"viewpoints": [{
            "article_id": "a", "judgement": "盈利改善", "evidence": article["content"]}]})
        with patch("openai.OpenAI", return_value=client) as factory:
            rows = organize_comparison([article], "盈利", provider="openrouter", model=MODELS[0], api_key=KEY)
        factory.assert_called_once_with(api_key=KEY, base_url=BASE_URL, timeout=90, max_retries=0)
        self.assertEqual(rows[0]["evidence"], article["content"])
        self.assert_request(client.chat.completions.create.call_args.kwargs, MODELS[0], stream=False)
        client.__exit__.assert_called_once()
        self.assertNotIn(KEY, json.dumps(rows))

    def test_manual_event_planner_writer_and_reviewer_use_router_workbench_transport(self):
        model = MODELS[3]
        brief = event_fixtures.brief()
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.side_effect = [response(payload) for payload in (
            event_fixtures.plan(), brief, {"brief": brief, "findings": [], "verdict": "pass"})]
        with EventStore(":memory:") as store, patch("openai.OpenAI", return_value=client) as factory:
            result = analyze_event([event_fixtures.article()], topic="Orders and margins", store=store,
                api_key=KEY, provider="openrouter", model=model)
            cached = analyze_event([event_fixtures.article()], topic="Orders and margins", store=store,
                event_id=result["event_id"], api_key=KEY, provider="openrouter", model=model)
        self.assertEqual(client.chat.completions.create.call_count, 3)
        self.assertEqual(factory.call_count, 3)
        for call in factory.call_args_list:
            self.assertEqual(call.kwargs, dict(api_key=KEY, base_url=BASE_URL, timeout=90, max_retries=0))
        for call in client.chat.completions.create.call_args_list:
            self.assert_request(call.kwargs, model, stream=False)
        self.assertEqual(result["metadata"]["api_call_count"], 3)
        self.assertNotEqual(result["metadata"]["review_status"], "review_failed")
        self.assertEqual(cached["id"], result["id"])
        self.assertTrue(cached["metadata"]["cached"])
        self.assertNotIn(KEY, json.dumps(result))

    def test_citation_repair_preserves_router_settings_and_remaining_timeout(self):
        streams = [report_fixtures.FakeStream(json.dumps(payload)) for payload in (
            report_fixtures.payload(executive_findings="Uncited factual paragraph."), report_fixtures.payload())]
        client = workflow_fixtures.client_with(*streams)
        result = generate_research_report("Synthetic topic", prepare_sources([report_fixtures.article()]),
            api_key=KEY, provider="openrouter", model=MODELS[0], as_of=AS_OF, client=client)
        self.assertTrue(result["metadata"]["citation_repaired"])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        for call in client.chat.completions.create.call_args_list:
            self.assert_request(call.kwargs, MODELS[0])
        repair = client.chat.completions.create.call_args_list[1].kwargs
        self.assertGreater(repair["timeout"], 0)
        self.assertLessEqual(repair["timeout"], 60)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_error_stream_terminal_never_publishes_even_complete_json(self):
        for reason in ("error", "content_filter"):
            with self.subTest(reason=reason):
                stream = report_fixtures.FakeStream(json.dumps(report_fixtures.payload()), reason=reason)
                client = workflow_fixtures.client_with(stream)
                with self.assertRaises(ModelResponseError):
                    generate_research_report("Synthetic topic", prepare_sources([report_fixtures.article()]),
                        api_key=KEY, provider="openrouter", model=MODELS[0], as_of=AS_OF, client=client)
                client.chat.completions.create.assert_called_once()
                self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
