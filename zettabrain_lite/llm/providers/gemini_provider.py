"""Google Gemini provider using the Gemini REST API (OpenAI-compatible endpoint)."""

import json
import os
from typing import Any, Dict, Iterator, Optional

import httpx

from ..base import LLMProvider

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_DEFAULT_MODEL = "gemini-2.0-flash-lite"


class GeminiProvider(LLMProvider):

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
    ):
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Google API key not found. Set GEMINI_API_KEY or configure in Settings."
            )
        self.model = model or GEMINI_DEFAULT_MODEL
        self.base_url = GEMINI_BASE_URL
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def generate(
        self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs
    ) -> str:
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
                raise RuntimeError("Invalid Google API key")
            elif e.response.status_code == 429:
                raise RuntimeError("Gemini rate limit exceeded. Try again later or use a local model.")
            raise RuntimeError(f"Gemini API error: {e.response.text[:300]}")
        except httpx.TimeoutException:
            raise RuntimeError(f"Gemini request timed out after {self.timeout}s")

    def stream(
        self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs
    ) -> Iterator[str]:
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
            raise RuntimeError(f"Gemini streaming error: {e.response.text[:300]}")
        except Exception as e:
            raise RuntimeError(f"Gemini streaming failed: {e}")

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
            "provider": "gemini",
            "model": self.model,
            "base_url": self.base_url,
            "api_key_set": bool(self.api_key),
        }
