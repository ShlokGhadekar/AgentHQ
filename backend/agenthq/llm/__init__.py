"""LLM access: one small interface (LLMClient), one client per provider, and a scripted fake."""

from agenthq.config import Settings
from agenthq.llm.anthropic_client import AnthropicClient
from agenthq.llm.base import LLMClient, LLMError, LLMResponse, Message, ToolCall, ToolSpec
from agenthq.llm.fake import FakeLLMClient
from agenthq.llm.openai_client import GROQ_BASE_URL, OpenAICompatibleClient

__all__ = [
    "FakeLLMClient", "LLMClient", "LLMError", "LLMResponse", "Message", "ToolCall", "ToolSpec", "make_llm_client",
]

# Used when neither agents.yaml nor LLM_MODEL names a model.
DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "anthropic": "claude-sonnet-5-5",
}


def make_llm_client(settings: Settings) -> LLMClient:
    """Build the real client for the configured provider. Fails fast if its API key is missing."""
    model = settings.llm_model or DEFAULT_MODELS[settings.llm_provider]
    if settings.llm_provider == "groq":
        if not settings.groq_api_key:
            raise ValueError("LLM_PROVIDER=groq but GROQ_API_KEY is not set. Add it to .env.")
        return OpenAICompatibleClient(
            settings.groq_api_key, model, settings.llm_timeout_s, settings.llm_max_tokens, base_url=GROQ_BASE_URL
        )
    if not settings.anthropic_api_key:
        raise ValueError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. Add it to .env.")
    return AnthropicClient(settings.anthropic_api_key, model, settings.llm_timeout_s, settings.llm_max_tokens)
