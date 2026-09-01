"""Unified OpenAI-compatible provider — covers Groq, Together AI, Cerebras, OpenRouter, Fireworks, OpenAI."""

import json
import os
from typing import Any, Dict, Iterator, Optional

import httpx

from ..base import LLMProvider

PROVIDER_REGISTRY = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "default_model": "llama-3.1-8b-instant",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "env_key": "TOGETHER_API_KEY",
        "default_model": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "env_key": "CEREBRAS_API_KEY",
        "default_model": "llama3.1-8b",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "meta-llama/llama-3.1-8b-instruct:free",
    },
    "fireworks": {
        "base_url": "https://api.fireworks.ai/inference/v1",
        "env_key": "FIREWORKS_API_KEY",
        "default_model": "accounts/fireworks/models/llama-v3p1-8b-instruct",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
    },
}


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        provider_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
    ):
        provider_name = provider_name or "groq"
        registry = PROVIDER_REGISTRY.get(provider_name, {})

        self.provider_name = provider_name
        self.base_url = (base_url or registry.get("base_url", "https://api.groq.com/openai/v1")).rstrip("/")

        env_key = registry.get("env_key", "GROQ_API_KEY")
        self.api_key = api_key or os.getenv(env_key)
        if not self.api_key:
            raise ValueError(f"API key not found. Set {env_key} environment variable or configure in Settings.")

        self.model = model or registry.get("default_model", "llama-3.1-8b-instant")
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.provider_name == "openrouter":
            h["HTTP-Referer"] = "https://zettabrain.ai"
            h["X-Title"] = "ZettaBrain Lite"
        return h

    def generate(self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise RuntimeError(f"Invalid API key for {self.provider_name}")
            elif e.response.status_code == 429:
                raise RuntimeError(f"{self.provider_name} rate limit exceeded")
            raise RuntimeError(f"{self.provider_name} API error: {e.response.text[:300]}")
        except httpx.TimeoutException:
            raise RuntimeError(f"{self.provider_name} request timed out after {self.timeout}s")

    def stream(self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs) -> Iterator[str]:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                data = json.loads(line[6:])
                                delta = data["choices"][0].get("delta", {})
                                if "content" in delta:
                                    yield delta["content"]
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"{self.provider_name} streaming error: {e.response.text[:300]}")
        except Exception as e:
            raise RuntimeError(f"{self.provider_name} streaming failed: {e}")

    def check_health(self) -> bool:
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(
                    f"{self.base_url}/models",
                    headers=self._headers(),
                )
                return response.status_code == 200
        except Exception:
            return False

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "provider": self.provider_name,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_set": bool(self.api_key),
        }
