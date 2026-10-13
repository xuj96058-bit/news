"""Five-provider routing and credential isolation without external requests."""

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import requests

with patch("dotenv.load_dotenv"):
    from src.analyzer import analyze_news
    from src.analysis_errors import ModelResponseError, ProviderAPIError, ProviderConfigurationError, analysis_error_message
    from src.llm_providers import PROVIDERS, provider_environment_key, validate_provider_model


KEY = "synthetic-session-key"
OTHER_KEY = "synthetic-other-provider-key"
PAYLOAD = {
    "extraction": {"event_type": "earnings", "entities": [], "fact": "Synthetic fact",
                   "direction": "neutral", "confidence": 0.8, "reasoning": "Synthetic reason"},
    "impacts": [], "summary": "Synthetic summary", "overall_confidence": 0.8,
}


class ProviderEnvironmentTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_dedicated_keys_are_isolated_for_each_provider(self):
        for identifier, provider in PROVIDERS.items():
            with self.subTest(provider=identifier), patch.dict(os.environ, {
                provider.key_env: KEY, "OPENAI_BASE_URL": "https://api.openai.com/v1",
            }, clear=True):
                self.assertEqual(provider_environment_key(identifier), KEY)
                for other in PROVIDERS:
                    if other != identifier:
                        self.assertEqual(provider_environment_key(other), "")

    def test_legacy_deepseek_key_is_allowed_for_matching_endpoint(self):
        for endpoint in ("https://api.deepseek.com", "https://api.deepseek.com/v1", "https://api.deepseek.com:443"):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {"OPENAI_API_KEY": KEY}, clear=True):
                os.environ["OPENAI_BASE_URL"] = endpoint
                self.assertEqual(provider_environment_key("deepseek"), KEY)
                self.assertEqual(provider_environment_key("openai"), "")

    def test_unconfigured_endpoint_defaults_key_owner_to_openai_only(self):
        os.environ["OPENAI_API_KEY"] = KEY
        self.assertEqual(provider_environment_key("openai"), KEY)
        self.assertEqual(provider_environment_key("deepseek"), "")

    def test_explicit_provider_assigns_legacy_key_when_endpoint_is_unset(self):
        for owner in ("openai", "deepseek"):
            with self.subTest(owner=owner), patch.dict(os.environ, {
                "OPENAI_API_KEY": KEY, "LLM_PROVIDER": owner,
            }, clear=True):
                self.assertEqual(provider_environment_key(owner), KEY)
                other = "deepseek" if owner == "openai" else "openai"
                self.assertEqual(provider_environment_key(other), "")

    def test_explicit_endpoint_takes_priority_over_provider_label(self):
        os.environ.update(OPENAI_API_KEY=KEY, OPENAI_BASE_URL="https://api.deepseek.com", LLM_PROVIDER="openai")
        self.assertEqual(provider_environment_key("deepseek"), KEY)
        self.assertEqual(provider_environment_key("openai"), "")
        os.environ.update(OPENAI_BASE_URL="https://custom.example", LLM_PROVIDER="deepseek")
        self.assertEqual(provider_environment_key("deepseek"), "")
        self.assertEqual(provider_environment_key("openai"), "")

    def test_legacy_key_is_not_sent_to_deepseek_from_other_or_malformed_urls(self):
        endpoints = (
            "https://api.openai.com/v1", "https://custom.example/v1", "https://api.deepseek.com.evil.example",
            "https://api.deepseek.com@evil.example", "http://api.deepseek.com", "https://api.deepseek.com:444",
            "https://api.deepseek.com:bad", "https://[invalid", "https://user@api.deepseek.com",
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {
                "OPENAI_API_KEY": KEY, "OPENAI_BASE_URL": endpoint,
            }, clear=True):
                self.assertEqual(provider_environment_key("deepseek"), "")

    def test_openai_does_not_use_key_from_legacy_deepseek_or_custom_endpoint(self):
        for endpoint in ("https://api.deepseek.com", "https://custom.example", "http://api.openai.com"):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {
                "OPENAI_API_KEY": OTHER_KEY, "OPENAI_BASE_URL": endpoint,
            }, clear=True):
                self.assertEqual(provider_environment_key("openai"), "")

    def test_dedicated_deepseek_key_takes_priority_and_is_trimmed(self):
        os.environ.update(DEEPSEEK_API_KEY="  " + KEY + "  ", OPENAI_API_KEY=OTHER_KEY,
                          OPENAI_BASE_URL="https://custom.example")
        self.assertEqual(provider_environment_key("deepseek"), KEY)

    def test_current_models_and_safe_custom_models_validate(self):
        for identifier, provider in PROVIDERS.items():
            for model in provider.models + (provider.models[0] + "-custom",):
                self.assertEqual(validate_provider_model(identifier, model), model)
        for model in ("o1", "o3-mini", "o4-mini", "ft:gpt-4o-mini:synthetic:custom:123"):
            self.assertEqual(validate_provider_model("openai", model), model)

    def test_mismatched_unsafe_and_unknown_models_are_rejected(self):
        for model in ("claude-sonnet-5", "gpt-6 bad", "gpt-6\nmalicious", "gpt-/path", "", None, "gpt-" + "a" * 160):
            with self.subTest(model=model), self.assertRaises(ProviderConfigurationError):
                validate_provider_model("openai", model)
        with self.assertRaises(ProviderConfigurationError):
            provider_environment_key("unknown")


class ProviderRoutingTests(unittest.TestCase):
    def setUp(self):
        self.patch(patch.dict(os.environ, {
            "OPENAI_API_KEY": OTHER_KEY, "OPENAI_BASE_URL": "https://custom.example/v1",
            "LLM_MODEL": "synthetic-unrelated-model",
        }, clear=True))
        self.client = MagicMock()
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps(PAYLOAD)),
        )])
        self.get_client = self.patch(patch("src.analyzer.get_client", return_value=self.client))
        self.process = self.patch(patch("src.analyzer.post_process_impacts", side_effect=lambda result, **_: result))
        self.post = self.patch(patch("src.llm_providers.requests.post", side_effect=AssertionError("Unexpected HTTP request")))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def claude_response(self, data=None, status=200):
        response = MagicMock()
        response.status_code = status
        response.json.return_value = data if data is not None else {
            "content": [{"type": "text", "text": json.dumps(PAYLOAD)}], "stop_reason": "end_turn",
        }
        self.post.side_effect = None
        self.post.return_value = response
        return response

    def test_compatible_providers_pin_official_endpoint_and_parameters(self):
        for identifier in ("deepseek", "openai", "xai", "gemini"):
            provider = PROVIDERS[identifier]
            with self.subTest(provider=identifier):
                self.get_client.reset_mock()
                self.client.reset_mock()
                result = analyze_news("Synthetic news", provider=identifier, api_key="  " + KEY + "  ",
                                      base_url="https://untrusted.example", universe_path="synthetic-universe")
                self.assertEqual(result.model_dump(), PAYLOAD)
                self.get_client.assert_called_once_with(api_key=KEY, base_url=provider.base_url)
                request = self.client.chat.completions.create.call_args.kwargs
                self.assertEqual(request["model"], provider.models[0])
                self.assertEqual(request["response_format"], {"type": "json_object"})
                self.assertNotIn(KEY, json.dumps(request))
                self.assertNotIn(OTHER_KEY, json.dumps(request))
                if identifier == "openai":
                    self.assertEqual(request["max_completion_tokens"], 8192)
                    self.assertNotIn("max_tokens", request)
                    self.assertNotIn("temperature", request)
                else:
                    self.assertIn("max_tokens", request)
                if identifier == "deepseek":
                    self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})
                else:
                    self.assertNotIn("extra_body", request)
        self.post.assert_not_called()
        self.assertEqual(os.environ["OPENAI_API_KEY"], OTHER_KEY)
        self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://custom.example/v1")

    def test_missing_or_explicit_empty_key_never_uses_another_provider(self):
        for identifier in PROVIDERS:
            for key in (None, "", "  "):
                with self.subTest(provider=identifier, key=key), self.assertRaises(ProviderConfigurationError):
                    analyze_news("Synthetic news", provider=identifier, api_key=key)
        self.get_client.assert_not_called()
        self.post.assert_not_called()

    def test_explicit_provider_uses_its_environment_key(self):
        os.environ["GEMINI_API_KEY"] = KEY
        analyze_news("Synthetic news", provider="gemini")
        self.get_client.assert_called_once_with(api_key=KEY, base_url=PROVIDERS["gemini"].base_url)

    def test_invalid_model_is_rejected_before_client_or_http_request(self):
        for identifier in PROVIDERS:
            with self.subTest(provider=identifier), self.assertRaises(ProviderConfigurationError):
                analyze_news("Synthetic news", provider=identifier, api_key=KEY, model="unrelated-model")
        self.get_client.assert_not_called()
        self.post.assert_not_called()

    def test_claude_native_messages_and_text_blocks(self):
        encoded = json.dumps(PAYLOAD)
        response = self.claude_response({"content": [
            {"type": "thinking", "thinking": "Synthetic hidden thinking"},
            {"type": "text", "text": encoded[:10]}, {"type": "text", "text": encoded[10:]},
        ], "stop_reason": "end_turn"})
        result = analyze_news("Synthetic news", provider="anthropic", api_key=KEY, base_url="https://untrusted.example")
        self.assertEqual(result.model_dump(), PAYLOAD)
        self.get_client.assert_not_called()
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args, (PROVIDERS["anthropic"].base_url,))
        arguments = self.post.call_args.kwargs
        self.assertEqual(arguments["headers"]["x-api-key"], KEY)
        self.assertEqual(arguments["headers"]["anthropic-version"], "2023-06-01")
        self.assertFalse(arguments["allow_redirects"])
        self.assertEqual(arguments["timeout"], (10, 90))
        self.assertEqual(arguments["json"]["model"], "claude-sonnet-5")
        self.assertIn("system", arguments["json"])
        self.assertNotIn("response_format", arguments["json"])
        self.assertNotIn(KEY, json.dumps(arguments["json"]))
        response.close.assert_called_once()

    def test_claude_empty_malformed_and_truncated_responses(self):
        cases = (
            ({"content": []}, "empty_content"),
            ({"content": None}, "empty_content"),
            ({"content": [{"type": "thinking"}]}, "empty_content"),
            ({"content": [{"type": "text", "text": "  "}]}, "empty_content"),
            ({"content": [{"type": "text", "text": None}]}, "invalid_shape"),
            ({"content": "not a list"}, "invalid_shape"),
            ({"content": [None]}, "invalid_shape"),
            ({"content": [{"type": "text", "text": "synthetic-secret-not-json"}]}, "invalid_json"),
            ({"content": [{"type": "text", "text": "{}"}]}, "invalid_schema"),
            ({"content": [], "stop_reason": "max_tokens"}, "truncated"),
            ([], "invalid_shape"),
        )
        for body, reason in cases:
            with self.subTest(reason=reason, body=body):
                response = self.claude_response(body)
                with self.assertRaises(ModelResponseError) as caught:
                    analyze_news("Synthetic news", provider="anthropic", api_key=KEY)
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIn("synthetic-secret", str(caught.exception))
                response.close.assert_called_once()

    def test_claude_http_status_errors_and_redirects_never_expose_response(self):
        for status in (301, 302, 307, 308, 400, 401, 403, 429, 500):
            with self.subTest(status=status):
                response = self.claude_response(status=status)
                response.text = KEY + " sensitive-provider-response"
                with self.assertRaises(ProviderAPIError) as caught:
                    analyze_news("Synthetic news", provider="anthropic", api_key=KEY)
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn("sensitive-provider-response", analysis_error_message(caught.exception))
                response.json.assert_not_called()
                response.close.assert_called_once()

    def test_claude_invalid_http_json_has_safe_error(self):
        response = self.claude_response()
        response.json.side_effect = ValueError(KEY + " sensitive-provider-response")
        with self.assertRaises(ModelResponseError) as caught:
            analyze_news("Synthetic news", provider="anthropic", api_key=KEY)
        self.assertEqual(caught.exception.reason, "invalid_json")
        self.assertNotIn(KEY, str(caught.exception))
        response.close.assert_called_once()

    def test_claude_timeout_does_not_echo_request_details(self):
        self.post.side_effect = requests.Timeout(KEY + " sensitive-request-details")
        with self.assertRaises(ProviderAPIError) as caught:
            analyze_news("Synthetic news", provider="anthropic", api_key=KEY)
        self.assertIsNone(caught.exception.status_code)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn("sensitive-request-details", analysis_error_message(caught.exception))


if __name__ == "__main__":
    unittest.main()
