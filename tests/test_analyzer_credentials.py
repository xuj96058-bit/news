"""Credential routing uses synthetic values and never makes an API request."""

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

with patch("dotenv.load_dotenv"):
    from src.analyzer import analyze_news, get_client


SERVER_KEY = "synthetic-server-credential"
SESSION_KEY = "synthetic-session-credential"
SERVER_URL = "https://synthetic-provider.example/v1"


class AnalyzerCredentialsTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "OPENAI_API_KEY": SERVER_KEY, "OPENAI_BASE_URL": SERVER_URL,
        })
        env.start()
        self.addCleanup(env.stop)
        client = patch("src.analyzer.OpenAI")
        self.client = client.start()
        self.addCleanup(client.stop)

    def test_explicit_credentials_override_environment_without_changing_it(self):
        result = get_client(api_key="  " + SESSION_KEY + "  ", base_url="https://api.deepseek.com")
        self.assertIs(result, self.client.return_value)
        self.client.assert_called_once_with(api_key=SESSION_KEY, base_url="https://api.deepseek.com")
        self.assertEqual(os.environ["OPENAI_API_KEY"], SERVER_KEY)
        self.assertEqual(os.environ["OPENAI_BASE_URL"], SERVER_URL)

    def test_omitted_credentials_preserve_environment_fallback(self):
        get_client()
        self.client.assert_called_once_with(api_key=SERVER_KEY, base_url=SERVER_URL)

    def test_missing_key_fails_before_client_creation(self):
        os.environ.pop("OPENAI_API_KEY", None)
        with self.assertRaises(RuntimeError):
            get_client()
        self.client.assert_not_called()

    def test_explicit_empty_key_does_not_silently_use_server_credential(self):
        for empty in ("", "   "):
            with self.subTest(key=repr(empty)), self.assertRaises(RuntimeError):
                get_client(api_key=empty)
        self.client.assert_not_called()

    def test_analyze_news_forwards_session_credentials_to_client(self):
        data = {
            "extraction": {"event_type": "earnings", "entities": [], "fact": "Synthetic fact",
                           "direction": "neutral", "confidence": 0.8, "reasoning": "Synthetic reasoning"},
            "impacts": [], "summary": "Synthetic summary", "overall_confidence": 0.8,
        }
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)))],
        )
        with patch("src.analyzer.get_client", return_value=client) as get, \
             patch("src.analyzer.post_process_impacts", side_effect=lambda result, **_: result) as process:
            result = analyze_news("Synthetic article", model="synthetic-model", universe_path="synthetic-universe.json",
                                  api_key=SESSION_KEY, base_url="https://api.deepseek.com")
        get.assert_called_once_with(api_key=SESSION_KEY, base_url="https://api.deepseek.com")
        process.assert_called_once_with(result, universe_path="synthetic-universe.json")
        self.assertEqual(result.summary, "Synthetic summary")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "synthetic-model")
        self.assertNotIn(SESSION_KEY, json.dumps(request))
        self.assertEqual(os.environ["OPENAI_API_KEY"], SERVER_KEY)


if __name__ == "__main__":
    unittest.main()
