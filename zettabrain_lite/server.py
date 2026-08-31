"""
ZettaBrain Lite — FastAPI Web Server
Single-user RAG + Skills platform with multi-provider LLM support.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import (
    BASE_DIR, CHROMA_DIR, DATA_DIR, SKILLS_DIR, STORAGE_CONF,
    OLLAMA_HOST, LLM_MODEL, EMBED_MODEL,
    load_config, save_config, get_setting, set_setting,
)
from .retrieval import hybrid_retrieve, RAG_PROMPT, format_context

PKG_DIR = Path(__file__).parent
STATIC_DIR = PKG_DIR / "static"
CHROMA_PATH = CHROMA_DIR / "default"
INGEST_LOG = DATA_DIR / "ingested_files.json"

# ── Vectorstore cache ────────────────────────────────────────────────────────
_vs_lock = threading.Lock()
_vs_cache: dict = {"vs": None}


def _get_vs():
    with _vs_lock:
        if _vs_cache["vs"] is None:
            from langchain_ollama import OllamaEmbeddings
            from langchain_chroma import Chroma

            cfg = load_config()
            embed_provider = cfg.get("embed_provider", "ollama")
            embed_model = cfg.get("embed_model", EMBED_MODEL)
            ollama_host = cfg.get("ollama_host", OLLAMA_HOST)

            if embed_provider == "ollama":
                emb = OllamaEmbeddings(model=embed_model, base_url=ollama_host)
            elif embed_provider == "openai":
                from langchain_openai import OpenAIEmbeddings
                emb = OpenAIEmbeddings(model=embed_model, api_key=cfg.get("openai_api_key"))
            else:
                emb = OllamaEmbeddings(model=embed_model, base_url=ollama_host)

            _vs_cache["vs"] = Chroma(
                persist_directory=str(CHROMA_PATH),
                embedding_function=emb,
                collection_name="zettabrain_docs",
            )
        return _vs_cache["vs"]


def _reset_vs_cache():
    with _vs_lock:
        _vs_cache["vs"] = None


# ── GPU detection ────────────────────────────────────────────────────────────
def _get_gpu_info() -> dict:
    info = {"type": "none", "name": None, "vram_gb": 0, "recommended_model": None}
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip().splitlines()[0]
        name, vram_mb = [s.strip() for s in out.split(",")]
        vram_gb = int(vram_mb) // 1024
        info.update({"type": "nvidia", "name": name, "vram_gb": vram_gb})
    except Exception:
        pass

    if info["type"] == "none":
        try:
            out = subprocess.check_output(
                ["rocm-smi", "--showmeminfo", "vram"], stderr=subprocess.DEVNULL, timeout=5
            ).decode()
            import re
            m = re.search(r"(\d+)\s*MB", out)
            if m:
                info.update({"type": "amd", "vram_gb": int(m.group(1)) // 1024})
        except Exception:
            pass

    if info["type"] == "none":
        try:
            if subprocess.check_output(["uname", "-m"]).decode().strip() == "arm64":
                mem = int(subprocess.check_output(
                    ["sysctl", "-n", "hw.memsize"]).decode().strip())
                info.update({"type": "apple_silicon", "vram_gb": mem // 1_073_741_824})
        except Exception:
            pass

    vg = info["vram_gb"]
    if info["type"] == "none" or vg == 0:
        info["recommended_model"] = "phi4-mini"
    elif vg >= 24:
        info["recommended_model"] = "qwen2.5:32b"
    elif vg >= 16:
        info["recommended_model"] = "qwen2.5:14b"
    elif vg >= 12:
        info["recommended_model"] = "mistral-nemo:12b"
    elif vg >= 8:
        info["recommended_model"] = "llama3.1:8b"
    elif vg >= 5:
        info["recommended_model"] = "mistral:7b"
    else:
        info["recommended_model"] = "phi4-mini"

    return info


# ── Helpers ──────────────────────────────────────────────────────────────────
def _ollama_running() -> bool:
    ollama_url = get_setting("ollama_host") or OLLAMA_HOST
    _url = ollama_url.replace("https://", "http://")
    try:
        r = requests.get(_url, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def _get_chunk_count() -> int:
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(CHROMA_PATH))
        return client.get_collection("zettabrain_docs").count()
    except Exception:
        return 0


def _get_sources() -> list:
    if not INGEST_LOG.exists():
        return []
    try:
        data = json.loads(INGEST_LOG.read_text(encoding="utf-8"))
        return sorted([Path(p).name for p in data.keys()])
    except Exception:
        return []


def _get_ollama_models() -> list:
    ollama_url = get_setting("ollama_host") or OLLAMA_HOST
    try:
        r = requests.get(f"{ollama_url}/api/tags", timeout=5)
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    return []


def _get_available_models() -> list:
    """Return all available models with provider prefix for the model dropdown."""
    models = []

    # Trial model (first in list if available)
    try:
        from .trial import get_trial_model_entry
        trial_entry = get_trial_model_entry()
        if trial_entry:
            models.append(trial_entry)
    except Exception:
        pass

    # Local Ollama models
    for m in _get_ollama_models():
        models.append({"id": f"ollama:{m}", "label": f"Local-{m}", "provider": "ollama"})

    # Cloud models from config
    cfg = load_config()

    if cfg.get("groq_api_key"):
        groq_model = cfg.get("groq_model", "llama-3.1-8b-instant")
        models.append({"id": f"groq:{groq_model}", "label": f"Groq-{groq_model}", "provider": "groq"})

    if cfg.get("openai_api_key"):
        oai_model = cfg.get("openai_model", "gpt-4o-mini")
        models.append({"id": f"openai:{oai_model}", "label": f"OpenAI-{oai_model}", "provider": "openai"})

    if cfg.get("anthropic_api_key"):
        claude_model = cfg.get("claude_model", "claude-sonnet-4-6")
        models.append({"id": f"claude:{claude_model}", "label": f"Claude-{claude_model}", "provider": "claude"})

    if cfg.get("together_api_key"):
        tog_model = cfg.get("together_model", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo")
        short = tog_model.split("/")[-1] if "/" in tog_model else tog_model
        models.append({"id": f"together:{tog_model}", "label": f"Together-{short}", "provider": "together"})

    if cfg.get("cerebras_api_key"):
        cer_model = cfg.get("cerebras_model", "llama3.1-8b")
        models.append({"id": f"cerebras:{cer_model}", "label": f"Cerebras-{cer_model}", "provider": "cerebras"})

    if cfg.get("openrouter_api_key"):
        or_model = cfg.get("openrouter_model", "meta-llama/llama-3.1-8b-instruct:free")
        short = or_model.split("/")[-1] if "/" in or_model else or_model
        models.append({"id": f"openrouter:{or_model}", "label": f"OpenRouter-{short}", "provider": "openrouter"})

    if cfg.get("fireworks_api_key"):
        fw_model = cfg.get("fireworks_model", "accounts/fireworks/models/llama-v3p1-8b-instruct")
        short = fw_model.split("/")[-1] if "/" in fw_model else fw_model
        models.append({"id": f"fireworks:{fw_model}", "label": f"Fireworks-{short}", "provider": "fireworks"})

    if cfg.get("gemini_api_key"):
        gem_model = cfg.get("gemini_model", "gemini-2.0-flash-lite")
        models.append({"id": f"gemini:{gem_model}", "label": f"Gemini-{gem_model}", "provider": "gemini"})

    return models


def _get_storage_sources() -> list:
    sources = []
    if STORAGE_CONF.exists():
        for line in STORAGE_CONF.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 4:
                sources.append({
                    "role": parts[0].strip(),
                    "type": parts[1].strip(),
                    "label": parts[2].strip(),
                    "path": parts[3].strip(),
                })
    if not sources:
        cfg = load_config()
        docs_folder = cfg.get("docs_folder", str(DATA_DIR / "documents"))
        sources.append({"role": "primary", "type": "local", "label": "Documents", "path": docs_folder})
    return sources


def _count_docs_all_sources() -> int:
    total = 0
    seen = set()
    for src in _get_storage_sources():
        p = Path(src["path"])
        if p in seen or not p.exists():
            continue
        seen.add(p)
        for ext in ["*.pdf", "*.txt", "*.docx", "*.md"]:
            total += len(list(p.rglob(ext)))
    return total


def _resolve_llm_for_chat(model_id: str):
    """Parse a model_id like 'ollama:llama3.1:8b' or 'groq:llama-3.1-8b-instant'
    and return a (provider, model, api_key, ollama_host) tuple."""
    cfg = load_config()
    if ":" in model_id:
        parts = model_id.split(":", 1)
        provider = parts[0]
        model = parts[1]
    else:
        provider = "ollama"
        model = model_id

    ollama_host = cfg.get("ollama_host", OLLAMA_HOST)
    api_key = None

    if provider == "ollama":
        pass
    elif provider == "trial":
        pass
    elif provider == "openai":
        api_key = cfg.get("openai_api_key")
    elif provider == "claude":
        api_key = cfg.get("anthropic_api_key")
    elif provider == "gemini":
        api_key = cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    else:
        api_key = cfg.get(f"{provider}_api_key")

    return provider, model, api_key, ollama_host


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="ZettaBrain Lite", version="0.1.0")


@app.on_event("startup")
async def _warmup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    if CHROMA_PATH.exists():
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _get_vs)
        except Exception:
            pass


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Request Models ───────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    question: str
    model: Optional[str] = None


class IngestRequest(BaseModel):
    folder: Optional[str] = None
    rebuild: bool = False


class SettingsUpdate(BaseModel):
    settings: dict


class PullRequest(BaseModel):
    model: str


class StorageAddRequest(BaseModel):
    type: str  # local, nfs, smb, s3
    label: str
    role: str = "secondary"
    # Local
    path: Optional[str] = None
    # NFS
    server_ip: Optional[str] = None
    export_path: Optional[str] = None
    mount_point: Optional[str] = None
    nfs_version: Optional[str] = "4"
    # SMB
    share_name: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    domain: Optional[str] = None
    # S3
    bucket: Optional[str] = None
    region: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None


class GenerateBody(BaseModel):
    input: str
    skill_name: str
    model: Optional[str] = None
    context: dict = {}
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class SkillUploadBody(BaseModel):
    content: str
    filename: str


# ── Routes: UI ───────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>ZettaBrain Lite</h1><p>Static files not found.</p>")


# ── Routes: Status & Models ──────────────────────────────────────────────────
@app.get("/api/status")
async def status():
    return {
        "ollama": {
            "running": _ollama_running(),
            "url": get_setting("ollama_host") or OLLAMA_HOST,
            "models": _get_ollama_models(),
            "active_llm": get_setting("llm_model") or LLM_MODEL,
            "active_embed": get_setting("embed_model") or EMBED_MODEL,
        },
        "vectorstore": {
            "exists": CHROMA_PATH.exists(),
            "chunks": _get_chunk_count(),
            "path": str(CHROMA_PATH),
        },
        "storage": {
            "doc_count": _count_docs_all_sources(),
            "sources": _get_storage_sources(),
        },
        "hardware": _get_gpu_info(),
        "sources": _get_sources(),
    }


@app.get("/api/models")
async def get_models():
    return {"models": _get_available_models()}


@app.get("/api/trial")
async def trial_status():
    try:
        from .trial import get_trial_usage
        return get_trial_usage()
    except Exception:
        return {"requests_used": 0, "requests_remaining": 0, "max_requests": 5, "exhausted": True}


@app.get("/api/sources")
async def get_sources():
    return {"sources": _get_sources()}


# ── Routes: Settings ─────────────────────────────────────────────────────────
@app.get("/api/settings")
async def get_settings():
    cfg = load_config()
    safe_cfg = {}
    for k, v in cfg.items():
        if "key" in k.lower() or "secret" in k.lower() or "password" in k.lower():
            safe_cfg[k] = "***" + str(v)[-4:] if v else ""
        else:
            safe_cfg[k] = v
    return {"settings": safe_cfg}


@app.post("/api/settings")
async def update_settings(body: SettingsUpdate):
    cfg = load_config()
    for k, v in body.settings.items():
        if v and not (isinstance(v, str) and v.startswith("***")):
            cfg[k] = v
    save_config(cfg)
    _reset_vs_cache()
    return {"success": True}


# ── Routes: Storage Management ───────────────────────────────────────────────
@app.get("/api/storage")
async def list_storage():
    return {"sources": _get_storage_sources(), "doc_count": _count_docs_all_sources()}


def _validate_ip(ip: str) -> bool:
    return bool(re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip))


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _mount_nfs(server_ip: str, export_path: str, mount_point: str, nfs_version: str = "4") -> str:
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    if _is_macos():
        opts = f"nfsvers={nfs_version},rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2"
        cmd = ["mount", "-t", "nfs", "-o", opts, f"{server_ip}:{export_path}", mount_point]
    else:
        opts = f"defaults,_netdev,nfsvers={nfs_version},rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2"
        cmd = ["mount", "-t", "nfs", "-o", opts, f"{server_ip}:{export_path}", mount_point]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return result.stderr.strip() or f"Mount failed (exit code {result.returncode})"
    return ""


def _mount_smb(server_ip: str, share_name: str, mount_point: str,
               username: str = "guest", password: str = "", domain: str = "") -> str:
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    creds_dir = Path("/etc/zettabrain")
    creds_dir.mkdir(parents=True, exist_ok=True)
    creds_file = creds_dir / f"smb-{server_ip}.credentials"
    creds_content = f"username={username}\npassword={password}\n"
    if domain:
        creds_content += f"domain={domain}\n"
    creds_file.write_text(creds_content, encoding="utf-8")
    creds_file.chmod(0o600)

    if _is_macos():
        mount_url = f"//{username}:{password}@{server_ip}/{share_name}"
        cmd = ["mount", "-t", "smbfs", mount_url, mount_point]
    else:
        opts = f"uid=0,gid=0,file_mode=0755,dir_mode=0755,noperm,_netdev,credentials={creds_file}"
        cmd = ["mount", "-t", "cifs", f"//{server_ip}/{share_name}", mount_point, "-o", opts]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return result.stderr.strip() or f"Mount failed (exit code {result.returncode})"
    return ""


@app.post("/api/storage")
async def add_storage(body: StorageAddRequest):
    error = ""
    final_path = ""

    if body.type == "local":
        if not body.path:
            raise HTTPException(status_code=400, detail="Path is required for local storage")
        p = Path(body.path)
        p.mkdir(parents=True, exist_ok=True)
        final_path = body.path

    elif body.type == "nfs":
        if not body.server_ip or not body.export_path or not body.mount_point:
            raise HTTPException(status_code=400, detail="NFS requires server_ip, export_path, and mount_point")
        if not _validate_ip(body.server_ip):
            raise HTTPException(status_code=400, detail="Invalid IP address format")
        if not body.export_path.startswith("/"):
            raise HTTPException(status_code=400, detail="Export path must start with /")
        error = _mount_nfs(body.server_ip, body.export_path, body.mount_point, body.nfs_version or "4")
        final_path = body.mount_point

    elif body.type == "smb":
        if not body.server_ip or not body.share_name or not body.mount_point:
            raise HTTPException(status_code=400, detail="SMB requires server_ip, share_name, and mount_point")
        if not _validate_ip(body.server_ip):
            raise HTTPException(status_code=400, detail="Invalid IP address format")
        error = _mount_smb(
            body.server_ip, body.share_name, body.mount_point,
            body.username or "guest", body.password or "", body.domain or "",
        )
        final_path = body.mount_point

    elif body.type == "s3":
        if not body.bucket or not body.mount_point:
            raise HTTPException(status_code=400, detail="S3 requires bucket and mount_point")
        Path(body.mount_point).mkdir(parents=True, exist_ok=True)
        s3_cfg = BASE_DIR / "s3_credentials"
        s3_cfg.parent.mkdir(parents=True, exist_ok=True)
        s3_content = f"{body.access_key or ''}:{body.secret_key or ''}"
        s3_cfg.write_text(s3_content, encoding="utf-8")
        s3_cfg.chmod(0o600)
        final_path = body.mount_point
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported storage type: {body.type}")

    if error:
        raise HTTPException(status_code=500, detail=f"Mount failed: {error}")

    STORAGE_CONF.parent.mkdir(parents=True, exist_ok=True)
    line = f"{body.role}|{body.type}|{body.label}|{final_path}\n"
    with open(STORAGE_CONF, "a", encoding="utf-8") as f:
        f.write(line)

    return {"success": True, "source": {
        "role": body.role, "type": body.type, "label": body.label, "path": final_path
    }}


@app.post("/api/storage/test")
async def test_storage(body: StorageAddRequest):
    """Test storage connectivity without mounting."""
    if body.type == "local":
        exists = Path(body.path or "").exists() if body.path else False
        return {"reachable": exists, "message": "Path exists" if exists else "Path not found"}

    if body.type == "nfs":
        if not body.server_ip:
            return {"reachable": False, "message": "Server IP required"}
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "3", body.server_ip],
            capture_output=True, text=True, timeout=10,
        )
        reachable = result.returncode == 0
        return {"reachable": reachable, "message": f"Server {'reachable' if reachable else 'unreachable'}"}

    if body.type == "smb":
        if not body.server_ip:
            return {"reachable": False, "message": "Server IP required"}
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "3", body.server_ip],
            capture_output=True, text=True, timeout=10,
        )
        reachable = result.returncode == 0
        return {"reachable": reachable, "message": f"Server {'reachable' if reachable else 'unreachable'}"}

    return {"reachable": False, "message": "Test not supported for this type"}


@app.delete("/api/storage/{index}")
async def remove_storage(index: int):
    if not STORAGE_CONF.exists():
        raise HTTPException(status_code=404, detail="No storage sources configured")
    lines = [l for l in STORAGE_CONF.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    if index < 0 or index >= len(lines):
        raise HTTPException(status_code=404, detail="Source not found")
    lines.pop(index)
    STORAGE_CONF.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")
    return {"success": True}


# ── Routes: Model Pull ───────────────────────────────────────────────────────
@app.post("/api/pull")
async def pull_model(req: PullRequest):
    ollama_url = get_setting("ollama_host") or OLLAMA_HOST
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _do_pull():
        try:
            resp = requests.post(
                f"{ollama_url}/api/pull",
                json={"name": req.model, "stream": True},
                stream=True, timeout=600,
            )
            if resp.status_code != 200:
                asyncio.run_coroutine_threadsafe(
                    queue.put(f"Error: Ollama returned {resp.status_code}\n"), loop)
                return
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    status_msg = chunk.get("status", "")
                    if chunk.get("total") and chunk.get("completed") is not None:
                        pct = int(chunk["completed"] / chunk["total"] * 100)
                        msg = f"{status_msg} {pct}%\n"
                    elif status_msg:
                        msg = f"{status_msg}\n"
                    else:
                        continue
                    asyncio.run_coroutine_threadsafe(queue.put(msg), loop)
                except Exception:
                    pass
        except Exception as exc:
            asyncio.run_coroutine_threadsafe(queue.put(f"Error: {exc}\n"), loop)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    loop.run_in_executor(None, _do_pull)

    async def _gen():
        while True:
            msg = await queue.get()
            if msg is None:
                break
            yield msg

    return StreamingResponse(_gen(), media_type="text/plain")


# ── Routes: Ingestion ────────────────────────────────────────────────────────
@app.post("/api/ingest")
async def ingest(req: IngestRequest):
    script = PKG_DIR / "scripts" / "05_ingest_documents.py"
    if not script.exists():
        raise HTTPException(status_code=404, detail="Ingest script not found.")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_PATH.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    docs_folder = req.folder or cfg.get("docs_folder")
    if not docs_folder:
        sources = _get_storage_sources()
        docs_folder = sources[0]["path"] if sources else str(DATA_DIR / "documents")

    cmd = [sys.executable, str(script)]
    if req.folder:
        cmd += ["--folder", req.folder]
    if req.rebuild:
        cmd += ["--rebuild"]

    env = os.environ.copy()
    env["ZETTABRAIN_CHROMA"] = str(CHROMA_PATH)
    env["ZETTABRAIN_DOCS"] = docs_folder
    env["OLLAMA_HOST"] = cfg.get("ollama_host", OLLAMA_HOST)

    try:
        result = subprocess.run(
            cmd, cwd=str(DATA_DIR),
            capture_output=True, text=True, timeout=600, env=env,
        )
        if result.returncode == 0:
            _reset_vs_cache()
        return {
            "success": result.returncode == 0,
            "output": result.stdout,
            "error": result.stderr if result.returncode != 0 else None,
            "chunks": _get_chunk_count(),
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Ingestion timed out.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ingest/source/{index}")
async def ingest_source(index: int):
    """Ingest documents from a specific storage source by index."""
    sources = _get_storage_sources()
    if index < 0 or index >= len(sources):
        raise HTTPException(status_code=404, detail="Source not found")
    source = sources[index]
    req = IngestRequest(folder=source["path"])
    return await ingest(req)


# ── Routes: Chat (RAG) ──────────────────────────────────────────────────────
@app.post("/api/chat")
async def chat(req: ChatRequest):
    if _get_chunk_count() == 0:
        raise HTTPException(status_code=422, detail="Vector store is empty. Ingest documents first.")

    model_id = req.model or f"ollama:{get_setting('llm_model') or LLM_MODEL}"
    provider, model, api_key, ollama_host = _resolve_llm_for_chat(model_id)

    question = req.question

    def _run():
        vs = _get_vs()
        t0 = time.monotonic()
        sources = hybrid_retrieve(question, vs)
        t_retr = time.monotonic() - t0

        context = format_context(sources)
        prompt_text = RAG_PROMPT.format(context=context, question=question)

        t1 = time.monotonic()

        if provider == "trial":
            from .trial import trial_generate
            answer = trial_generate(prompt_text, temperature=0.0, max_tokens=1024)
        else:
            from .llm.factory import get_chat_llm

            llm = get_chat_llm(
                provider=provider, model=model,
                ollama_host=ollama_host, api_key=api_key,
            )

            from langchain_core.prompts import PromptTemplate
            prompt = PromptTemplate.from_template(RAG_PROMPT)
            response = llm.invoke(prompt.format(context=context, question=question))
            if hasattr(response, 'content'):
                answer = response.content.strip()
            else:
                answer = str(response).strip()

        t_gen = time.monotonic() - t1
        return sources, answer, t_retr, t_gen

    try:
        loop = asyncio.get_event_loop()
        sources, answer, t_retr, t_gen = await loop.run_in_executor(None, _run)

        # Save to history
        from .database import get_session, ChatHistory
        with get_session() as session:
            session.add(ChatHistory(
                question=req.question,
                answer=answer,
                model=model_id,
                chunks_searched=len(sources),
                duration_ms=round((t_retr + t_gen) * 1000),
                sources=json.dumps([Path(s.metadata.get("source", "?")).name for s in sources]),
            ))
            session.commit()

        return {
            "answer": answer,
            "model": model_id,
            "chunks_searched": len(sources),
            "timing": {
                "retrieve_ms": round(t_retr * 1000),
                "generate_ms": round(t_gen * 1000),
            },
            "sources": [
                {"filename": Path(s.metadata.get("source", "?")).name,
                 "page": s.metadata.get("page", ""),
                 "preview": s.page_content[:200]}
                for s in sources
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            question = payload.get("question", "").strip()
            model_id = payload.get("model", f"ollama:{get_setting('llm_model') or LLM_MODEL}")

            if not question:
                await websocket.send_json({"type": "error", "message": "Empty question"})
                continue
            if _get_chunk_count() == 0:
                await websocket.send_json({"type": "error", "message": "Vector store is empty. Ingest documents first."})
                continue

            provider, model, api_key, ollama_host = _resolve_llm_for_chat(model_id)

            try:
                loop = asyncio.get_event_loop()
                vectorstore = await loop.run_in_executor(None, _get_vs)

                t_r0 = time.monotonic()
                sources = await loop.run_in_executor(
                    None, lambda: hybrid_retrieve(question, vectorstore)
                )
                t_retr = time.monotonic() - t_r0
                source_list = [
                    {"filename": Path(s.metadata.get("source", "?")).name,
                     "page": s.metadata.get("page", ""),
                     "preview": s.page_content[:200]}
                    for s in sources
                ]
                await websocket.send_json({"type": "sources", "sources": source_list})

                context = format_context(sources)
                prompt_text = RAG_PROMPT.format(context=context, question=question)

                # Stream based on provider
                if provider == "trial":
                    from .trial import trial_generate
                    t_g0 = time.monotonic()
                    full_answer = ""
                    error_msg = None
                    try:
                        result = await loop.run_in_executor(
                            None, lambda: trial_generate(prompt_text, temperature=0.0, max_tokens=1024)
                        )
                        full_answer = result
                        await websocket.send_json({"type": "token", "token": result})
                    except Exception as exc:
                        error_msg = str(exc)

                elif provider == "ollama":
                    queue: asyncio.Queue = asyncio.Queue()
                    t_g0 = time.monotonic()

                    def _stream_ollama():
                        try:
                            resp = requests.post(
                                f"{ollama_host}/api/generate",
                                json={"model": model, "prompt": prompt_text, "stream": True},
                                stream=True, timeout=600,
                            )
                            if resp.status_code != 200:
                                asyncio.run_coroutine_threadsafe(
                                    queue.put(("error", f"Ollama {resp.status_code}")), loop)
                                return
                            for line in resp.iter_lines():
                                if line:
                                    chunk = json.loads(line)
                                    if "error" in chunk:
                                        asyncio.run_coroutine_threadsafe(
                                            queue.put(("error", chunk["error"])), loop)
                                        return
                                    token = chunk.get("response", "")
                                    asyncio.run_coroutine_threadsafe(
                                        queue.put(("token", token)), loop)
                                    if chunk.get("done"):
                                        break
                        except Exception as exc:
                            asyncio.run_coroutine_threadsafe(
                                queue.put(("error", str(exc))), loop)
                        finally:
                            asyncio.run_coroutine_threadsafe(
                                queue.put(("done", None)), loop)

                    loop.run_in_executor(None, _stream_ollama)

                    full_answer = ""
                    error_msg = None
                    while True:
                        kind, value = await queue.get()
                        if kind == "token":
                            full_answer += value
                            await websocket.send_json({"type": "token", "token": value})
                        elif kind == "error":
                            error_msg = value
                            break
                        else:
                            break

                else:
                    # Cloud providers: use LLM factory streaming
                    from .llm.factory import create_generation_provider
                    t_g0 = time.monotonic()
                    gen_provider = create_generation_provider(
                        provider_name=provider, model=model, api_key=api_key,
                    )
                    full_answer = ""
                    error_msg = None

                    def _stream_cloud():
                        tokens = []
                        for token in gen_provider.stream(prompt_text, temperature=0.0, max_tokens=1024):
                            tokens.append(token)
                        return tokens

                    tokens = await loop.run_in_executor(None, _stream_cloud)
                    for token in tokens:
                        full_answer += token
                        await websocket.send_json({"type": "token", "token": token})

                if error_msg:
                    await websocket.send_json({"type": "error", "message": error_msg})
                    continue

                t_gen = time.monotonic() - t_g0

                # Save to history
                from .database import get_session, ChatHistory
                with get_session() as session:
                    session.add(ChatHistory(
                        question=question, answer=full_answer, model=model_id,
                        chunks_searched=len(sources),
                        duration_ms=round((t_retr + t_gen) * 1000),
                        sources=json.dumps([s["filename"] for s in source_list]),
                    ))
                    session.commit()

                await websocket.send_json({
                    "type": "done",
                    "answer": full_answer,
                    "model": model_id,
                    "chunks_searched": len(sources),
                    "timing": {
                        "retrieve_ms": round(t_retr * 1000),
                        "generate_ms": round(t_gen * 1000),
                    },
                })

            except Exception as e:
                msg = str(e)
                if "hnsw" in msg.lower() or "segment" in msg.lower():
                    _reset_vs_cache()
                    msg = "Vector store index is corrupt. Clear it and re-run ingestion."
                await websocket.send_json({"type": "error", "message": msg})

    except WebSocketDisconnect:
        pass


# ── Routes: Skills / Generation ──────────────────────────────────────────────
_BUILTIN_SKILLS_DIR = PKG_DIR / "skills"


@app.get("/api/skills")
async def list_skills():
    from .generation.skill_parser import SkillParser
    seen_names = set()
    skills = []

    for skills_dir in [SKILLS_DIR, _BUILTIN_SKILLS_DIR]:
        if not skills_dir.exists():
            continue
        for f in sorted(skills_dir.glob("*.md")):
            try:
                skill = SkillParser.parse_file(f)
                if skill.name not in seen_names:
                    seen_names.add(skill.name)
                    skills.append({
                        "name": skill.name,
                        "version": skill.version,
                        "description": skill.description,
                        "business_type": skill.business_type,
                        "requires_corpus": skill.requires_corpus,
                        "tags": skill.tags,
                    })
            except Exception:
                continue

    return {"skills": skills}


@app.post("/api/generate")
async def generate_document(body: GenerateBody):
    from .generation.skill_parser import SkillParser, load_skill
    from .generation.engine import GenerationEngine
    from .generation.models import GenerationRequest

    skill_file = _find_skill_file(body.skill_name)
    if not skill_file:
        raise HTTPException(status_code=404, detail=f"Skill '{body.skill_name}' not found")

    skill = load_skill(skill_file)

    corpus_retriever = None
    if skill.requires_corpus:
        corpus_retriever = _build_corpus_retriever()

    model_id = body.model or f"ollama:{get_setting('llm_model') or LLM_MODEL}"
    provider, model, api_key, ollama_host = _resolve_llm_for_chat(model_id)

    if provider == "trial":
        from .trial import trial_generate as _trial_gen
        from .llm.base import LLMProvider as _LP

        class _TrialProvider(_LP):
            def generate(self, prompt, temperature=0.7, max_tokens=2000, **kw):
                return _trial_gen(prompt, temperature=temperature, max_tokens=max_tokens)
            def stream(self, prompt, temperature=0.7, max_tokens=2000, **kw):
                yield self.generate(prompt, temperature, max_tokens)
            def check_health(self):
                return True
            def get_model_info(self):
                return {"provider": "trial", "model": "gemini-2.0-flash-lite"}

        llm_provider = _TrialProvider()
    else:
        from .llm.factory import create_generation_provider
        gen_kwargs = {"provider_name": provider, "model": model}
        if api_key:
            gen_kwargs["api_key"] = api_key
        if provider == "ollama":
            gen_kwargs["base_url"] = ollama_host
        llm_provider = create_generation_provider(**gen_kwargs)

    engine = GenerationEngine(llm_provider=llm_provider, corpus_retriever=corpus_retriever)

    request = GenerationRequest(
        input=body.input,
        skill_name=body.skill_name,
        context=body.context,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
    )

    result = engine.generate(skill, request)

    if not result.success:
        raise HTTPException(status_code=500, detail=f"Generation failed: {result.error}")

    # Save to history
    from .database import get_session, GenerationHistory
    with get_session() as session:
        record = GenerationHistory(
            skill_name=result.skill_name,
            skill_version=result.skill_version,
            input_text=body.input,
            output_content=result.content,
            citations=json.dumps(result.citations) if result.citations else None,
            generation_time_ms=result.generation_time_ms,
            metadata_json=json.dumps(result.metadata) if result.metadata else None,
        )
        session.add(record)
        session.commit()
        session.refresh(record)

    return {
        "id": record.id,
        "content": result.content,
        "skill_name": result.skill_name,
        "skill_version": result.skill_version,
        "citations": result.citations,
        "generation_time_ms": result.generation_time_ms,
    }


@app.post("/api/skills/upload")
async def upload_skill(body: SkillUploadBody):
    from .generation.skill_parser import SkillParser
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
        tmp.write(body.content)
        tmp_path = tmp.name

    try:
        skill = SkillParser.parse_and_validate(tmp_path)
    except (ValueError, FileNotFoundError) as e:
        Path(tmp_path).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    filename = body.filename if body.filename.endswith(".md") else f"{body.filename}.md"
    skill_path = SKILLS_DIR / filename
    skill_path.write_text(body.content, encoding="utf-8")

    return {"message": f"Skill '{skill.name}' uploaded", "skill": {
        "name": skill.name, "version": skill.version, "description": skill.description,
    }}


@app.delete("/api/skills/{skill_name}")
async def delete_skill(skill_name: str):
    from .generation.skill_parser import SkillParser
    if not SKILLS_DIR.exists():
        raise HTTPException(status_code=404, detail="Skill not found")
    for f in SKILLS_DIR.glob("*.md"):
        try:
            skill = SkillParser.parse_file(f)
            if skill.name == skill_name:
                f.unlink()
                return {"message": f"Skill '{skill_name}' deleted"}
        except Exception:
            continue
    raise HTTPException(status_code=404, detail="Skill not found or is built-in")


# ── Routes: History ──────────────────────────────────────────────────────────
@app.get("/api/history/chat")
async def chat_history(limit: int = 50):
    from sqlmodel import select
    from .database import get_session, ChatHistory
    with get_session() as session:
        rows = session.exec(
            select(ChatHistory).order_by(ChatHistory.created_at.desc()).limit(limit)
        ).all()
    return [
        {"id": r.id, "question": r.question[:200], "answer": r.answer[:300],
         "model": r.model, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@app.get("/api/history/generation")
async def generation_history(limit: int = 50):
    from sqlmodel import select
    from .database import get_session, GenerationHistory
    with get_session() as session:
        rows = session.exec(
            select(GenerationHistory).order_by(GenerationHistory.created_at.desc()).limit(limit)
        ).all()
    return [
        {"id": r.id, "skill_name": r.skill_name, "input_text": r.input_text[:200],
         "output_preview": r.output_content[:300], "created_at": r.created_at.isoformat()}
        for r in rows
    ]


# ── Routes: Clear Vectorstore ────────────────────────────────────────────────
@app.delete("/api/vectorstore")
async def clear_vectorstore():
    _reset_vs_cache()
    try:
        import chromadb
        chromadb.PersistentClient(path=str(CHROMA_PATH)).delete_collection("zettabrain_docs")
    except Exception:
        if CHROMA_PATH.exists():
            shutil.rmtree(str(CHROMA_PATH), ignore_errors=True)
            CHROMA_PATH.mkdir(parents=True, exist_ok=True)

    if INGEST_LOG.exists():
        INGEST_LOG.write_text("{}", encoding="utf-8")

    return {"success": True, "message": "Vector store cleared."}


# ── Helpers: Skills ──────────────────────────────────────────────────────────
def _find_skill_file(skill_name: str):
    from .generation.skill_parser import SkillParser
    for skills_dir in [SKILLS_DIR, _BUILTIN_SKILLS_DIR]:
        if not skills_dir.exists():
            continue
        for f in skills_dir.glob("*.md"):
            try:
                skill = SkillParser.parse_file(f)
                if skill.name == skill_name:
                    return f
            except Exception:
                continue
    return None


def _build_corpus_retriever():
    class LiteCorpusRetriever:
        def get_context_for_generation(self, query, n_results=5, min_relevance=0.3, **kwargs):
            try:
                vs = _get_vs()
                sources = hybrid_retrieve(query, vs, top_k=n_results)
                if not sources:
                    return None, []

                from dataclasses import dataclass

                @dataclass
                class Citation:
                    document_title: str
                    citation_ref: str = ""

                context_parts = ["# CORPUS CONTEXT (from document library)"]
                citations = []
                for i, doc in enumerate(sources, 1):
                    source = Path(doc.metadata.get("source", "unknown")).stem
                    context_parts.append(f"\n## Source [{i}]: {source}")
                    context_parts.append(doc.page_content)
                    citations.append(Citation(document_title=source, citation_ref=f"[{i}]"))

                return "\n".join(context_parts), citations
            except Exception:
                return None, []

    return LiteCorpusRetriever()
