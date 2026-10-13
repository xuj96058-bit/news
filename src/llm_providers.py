"""Provider choices, isolated credentials, and the native Claude adapter."""

from dataclasses import dataclass
import os
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

import requests

from .analysis_errors import ModelResponseError, ProviderAPIError, ProviderConfigurationError


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    base_url: str
    models: tuple[str, ...]
    key_env: str


PROVIDERS = {
    "deepseek": Provider("deepseek", "DeepSeek", "https://api.deepseek.com",
                         ("deepseek-v4-pro", "deepseek-flash"), "DEEPSEEK_API_KEY"),
    "openai": Provider("openai", "OpenAI", "https://api.openai.com/v1",
                       ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"), "OPENAI_API_KEY"),
    "xai": Provider("xai", "xAI / Grok", "https://api.x.ai/v1",
                    ("grok-4.7",), "XAI_API_KEY"),
    "anthropic": Provider("anthropic", "Anthropic / Claude", "https://api.anthropic.com/v1/messages",
                          ("claude-sonnet-5", "claude-opus-5-5", "claude-haiku-4-5"), "ANTHROPIC_API_KEY"),
    "gemini": Provider("gemini", "Google / Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/",
                       ("gemini-3.8-flash",), "GEMINI_API_KEY"),
}


def get_provider(provider_id: str) -> Provider:
    try:
        return PROVIDERS[provider_id]
    except (KeyError, TypeError):
        raise ProviderConfigurationError("unknown_provider") from None


def _legacy_endpoint_host() -> str | None:
    # Historical CLI installations use OPENAI_API_KEY for DeepSeek. A matching
    # HTTPS origin is required before offering that key to a selected provider.
    configured = os.getenv("OPENAI_BASE_URL", "").strip()
    if not configured:
        return None
    try:
        parsed = urlsplit(configured)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return "unrecognized"
        return parsed.hostname or "unrecognized"
    except ValueError:
        return "unrecognized"


def provider_environment_key(provider_id: str) -> str:
    provider = get_provider(provider_id)
    key = os.getenv(provider.key_env, "").strip()
    legacy_host = _legacy_endpoint_host()
    if legacy_host is None:
        owner = os.getenv("LLM_PROVIDER", "").strip().lower()
        owner = owner if owner in {"openai", "deepseek"} else "openai"
    else:
        owner = {"api.openai.com": "openai", "api.deepseek.com": "deepseek"}.get(legacy_host)
    if provider_id == "openai":
        # OPENAI_API_KEY may belong to an old DeepSeek/custom endpoint setup.
        return key if owner == "openai" else ""
    if key:
        return key
    if provider_id == "deepseek" and owner == "deepseek":
        return os.getenv("OPENAI_API_KEY", "").strip()
    return ""


def validate_provider_model(provider_id: str, model: str) -> str:
    get_provider(provider_id)
    prefixes = {
        "deepseek": r"deepseek-", "openai": r"(?:gpt-|chatgpt-|o[1-9](?:-|$)|ft:(?:gpt-|o[1-9](?:-|:)))",
        "xai": r"grok-", "anthropic": r"claude-", "gemini": r"gemini-",
    }
    if not isinstance(model, str):
        raise ProviderConfigurationError("invalid_model")
    model = model.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", model) or not re.match(prefixes[provider_id], model):
        raise ProviderConfigurationError("invalid_model")
    return model


def claude_completion(*, model: str, api_key: str, system: str, user: str):
    """Return the common response shape without exposing HTTP bodies in errors."""
    try:
        response = requests.post(
            PROVIDERS["anthropic"].base_url,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 4096, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=(10, 90), allow_redirects=False,
        )
    except requests.RequestException:
        raise ProviderAPIError() from None
    try:
        if not 200 <= response.status_code < 300:
            raise ProviderAPIError(response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise ModelResponseError("invalid_json") from None
    finally:
        response.close()
    if not isinstance(data, dict):
        raise ModelResponseError("invalid_shape")
    if data.get("stop_reason") == "max_tokens":
        raise ModelResponseError("truncated")
    blocks = data.get("content")
    if blocks is None or blocks == []:
        raise ModelResponseError("empty_content")
    if not isinstance(blocks, list):
        raise ModelResponseError("invalid_shape")
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ModelResponseError("invalid_shape")
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise ModelResponseError("invalid_shape")
            parts.append(block["text"])
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content="".join(parts)),
    )])
