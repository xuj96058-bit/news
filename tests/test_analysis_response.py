"""Response diagnostics use synthetic payloads and an in-memory HTTP transport."""

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from src.analyzer import AnalysisResult, analyze_news, parse_analysis_response
    from src.analysis_errors import ModelResponseError, analysis_error_message


def valid_payload():
    return {
        "extraction": {
            "event_type": "earnings", "entities": [], "fact": "Synthetic fact",
            "direction": "neutral", "confidence": 0.8, "reasoning": "Synthetic reasoning",
        },
        "impacts": [], "summary": "Synthetic summary", "overall_confidence": 0.8,
    }


def response(content, finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason,
    )])


class AnalysisResponseTests(unittest.TestCase):
    def assert_response_error(self, value, reason):
        with self.assertRaises(ModelResponseError) as caught:
            parse_analysis_response(value)
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def test_valid_object_returns_typed_result(self):
        parsed = parse_analysis_response(response(json.dumps(valid_payload())))
        self.assertIsInstance(parsed, AnalysisResult)
        self.assertEqual(parsed.model_dump(), valid_payload())

    def test_no_choices_is_empty_content(self):
        for choices in ([], None):
            with self.subTest(choices=choices):
                self.assert_response_error(SimpleNamespace(choices=choices), "empty_content")

    def test_null_empty_and_whitespace_content_are_empty_content(self):
        for content in (None, "", "  \n\t  "):
            with self.subTest(content=content):
                self.assert_response_error(response(content), "empty_content")

    def test_nonstring_content_is_rejected_before_json_loading(self):
        for content in ({"text": "synthetic"}, ["synthetic"], 7):
            with self.subTest(content=content):
                self.assert_response_error(response(content), "invalid_shape")

    def test_malformed_json_has_safe_diagnostic(self):
        sensitive_text = "synthetic-sensitive-provider-output"
        error = self.assert_response_error(response('{"summary": "' + sensitive_text), "invalid_json")
        self.assertNotIn(sensitive_text, str(error))

    def test_json_null_arrays_and_scalars_are_invalid_shapes(self):
        for content in ("null", "[]", '"synthetic"', "7", "true"):
            with self.subTest(content=content):
                self.assert_response_error(response(content), "invalid_shape")

    def test_schema_failure_has_safe_diagnostic(self):
        sensitive_text = "synthetic-sensitive-schema-input"
        payload = valid_payload()
        payload["overall_confidence"] = sensitive_text
        error = self.assert_response_error(response(json.dumps(payload)), "invalid_schema")
        self.assertNotIn(sensitive_text, str(error))

    def test_missing_schema_fields_are_invalid_schema(self):
        self.assert_response_error(response("{}"), "invalid_schema")

    def test_length_finish_reason_is_truncated_even_with_valid_json(self):
        for content in (json.dumps(valid_payload()), None, '{"summary":'):
            with self.subTest(content=content):
                self.assert_response_error(response(content, finish_reason="length"), "truncated")

    def test_authentication_and_type_errors_have_distinct_safe_messages(self):
        secret = "synthetic-sensitive-error-input"
        unauthorized = RuntimeError(secret + " raw-provider-body")
        unauthorized.status_code = 401
        auth_message = analysis_error_message(unauthorized)
        type_message = analysis_error_message(TypeError(secret + " raw-provider-body"))
        self.assertIn("API key 驗證失敗", auth_message)
        self.assertIn("TypeError", type_message)
        self.assertIn("不是 API key 驗證結果", type_message)
        for message in (auth_message, type_message):
            self.assertNotIn(secret, message)
            self.assertNotIn("raw-provider-body", message)

    def test_installed_sdk_roundtrip_uses_only_mock_transport(self):
        # OpenAI 3 uses httpx2; older supported SDKs use httpx.
        from openai import OpenAI
        import inspect
        transport_module = "httpx2" if "httpx2" in str(inspect.signature(OpenAI)) else "httpx"
        http = __import__(transport_module)
        requests = []

        def handle_request(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-diagnostic-key")
            requests.append(json.loads(request.content))
            return http.Response(200, json={
                "id": "synthetic-completion", "object": "chat.completion", "created": 1,
                "model": "deepseek-v4-pro",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(valid_payload()),
                }}],
            })

        with patch.dict(os.environ, {}, clear=True), \
             http.Client(transport=http.MockTransport(handle_request)) as http_client, \
             OpenAI(api_key="synthetic-diagnostic-key", base_url="https://api.deepseek.com",
                    http_client=http_client, max_retries=0) as client, \
             patch("src.analyzer.get_client", return_value=client) as get_client, \
             patch("src.analyzer.post_process_impacts", side_effect=lambda result, **_: result):
            result = analyze_news("Synthetic article", model="deepseek-v4-pro",
                                  api_key="synthetic-diagnostic-key", base_url="https://api.deepseek.com")

        self.assertEqual(result.model_dump(), valid_payload())
        get_client.assert_called_once_with(api_key="synthetic-diagnostic-key", base_url="https://api.deepseek.com")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["response_format"], {"type": "json_object"})
        self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("synthetic-diagnostic-key", json.dumps(requests[0]))


if __name__ == "__main__":
    unittest.main()
