"""Ollama LLM provider for local model inference."""

import json
import os
from typing import Any, Dict, Iterator

import httpx

from ..base import LLMProvider


class OllamaProvider(LLMProvider):
    # Fixed seed used when temperature == 0 to make generation fully reproducible.
    # Ollama's greedy decode (temperature=0) is already deterministic per-run, but
    # setting an explicit seed eliminates any residual sampling variance across runs.
    _DETERMINISTIC_SEED = 42

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1:8b",
        timeout: int = 600,
    ):
        self.base_url = os.getenv("OLLAMA_BASE_URL", base_url).rstrip("/")
        self.model = os.getenv("OLLAMA_MODEL", model)
        self.timeout = int(os.getenv("OLLAMA_TIMEOUT", str(timeout)))

    def _build_options(self, temperature: float, max_tokens: int, extra: dict) -> dict:
        """Build the Ollama options dict, injecting a fixed seed at temperature=0."""
        options: dict = {"num_predict": max_tokens, "temperature": temperature}
        if temperature == 0.0:
            options["seed"] = self._DETERMINISTIC_SEED
            # top_k=1 + top_p=1 with temperature=0 fully disables sampling
            options["top_k"] = 1
            options["top_p"] = 1.0
        options.update(extra)
        return options

    def _explain_http_error(self, exc: httpx.HTTPStatusError) -> str:
        """Turn an Ollama error response into something the user can act on.

        Ollama puts the real reason in the response body — most often that the model does not
        fit in available memory. Reporting only the status code leaves the user with nothing.
        """
        detail = ""
        try:
            body = exc.response.json()
            detail = body.get("error", "") if isinstance(body, dict) else ""
        except Exception:
            detail = (exc.response.text or "").strip()
        detail = detail[:400]

        lowered = detail.lower()
        if "memory" in lowered or "out of memory" in lowered or "system memory" in lowered:
            return (
                f"The model '{self.model}' does not fit in this machine's memory. "
                "Choose a smaller model in Settings, or use one of the free cloud models. "
                f"(Ollama said: {detail})"
            )
        if "not found" in lowered or "no such model" in lowered:
            return (
                f"The model '{self.model}' is not installed. Pull it from Settings, "
                f"or run: ollama pull {self.model}"
            )
        if detail:
            return f"Ollama could not run '{self.model}': {detail}"
        return (
            f"Ollama returned HTTP {exc.response.status_code} for model '{self.model}'. "
            "Check that Ollama is running and the model is installed."
        )

    def generate(self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": self._build_options(temperature, max_tokens, kwargs),
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(f"{self.base_url}/api/generate", json=payload)
                response.raise_for_status()
                return response.json().get("response", "")
        except httpx.TimeoutException:
            raise RuntimeError(
                f"Ollama generation timed out after {self.timeout}s. Try reducing max_tokens or increasing timeout."
            )
        except httpx.HTTPStatusError as e:
            raise RuntimeError(self._explain_http_error(e))
        except httpx.HTTPError as e:
            raise RuntimeError(f"Could not reach Ollama at {self.base_url}: {e}")

    def stream(self, prompt: str, temperature: float = 0.7, max_tokens: int = 2000, **kwargs) -> Iterator[str]:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "options": self._build_options(temperature, max_tokens, kwargs),
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("POST", f"{self.base_url}/api/generate", json=payload) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if line:
                            try:
                                data = json.loads(line)
                                if "response" in data:
                                    yield data["response"]
                            except json.JSONDecodeError:
                                continue
        except httpx.HTTPStatusError as e:
            raise RuntimeError(self._explain_http_error(e))
        except Exception as e:
            raise RuntimeError(f"Ollama streaming failed: {e}")

    def check_health(self) -> bool:
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.base_url}/api/tags")
                return response.status_code == 200
        except Exception:
            return False

    def get_model_info(self) -> Dict[str, Any]:
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(f"{self.base_url}/api/show", json={"name": self.model})
                response.raise_for_status()
                return {
                    "provider": "ollama",
                    "model": self.model,
                    "base_url": self.base_url,
                    "details": response.json(),
                }
        except Exception as e:
            return {"provider": "ollama", "model": self.model, "error": str(e)}
