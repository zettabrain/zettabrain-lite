"""Claude/Anthropic LLM provider for document generation."""

import os
from typing import Any, Dict, Iterator, Optional

from ..base import LLMProvider


class ClaudeProvider(LLMProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
    ):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("API key not found. Set ANTHROPIC_API_KEY or configure in Settings.")
        self.model = model or "claude-sonnet-4-6"
        self.timeout = timeout

    def generate(self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)
        message = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    def stream(self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs) -> Iterator[str]:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)
        with client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                yield text

    def check_health(self) -> bool:
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=self.api_key, timeout=10)
            client.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": "test"}],
            )
            return True
        except Exception:
            return False

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "provider": "claude",
            "model": self.model,
            "api_key_set": bool(self.api_key),
        }
