"""
ZettaBrain Trial Proxy — Rate-limited gateway for first-run experience.

Uses Groq's free tier for fast inference. Holds the API key server-side
and rate-limits each ZettaBrain installation to N requests.

Environment variables:
  GROQ_API_KEY   — Your Groq API key (free at console.groq.com)
  MAX_REQUESTS   — Max requests per install (default: 25)
  REDIS_URL      — Optional Redis for persistent rate limiting
"""

import json
import logging
import os
import time
from collections import defaultdict
from typing import Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger("trial-proxy")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="ZettaBrain Trial Proxy", version="3.0.0")

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "25"))

MODEL_PREFERENCE = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
    "mixtral-8x7b-32768",
]

_resolved_model: Optional[str] = None
_model_resolved_at: float = 0
MODEL_CACHE_TTL = 3600

_usage: dict = defaultdict(lambda: {"count": 0, "first_seen": 0})


async def _discover_model() -> str:
    """Find a working Groq model by trying the preference list."""
    global _resolved_model, _model_resolved_at

    if _resolved_model and (time.time() - _model_resolved_at) < MODEL_CACHE_TTL:
        return _resolved_model

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        for model in MODEL_PREFERENCE:
            try:
                resp = await client.post(
                    GROQ_URL,
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 5,
                    },
                )
                if resp.status_code == 200:
                    _resolved_model = model
                    _model_resolved_at = time.time()
                    log.info(f"Resolved trial model: {model}")
                    return model
                log.info(f"Model {model} returned {resp.status_code}, trying next")
            except Exception as e:
                log.info(f"Model {model} failed: {e}, trying next")

    _resolved_model = MODEL_PREFERENCE[0]
    _model_resolved_at = time.time()
    return _resolved_model


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
    return {"status": "ok", "model": model, "backend": "groq"}


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

    model = await _discover_model()
    body["model"] = model
    if body.get("max_tokens", 0) > 2048:
        body["max_tokens"] = 2048

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Upstream error: {response.text[:300]}"
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
