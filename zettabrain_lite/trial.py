"""
ZettaBrain Lite — Trial gateway for first-run experience.

Provides a rate-limited trial using a proxy so users can try ZettaBrain
immediately without configuring API keys or downloading a local model.

The proxy holds the real API key server-side and auto-discovers the best
available Gemini model. Each installation gets a unique install_id and
is limited to TRIAL_MAX_REQUESTS requests.
"""

import hashlib
import json
import platform
import uuid
from typing import Optional

import httpx

from .config import BASE_DIR

TRIAL_STATE_FILE = BASE_DIR / "trial_state.json"
TRIAL_MAX_REQUESTS = 25
TRIAL_PROXY_URL = "https://zettabrain-trial-proxy-38374664161.us-central1.run.app/v1"
TRIAL_PROVIDER = "trial"

_cached_model: Optional[str] = None


def _get_trial_model() -> str:
    """Ask the proxy which Gemini model is currently available."""
    global _cached_model
    if _cached_model:
        return _cached_model
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{TRIAL_PROXY_URL}/model")
            resp.raise_for_status()
            _cached_model = resp.json().get("model", "gemini-2.5-flash")
    except Exception:
        _cached_model = "gemini-2.5-flash"
    return _cached_model


def _get_install_id() -> str:
    state = _load_trial_state()
    if "install_id" not in state:
        raw = f"{platform.node()}-{uuid.getnode()}-{uuid.uuid4().hex[:8]}"
        state["install_id"] = hashlib.sha256(raw.encode()).hexdigest()[:24]
        _save_trial_state(state)
    return state["install_id"]


def _load_trial_state() -> dict:
    if TRIAL_STATE_FILE.exists():
        try:
            return json.loads(TRIAL_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_trial_state(state: dict):
    TRIAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRIAL_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_trial_usage() -> dict:
    state = _load_trial_state()
    used = state.get("requests_used", 0)
    return {
        "install_id": _get_install_id(),
        "requests_used": used,
        "requests_remaining": max(0, TRIAL_MAX_REQUESTS - used),
        "max_requests": TRIAL_MAX_REQUESTS,
        "exhausted": used >= TRIAL_MAX_REQUESTS,
    }


def increment_trial_usage():
    state = _load_trial_state()
    state["requests_used"] = state.get("requests_used", 0) + 1
    _save_trial_state(state)


def is_trial_available() -> bool:
    usage = get_trial_usage()
    return not usage["exhausted"]


def trial_generate(prompt: str, temperature: float = 0.7, max_tokens: int = 1024) -> str:
    usage = get_trial_usage()
    if usage["exhausted"]:
        remaining_msg = (
            "Your free trial has ended. To keep using ZettaBrain, choose one of these free options:\n\n"
            "FASTEST (30 seconds):\n"
            "  1. Get a free Groq API key at https://console.groq.com\n"
            "  2. Paste it in Settings > Cloud Providers > Groq API Key\n"
            "  3. Select a Groq model from the dropdown — 500+ tokens/sec, no GPU needed\n\n"
            "FREE FOREVER (needs 5min + decent hardware):\n"
            "  - Go to Settings > Pull LLM Model and pull 'phi4-mini' for a lightweight local model\n\n"
            "OTHER FREE OPTIONS:\n"
            "  - Google Gemini: free at https://aistudio.google.com/apikey\n"
            "  - OpenRouter: free models at https://openrouter.ai"
        )
        raise RuntimeError(remaining_msg)

    install_id = _get_install_id()
    model = _get_trial_model()

    try:
        with httpx.Client(timeout=120) as client:
            response = client.post(
                f"{TRIAL_PROXY_URL}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "X-ZettaBrain-Install-ID": install_id,
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            response.raise_for_status()
            result = response.json()["choices"][0]["message"]["content"]
            increment_trial_usage()
            return result

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            raise RuntimeError("Trial rate limit reached. Pull a local model or add an API key.")
        raise RuntimeError(f"Trial request failed: {e.response.text[:200]}")
    except httpx.ConnectError:
        raise RuntimeError(
            "Cannot reach trial server. You can use ZettaBrain offline by pulling a local model: "
            "Settings > Pull LLM Model"
        )
    except Exception as e:
        raise RuntimeError(f"Trial request failed: {e}")


def get_trial_model_entry() -> Optional[dict]:
    if not is_trial_available():
        return None
    usage = get_trial_usage()
    remaining = usage["requests_remaining"]
    model = _get_trial_model()
    return {
        "id": f"trial:{model}",
        "label": f"Try Free ({remaining} left) - Gemini Flash",
        "provider": "trial",
    }
