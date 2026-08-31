"""
ZettaBrain Trial Proxy — Rate-limited gateway for first-run experience.

Deploy this on Railway/Fly.io/Render. It holds the real Gemini API key
server-side and rate-limits each ZettaBrain installation to N requests.

Environment variables:
  GEMINI_API_KEY  — Your Google Gemini API key
  MAX_REQUESTS    — Max requests per install (default: 5)
  REDIS_URL       — Optional Redis URL for persistent rate limiting
                    (falls back to in-memory dict if not set)
"""

import json
import os
import time
from collections import defaultdict

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="ZettaBrain Trial Proxy", version="1.0.0")

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "25"))

_usage: dict = defaultdict(lambda: {"count": 0, "first_seen": 0})


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
    return {"status": "ok"}


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

    body["model"] = body.get("model", "gemini-2.5-flash")
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
