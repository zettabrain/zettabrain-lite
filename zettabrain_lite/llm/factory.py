"""LLM factory — create providers from config for both RAG chat and generation."""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseLLM

from .base import LLMProvider
from .providers.openai_compatible import PROVIDER_REGISTRY

CLOUD_PROVIDERS = {
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
}

_llm_cache: Dict[Tuple, BaseLLM] = {}
_embed_cache: Dict[Tuple, Embeddings] = {}


def get_chat_llm(
    provider: str,
    model: str,
    ollama_host: Optional[str] = None,
    api_key: Optional[str] = None,
) -> BaseLLM:
    """Create a LangChain LLM for RAG chat (cached)."""
    if provider == "ollama":
        cache_key = (provider, model, ollama_host)
    else:
        cache_key = (provider, model, api_key[:8] if api_key else None)

    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    if provider == "ollama":
        if not ollama_host:
            raise ValueError("ollama_host is required for Ollama provider")
        from langchain_ollama import OllamaLLM
        llm = OllamaLLM(model=model, base_url=ollama_host, temperature=0.0, num_predict=1024)

    elif provider in CLOUD_PROVIDERS:
        if not api_key:
            env_var = f"{provider.upper()}_API_KEY"
            api_key = os.getenv(env_var)
        if not api_key:
            raise ValueError(f"API key required for {provider}. Configure in Settings.")
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=CLOUD_PROVIDERS[provider],
            temperature=0.0,
            max_tokens=1024,
        )

    elif provider == "openai":
        if not api_key:
            raise ValueError("API key required for OpenAI. Configure in Settings.")
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model=model, api_key=api_key, temperature=0.0, max_tokens=1024)

    elif provider == "claude":
        if not api_key:
            raise ValueError("API key required for Claude. Configure in Settings.")
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model=model, api_key=api_key, max_tokens=1024)

    elif provider == "gemini":
        if not api_key:
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("API key required for Gemini. Configure in Settings.")
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            temperature=0.0,
            max_tokens=1024,
        )

    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")

    _llm_cache[cache_key] = llm
    return llm


def get_embeddings(
    provider: str,
    model: str,
    ollama_host: Optional[str] = None,
    openai_key: Optional[str] = None,
) -> Embeddings:
    """Create a LangChain embeddings instance (cached)."""
    if provider == "ollama":
        cache_key = (provider, model, ollama_host)
    elif provider == "openai":
        cache_key = (provider, model, openai_key[:8] if openai_key else None)
    else:
        raise ValueError(f"Unsupported embedding provider: {provider}")

    if cache_key in _embed_cache:
        return _embed_cache[cache_key]

    if provider == "ollama":
        if not ollama_host:
            raise ValueError("ollama_host is required for Ollama embeddings")
        from langchain_ollama import OllamaEmbeddings
        embeddings = OllamaEmbeddings(model=model, base_url=ollama_host)
    elif provider == "openai":
        if not openai_key:
            raise ValueError("API key required for OpenAI embeddings. Configure in Settings.")
        from langchain_openai import OpenAIEmbeddings
        embeddings = OpenAIEmbeddings(model=model, api_key=openai_key)

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
    from ..config import get_setting, OLLAMA_HOST

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
            model=model or get_setting("gemini_model") or "gemini-2.0-flash-lite",
            **kwargs,
        )

    else:
        raise ValueError(
            f"Unknown provider: {provider_name}. "
            f"Supported: ollama, groq, together, cerebras, openrouter, fireworks, openai, claude, gemini"
        )
