"""Embedding providers, called directly over HTTP.

Replaces langchain-ollama and langchain-openai. Both are thin wrappers over one endpoint,
and they pull in the wider LangChain dependency tree for it.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

_BATCH = 32
_TIMEOUT = 120


class Embedder(Protocol):
    """What the store needs from an embedding provider."""

    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class OllamaEmbedder:
    """Embeddings from a local Ollama server."""

    def __init__(self, model: str = "nomic-embed-text", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._dimension = 0

    @property
    def dimension(self) -> int:
        if not self._dimension:
            self._dimension = len(self.embed_query("dimension probe"))
        return self._dimension

    def _post(self, texts: list[str]) -> list[list[float]]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            # /api/embed is the current endpoint and takes a batch; /api/embeddings is the
            # older single-input one, still present on installations that have not updated.
            try:
                resp = client.post(
                    f"{self.base_url}/api/embed", json={"model": self.model, "input": texts}
                )
                resp.raise_for_status()
                return resp.json()["embeddings"]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise RuntimeError(self._explain(exc)) from exc
            out = []
            for text in texts:
                resp = client.post(
                    f"{self.base_url}/api/embeddings", json={"model": self.model, "prompt": text}
                )
                resp.raise_for_status()
                out.append(resp.json()["embedding"])
            return out

    def _explain(self, exc: httpx.HTTPStatusError) -> str:
        detail = ""
        try:
            detail = exc.response.json().get("error", "")
        except Exception:
            detail = (exc.response.text or "").strip()[:300]
        if "not found" in detail.lower():
            return (
                f"The embedding model '{self.model}' is not installed. "
                f"Pull it from Settings, or run: ollama pull {self.model}"
            )
        return f"Could not create embeddings with '{self.model}': {detail}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            vectors.extend(self._post(texts[start : start + _BATCH]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._post([text])[0]


class OpenAIEmbedder:
    """Embeddings from the OpenAI API, for users who prefer a hosted model."""

    def __init__(self, model: str = "text-embedding-3-small", api_key: str = ""):
        self.model = model
        self.api_key = api_key
        self._dimension = 0

    @property
    def dimension(self) -> int:
        if not self._dimension:
            self._dimension = len(self.embed_query("dimension probe"))
        return self._dimension

    def _post(self, texts: list[str]) -> list[list[float]]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in data]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            vectors.extend(self._post(texts[start : start + _BATCH]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._post([text])[0]


def build_embedder(config: dict, default_model: str, default_host: str) -> Embedder:
    """Create the embedder named by the user's settings."""
    provider = config.get("embed_provider", "ollama")
    model = config.get("embed_model") or default_model
    if provider == "openai":
        return OpenAIEmbedder(model=model, api_key=config.get("openai_api_key", ""))
    return OllamaEmbedder(model=model, base_url=config.get("ollama_host") or default_host)
