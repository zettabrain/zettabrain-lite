"""ZettaBrain Lite — Configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path

if os.name == "nt":
    _local_app = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    BASE_DIR = _local_app / "ZettaBrain-Lite"
else:
    BASE_DIR = Path(os.environ.get("ZETTABRAIN_LITE_DIR", "/opt/zettabrain-lite"))

DATA_DIR = BASE_DIR / "data"
CHROMA_DIR = BASE_DIR / "chromadb"
SKILLS_DIR = BASE_DIR / "skills"
STORAGE_CONF = BASE_DIR / "storage.conf"
CONFIG_FILE = BASE_DIR / "config.json"
DATABASE_PATH = DATA_DIR / "lite.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

PORT = int(os.environ.get("ZETTABRAIN_LITE_PORT", "7860"))
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
LLM_MODEL = os.environ.get("ZETTABRAIN_LLM_MODEL", "llama3.1:8b")
EMBED_MODEL = os.environ.get("ZETTABRAIN_EMBED_MODEL", "nomic-embed-text")


def load_config() -> dict:
    """Load user config from config.json."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    """Persist user config to config.json."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_setting(key: str, default=None):
    """Get a single setting value."""
    cfg = load_config()
    return cfg.get(key, default)


def set_setting(key: str, value):
    """Set a single setting value."""
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)
