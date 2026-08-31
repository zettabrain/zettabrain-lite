"""
ZettaBrain Trial Proxy — Rate-limited gateway for first-run experience.

Deploy this on Railway/Fly.io/Render. It holds the real Gemini API key
server-side and rate-limits each ZettaBrain installation to N requests.

The proxy auto-discovers available Gemini models on startup so it never
breaks when Google deprecates a model.

Environment variables:
  GEMINI_API_KEY  — Your Google Gemini API key
  MAX_REQUESTS    — Max requests per install (default: 25)
  REDIS_URL       — Optional Redis URL for persistent rate limiting
                    (falls back to in-memory dict if not set)
"""

import json
import logging
import os
import time
from collections import defaultdict

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger("trial-proxy")

app = FastAPI(title="ZettaBrain Trial Proxy", version="1.1.0")

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "25"))

MODEL_PREFERENCE = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-pro",
]

_resolved_model: str | None = None
_model_resolved_at: float = 0
MODEL_CACHE_TTL = 3600

_usage: dict = defaultdict(lambda: {"count": 0, "first_seen": 0})


async def _discover_model() -> str:
    """Query Gemini API for available models and pick the best flash model."""
    global _resolved_model, _model_resolved_at

    if _resolved_model and (time.time() - _model_resolved_at) < MODEL_CACHE_TTL:
        return _resolved_model

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                GEMINI_MODELS_URL,
                params={"key": GEMINI_API_KEY},
            )
            resp.raise_for_status()
            models_data = resp.json()

        available = set()
        for m in models_data.get("models", []):
            name = m.get("name", "")
            if name.startswith("models/"):
                name = name[len("models/"):]
            available.add(name)

        for preferred in MODEL_PREFERENCE:
            if preferred in available:
                _resolved_model = preferred
                _model_resolved_at = time.time()
                log.info(f"Resolved trial model: {preferred}")
                return preferred

        flash_models = sorted(
            [n for n in available if "flash" in n.lower()],
            reverse=True,
        )
        if flash_models:
            _resolved_model = flash_models[0]
            _model_resolved_at = time.time()
            log.info(f"Resolved trial model (fallback): {flash_models[0]}")
            return flash_models[0]

        if available:
            pick = sorted(available)[-1]
            _resolved_model = pick
            _model_resolved_at = time.time()
            log.info(f"Resolved trial model (last resort): {pick}")
            return pick

    except Exception as e:
        log.warning(f"Model discovery failed: {e}")
        if _resolved_model:
            return _resolved_model

    return "gemini-2.5-flash"


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
    return {"status": "ok", "model": model}


@app.get("/v1/model")
async def get_model():
    """Returns the currently resolved model — called by the client on startup."""
    model = await _discover_model()
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

    model = await _discover_model()
    body["model"] = model
    if body.get("max_tokens", 0) > 2048:
        body["max_tokens"] = 2048

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                GEMINI_URL,
                headers={
                    "Authorization": f"Bearer {GEMINI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Upstream error: {response.text[:200]}"
                )

            _incr_usage(install_id)
            result = response.json()
            result["_trial"] = {
                "requests_used": usage["count"] + 1,
                "requests_remaining": MAX_REQUESTS - usage["count"] - 1,
                "model": model,
            }
            return JSONResponse(content=result)

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
