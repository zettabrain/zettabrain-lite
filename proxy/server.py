"""
ZettaBrain Trial Proxy — Rate-limited gateway via Vertex AI.

Uses the native Vertex AI generateContent endpoint, which bills to
your Google Cloud credits. On Cloud Run, authentication is automatic
via the service account (Application Default Credentials).

Environment variables:
  GOOGLE_CLOUD_PROJECT — GCP project ID (auto-set on Cloud Run)
  VERTEX_LOCATION      — Region (default: us-central1)
  MAX_REQUESTS         — Max requests per install (default: 25)
  REDIS_URL            — Optional Redis for persistent rate limiting
"""

import json
import logging
import os
import time
from collections import defaultdict
from typing import Optional

import google.auth
import google.auth.transport.requests
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger("trial-proxy")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="ZettaBrain Trial Proxy", version="3.0.0")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "zettabrain")
LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
VERTEX_BASE = (
    f"https://{LOCATION}-aiplatform.googleapis.com/v1"
    f"/projects/{PROJECT_ID}/locations/{LOCATION}/publishers/google/models"
)
MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "25"))

MODEL_PREFERENCE = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
]

_credentials = None
_resolved_model: Optional[str] = None
_model_resolved_at: float = 0
MODEL_CACHE_TTL = 3600

_usage: dict = defaultdict(lambda: {"count": 0, "first_seen": 0})


def _get_auth_headers() -> dict:
    global _credentials
    if _credentials is None:
        _credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    auth_req = google.auth.transport.requests.Request()
    _credentials.refresh(auth_req)
    return {
        "Authorization": f"Bearer {_credentials.token}",
        "Content-Type": "application/json",
    }


def _openai_to_vertex(body: dict) -> dict:
    """Convert OpenAI chat format to Vertex AI generateContent format."""
    contents = []
    system_text = None

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        text = msg.get("content", "")

        if role == "system":
            system_text = text
            continue

        vertex_role = "model" if role == "assistant" else "user"
        contents.append({
            "role": vertex_role,
            "parts": [{"text": text}],
        })

    vertex_body = {
        "contents": contents,
        "generationConfig": {
            "temperature": body.get("temperature", 0.7),
            "maxOutputTokens": min(body.get("max_tokens", 2048), 2048),
        },
    }

    if system_text:
        vertex_body["systemInstruction"] = {
            "parts": [{"text": system_text}]
        }

    return vertex_body


def _vertex_to_openai(vertex_resp: dict, model: str) -> dict:
    """Convert Vertex AI response to OpenAI chat completion format."""
    text = ""
    candidates = vertex_resp.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts:
            text = parts[0].get("text", "")

    return {
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "model": model,
    }


async def _call_vertex(body: dict) -> tuple:
    """Call Vertex AI, trying models from preference list. Returns (response_dict, model)."""
    global _resolved_model, _model_resolved_at

    headers = _get_auth_headers()
    vertex_body = _openai_to_vertex(body)

    if _resolved_model and (time.time() - _model_resolved_at) < MODEL_CACHE_TTL:
        url = f"{VERTEX_BASE}/{_resolved_model}:generateContent"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers, json=vertex_body)
            if resp.status_code == 200:
                return resp.json(), _resolved_model
            log.warning(f"Cached model {_resolved_model} failed ({resp.status_code}): {resp.text[:200]}")
            _resolved_model = None

    last_resp = None
    async with httpx.AsyncClient(timeout=120) as client:
        for model in MODEL_PREFERENCE:
            url = f"{VERTEX_BASE}/{model}:generateContent"
            resp = await client.post(url, headers=headers, json=vertex_body)
            last_resp = resp
            if resp.status_code == 200:
                _resolved_model = model
                _model_resolved_at = time.time()
                log.info(f"Resolved trial model: {model}")
                return resp.json(), model
            log.info(f"Model {model} returned {resp.status_code}: {resp.text[:150]}")

    raise HTTPException(
        status_code=last_resp.status_code if last_resp else 500,
        detail=f"No models available. Last error: {last_resp.text[:300] if last_resp else 'unknown'}"
    )


def _get_usage(install_id: str) -> dict:
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            import redis
            r = redis.from_url(redis_url)
            data = r.get(f"zb:trial:{install_id}")
            if data:
                return json.loads(data)
            return {"count": 0, "first_seen": 0}
        except Exception:
            pass
    return _usage[install_id]


def _incr_usage(install_id: str):
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            import redis
            r = redis.from_url(redis_url)
            key = f"zb:trial:{install_id}"
            data = r.get(key)
            usage = json.loads(data) if data else {"count": 0, "first_seen": 0}
            usage["count"] += 1
            if usage["first_seen"] == 0:
                usage["first_seen"] = int(time.time())
            r.set(key, json.dumps(usage))
            return
        except Exception:
            pass
    _usage[install_id]["count"] += 1
    if _usage[install_id]["first_seen"] == 0:
        _usage[install_id]["first_seen"] = int(time.time())


@app.get("/health")
async def health():
    model = _resolved_model or "not yet resolved"
    return {"status": "ok", "model": model, "backend": "vertex-ai"}


@app.get("/v1/model")
async def get_model():
    model = _resolved_model or MODEL_PREFERENCE[0]
    return {"model": model}


@app.post("/v1/chat/completions")
async def proxy_chat(request: Request):
    install_id = request.headers.get("X-ZettaBrain-Install-ID", "unknown")

    if not install_id or install_id == "unknown":
        raise HTTPException(status_code=400, detail="Missing X-ZettaBrain-Install-ID header")

    usage = _get_usage(install_id)
    if usage["count"] >= MAX_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail=f"Trial limit reached ({MAX_REQUESTS} requests). "
                   f"Pull a local model or add your own API key to continue."
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if body.get("max_tokens", 0) > 2048:
        body["max_tokens"] = 2048

    try:
        vertex_result, model = await _call_vertex(body)
        openai_result = _vertex_to_openai(vertex_result, model)

        _incr_usage(install_id)
        openai_result["_trial"] = {
            "requests_used": usage["count"] + 1,
            "requests_remaining": MAX_REQUESTS - usage["count"] - 1,
            "model": model,
        }
        return JSONResponse(content=openai_result)

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
