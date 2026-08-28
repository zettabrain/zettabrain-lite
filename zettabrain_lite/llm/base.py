"""Base LLM provider interface for direct generation (streaming-capable)."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator


class LLMProvider(ABC):

    @abstractmethod
    def generate(
        self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs
    ) -> str:
        pass

    @abstractmethod
    def stream(
        self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs
    ) -> Iterator[str]:
        pass

    @abstractmethod
    def check_health(self) -> bool:
        pass

    @abstractmethod
    def get_model_info(self) -> Dict[str, Any]:
        pass
