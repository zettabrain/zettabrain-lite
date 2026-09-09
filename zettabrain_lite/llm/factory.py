"""LLM factory — create providers from config for both RAG chat and generation."""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from .base import LLMProvider

CLOUD_PROVIDERS = {
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
}

_llm_cache: Dict[Tuple, "_ChatAdapter"] = {}
_embed_cache: Dict[Tuple, object] = {}

# Chat answers are short and must be reproducible.
_CHAT_TEMPERATURE = 0.0
_CHAT_MAX_TOKENS = 1024


class _ChatAdapter:
    """Gives an LLMProvider the .invoke(prompt) -> str call the chat routes expect.

    Chat used LangChain LLMs while generation used these providers, so every provider was
    implemented twice. One implementation now serves both.
    """

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    def invoke(self, prompt: str) -> str:
        return self.provider.generate(
            prompt, temperature=_CHAT_TEMPERATURE, max_tokens=_CHAT_MAX_TOKENS
        )

    def stream(self, prompt: str):
        return self.provider.stream(
            prompt, temperature=_CHAT_TEMPERATURE, max_tokens=_CHAT_MAX_TOKENS
        )


def get_chat_llm(
    provider: str,
    model: str,
    ollama_host: Optional[str] = None,
    api_key: Optional[str] = None,
) -> "_ChatAdapter":
    """Create the LLM used for RAG chat (cached)."""
    if provider == "ollama":
        cache_key = (provider, model, ollama_host)
    else:
        cache_key = (provider, model, api_key[:8] if api_key else None)

    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    kwargs: dict = {"provider_name": provider, "model": model}
    if provider == "ollama":
        if not ollama_host:
            raise ValueError("ollama_host is required for Ollama provider")
        kwargs["base_url"] = ollama_host
    elif api_key:
        kwargs["api_key"] = api_key

    adapter = _ChatAdapter(create_generation_provider(**kwargs))
    _llm_cache[cache_key] = adapter
    return adapter


def get_embeddings(
    provider: str,
    model: str,
    ollama_host: Optional[str] = None,
    openai_key: Optional[str] = None,
):
    """Create an embeddings instance (cached)."""
    if provider == "ollama":
        cache_key = (provider, model, ollama_host)
    elif provider == "openai":
        cache_key = (provider, model, openai_key[:8] if openai_key else None)
    else:
        raise ValueError(f"Unsupported embedding provider: {provider}")

    if cache_key in _embed_cache:
        return _embed_cache[cache_key]

    from ..embeddings import OllamaEmbedder, OpenAIEmbedder

    if provider == "ollama":
        if not ollama_host:
            raise ValueError("ollama_host is required for Ollama embeddings")
        embeddings = OllamaEmbedder(model=model, base_url=ollama_host)
    elif provider == "openai":
        if not openai_key:
            raise ValueError("API key required for OpenAI embeddings. Configure in Settings.")
        embeddings = OpenAIEmbedder(model=model, api_key=openai_key)

    _embed_cache[cache_key] = embeddings
    return embeddings


def create_generation_provider(
    provider_name: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    **kwargs,
) -> LLMProvider:
    """Create a direct LLM provider for document generation (streaming support)."""
    from ..config import OLLAMA_HOST, get_setting

    provider_name = provider_name or get_setting("llm_provider") or "ollama"

    if provider_name == "ollama":
        from .providers.ollama import OllamaProvider

        return OllamaProvider(
            base_url=base_url or get_setting("ollama_host") or OLLAMA_HOST,
            model=model or get_setting("llm_model") or "llama3.1:8b",
            **kwargs,
        )

    elif provider_name in ("groq", "together", "cerebras", "openrouter", "fireworks", "openai"):
        from .providers.openai_compatible import OpenAICompatibleProvider

        resolved_key = api_key or get_setting(f"{provider_name}_api_key")
        resolved_model = model or get_setting(f"{provider_name}_model")
        oai_kwargs = {"provider_name": provider_name}
        if resolved_key:
            oai_kwargs["api_key"] = resolved_key
        if resolved_model:
            oai_kwargs["model"] = resolved_model
        if base_url:
            oai_kwargs["base_url"] = base_url
        oai_kwargs.update(kwargs)
        return OpenAICompatibleProvider(**oai_kwargs)

    elif provider_name in ("claude", "anthropic"):
        from .providers.claude_provider import ClaudeProvider

        return ClaudeProvider(
            api_key=api_key or get_setting("anthropic_api_key"),
            model=model or get_setting("claude_model") or "claude-sonnet-4-6",
            **kwargs,
        )

    elif provider_name == "gemini":
        from .providers.gemini_provider import GeminiProvider

        return GeminiProvider(
            api_key=api_key or get_setting("gemini_api_key"),
            model=model or get_setting("gemini_model") or "gemini-3.5-flash-lite",
            **kwargs,
        )

    else:
        raise ValueError(
            f"Unknown provider: {provider_name}. "
            f"Supported: ollama, groq, together, cerebras, openrouter, fireworks, openai, claude, gemini"
        )
