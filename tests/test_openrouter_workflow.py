"""OpenRouter request integration with synthetic sources and mock transports only."""

import inspect
import json
import unittest
from unittest.mock import MagicMock, patch

from openai import OpenAI

from src.analysis_errors import ProviderConfigurationError
from src.research import generate_research_report, prepare_sources
from src.research_analysis import extract_research_observations
from src.research_workflow import generate_research_workflow
import test_argumentation as argument_fixtures
import test_deep_research_workflow as deep_fixtures
import test_research as report_fixtures
import test_research_analysis as observation_fixtures
import test_research_design as evidence_fixtures
import test_research_workflow as workflow_fixtures


KEY = "synthetic-router-credential"
MODELS = ("x-ai/grok-4.7", "openai/gpt-6.1-sol", "google/gemini-3.8-flash")
BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterWorkflowTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network call"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def assert_request(self, request, model, *, wire=False):
        self.assertEqual(request["model"], model)
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertTrue(request["stream"])
        self.assertGreaterEqual(request["max_tokens"], 8192)
        self.assertNotIn("max_completion_tokens", request)
        self.assertNotIn("temperature", request)
        self.assertNotIn("thinking", request)
        self.assertNotIn("reasoning_effort", request)
        if wire:
            self.assertEqual(request["provider"], {"require_parameters": True})
            self.assertNotIn("extra_body", request)
        else:
            self.assertEqual(request["extra_body"], {"provider": {"require_parameters": True}})
        self.assertNotIn(KEY, json.dumps(request))

    def test_sdk_stream_roundtrip_uses_router_endpoint_and_normalized_json_body(self):
        # The installed OpenAI SDK may use either transport library; both stay local.
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
                        api_key=KEY, provider="openrouter", model=model, as_of=deep_fixtures.AS_OF, client=client)
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
                        api_key=KEY, provider="openrouter", model=MODELS[0], as_of=deep_fixtures.AS_OF, client=client)

    def test_observation_extractor_pins_router_and_keeps_json_stream_timeout(self):
        for model in MODELS:
            with self.subTest(model=model):
                client = MagicMock()
                stream = observation_fixtures.Stream({"observations": [observation_fixtures.row()]})
                client.chat.completions.create.return_value = stream
                with patch("src.research_analysis.OpenAI", return_value=client) as factory:
                    result = extract_research_observations("Battery production", observation_fixtures.sources(),
                        api_key=KEY, provider="openrouter", model=model, as_of=deep_fixtures.AS_OF)
                factory.assert_called_once_with(api_key=KEY, base_url=BASE_URL, timeout=60.0, max_retries=0)
                self.assertEqual(result["preparation"]["accepted_count"], 1)
                request = client.chat.completions.create.call_args.kwargs
                self.assert_request(request, model)
                self.assertEqual(request["timeout"], 60)
                self.assertTrue(stream.closed)
                client.close.assert_called_once()

    def test_both_deep_writers_route_every_stage_with_same_model_and_key(self):
        for style in ("argumentative", "legacy"):
            for model in MODELS:
                with self.subTest(style=style, model=model):
                    source_rows = deep_fixtures.sources()
                    numeric = evidence_fixtures.numeric(id="E3", requirement_id="R2")
                    source_rows[0]["content"] += " " + numeric["quote"]
                    research_design = deep_fixtures.design()
                    research_design["requirements"].append({"id": "R2", "question_id": "Q1",
                        "description": "Revenue figure", "kind": "numeric", "critical": False})
                    research_design["methods"] = ["comparison"]
                    evidence = deep_fixtures.evidence()
                    evidence["items"].append(numeric)
                    client, streams, payloads = MagicMock(), [], []

                    def respond(**kwargs):
                        packet = json.loads(kwargs["messages"][1]["content"])
                        payloads.append(packet)
                        if "available_sources" in packet:
                            value = research_design
                        elif "previous_items" in packet:
                            value = evidence
                        elif "paragraphs" in packet:
                            value = {"claims": [{"section": p["section"], "paragraph": p["paragraph"],
                                "status": "supported", "evidence_ids": ["E1", "E2"], "calculation_ids": [],
                                "reason": "Synthetic mock comparison."} for p in packet["paragraphs"]],
                                "summary": "Synthetic audit."}
                        elif "evidence_items" in packet and "requirements" in packet:
                            value = {"requests": [], "limitations": ["Only one revenue period is supplied."]}
                        elif "writer_draft" in packet:
                            value = {"draft": argument_fixtures.essay() if style == "argumentative" else workflow_fixtures.draft(),
                                "findings": [], "summary": "Synthetic review."}
                            if style == "argumentative":
                                value["quality"] = {**{key: 3 for key in ("relevance", "evidence", "reasoning", "readability")}, "verdict": "pass"}
                        elif style == "legacy":
                            value = workflow_fixtures.draft()
                        elif "argument_plan" in packet:
                            value = argument_fixtures.essay()
                        else:
                            value = argument_fixtures.plan()
                        stream = workflow_fixtures.Stream(value)
                        streams.append(stream)
                        return stream

                    client.chat.completions.create.side_effect = respond
                    with patch("src.deep_research_workflow.OpenAI", return_value=client) as factory:
                        result = generate_research_workflow(deep_fixtures.TOPIC, source_rows,
                            api_key=KEY, provider="openrouter", model=model, as_of=deep_fixtures.AS_OF,
                            deep_research=True, writing_style=style,
                            report_mode="argumentative" if style == "argumentative" else "gap", retrieve_sources=False)
                    factory.assert_called_once_with(api_key=KEY, base_url=BASE_URL, timeout=60, max_retries=0)
                    self.assertNotEqual(result["metadata"]["review_status"], "review_failed", result["metadata"].get("review_error"))
                    stages = {row["stage"] for row in result["metadata"]["research_execution"]["stages"]}
                    self.assertTrue({"designing", "organizing", "calculating", "writing", "auditing", "reviewing"} <= stages, stages)
                    if style == "argumentative":
                        self.assertIn("planning", stages)
                    self.assertEqual(result["metadata"]["research_execution"]["api_call_count"], len(payloads))
                    self.assertTrue(any("writer_draft" in packet for packet in payloads))
                    for call in client.chat.completions.create.call_args_list:
                        self.assert_request(call.kwargs, model)
                        self.assertGreater(call.kwargs["timeout"], 0)
                    self.assertTrue(all(stream.closed for stream in streams))
                    client.close.assert_called_once()
                    self.assertNotIn(KEY, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
