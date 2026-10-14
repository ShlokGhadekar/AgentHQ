"""LLM access: one small interface (LLMClient), one client per provider, and a scripted fake."""

from agenthq.config import Settings
from agenthq.llm.anthropic_client import AnthropicClient
from agenthq.llm.base import LLMClient, LLMError, LLMResponse, Message, ToolCall, ToolSpec
from agenthq.llm.fake import FakeLLMClient
from agenthq.llm.openai_client import OpenAICompatibleClient

__all__ = [
    "FakeLLMClient", "LLMClient", "LLMError", "LLMResponse", "Message", "ToolCall", "ToolSpec", "make_llm_client",
]

# Used when neither agents.yaml nor LLM_MODEL names a model.
DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "cerebras": "gpt-oss-120b",
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-sonnet-5-5",
}

# Providers that speak the OpenAI Chat Completions format, so one client class serves them all.
OPENAI_COMPATIBLE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
}


def make_llm_client(settings: Settings, provider: str | None = None) -> LLMClient:
    """Build the client for a provider (default: LLM_PROVIDER). Fails fast if its API key is missing."""
    provider = provider or settings.llm_provider
    # LLM_MODEL overrides the default model of the default provider only.
    model = settings.llm_model if provider == settings.llm_provider and settings.llm_model else DEFAULT_MODELS[provider]
    key = getattr(settings, f"{provider}_api_key")
    if not key:
        raise ValueError(f"The {provider} provider needs {provider.upper()}_API_KEY. Add it to .env.")
    if provider in OPENAI_COMPATIBLE_URLS:
        return OpenAICompatibleClient(key, model, settings.llm_timeout_s, settings.llm_max_tokens,
                                      base_url=OPENAI_COMPATIBLE_URLS[provider])
    return AnthropicClient(key, model, settings.llm_timeout_s, settings.llm_max_tokens)
