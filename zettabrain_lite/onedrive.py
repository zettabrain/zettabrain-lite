"""ZettaBrain Lite — OneDrive connector using MSAL device code flow."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import httpx
import msal

from .config import BASE_DIR, DATA_DIR

log = logging.getLogger(__name__)

ONEDRIVE_TOKEN_CACHE = BASE_DIR / "onedrive_token_cache.json"
ONEDRIVE_DOWNLOAD_DIR = DATA_DIR / "onedrive"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Files.Read.All"]


class OneDriveConnector:
    def __init__(self, client_id: str, tenant_id: str = "common"):
        self.client_id = client_id
        self.tenant_id = tenant_id
        self._app: Optional[msal.PublicClientApplication] = None
        self._token_cache = msal.SerializableTokenCache()
        self._load_cache()

    def _load_cache(self) -> None:
        if ONEDRIVE_TOKEN_CACHE.exists():
            self._token_cache.deserialize(ONEDRIVE_TOKEN_CACHE.read_text(encoding="utf-8"))

    def _save_cache(self) -> None:
        ONEDRIVE_TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ONEDRIVE_TOKEN_CACHE.write_text(self._token_cache.serialize(), encoding="utf-8")

    def _get_app(self) -> msal.PublicClientApplication:
        if self._app is None:
            authority = f"https://login.microsoftonline.com/{self.tenant_id}"
            self._app = msal.PublicClientApplication(
                self.client_id,
                authority=authority,
                token_cache=self._token_cache,
            )
        return self._app

    def start_device_flow(self) -> dict:
        app = self._get_app()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError("Could not start device login. Check your App (Client) ID.")
        return flow

    def complete_device_flow(self, flow: dict) -> dict:
        app = self._get_app()
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            error = result.get("error_description", "Login was not completed in time.")
            raise RuntimeError(f"OneDrive login failed: {error}")
        self._save_cache()
        return result

    def get_access_token(self) -> Optional[str]:
        app = self._get_app()
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            self._save_cache()
            return result["access_token"]
        return None

    def list_files(self, folder_path: str = "/", token: Optional[str] = None) -> list[dict]:
        if not token:
            token = self.get_access_token()
        if not token:
            raise RuntimeError("Not authenticated. Connect to OneDrive first.")

        url = f"{GRAPH_BASE}/me/drive/root/children"
        if folder_path and folder_path != "/":
            url = f"{GRAPH_BASE}/me/drive/root:/{folder_path.strip('/')}:/children"

        files = []
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            for item in resp.json().get("value", []):
                files.append({
                    "name": item["name"],
                    "id": item["id"],
                    "size": item.get("size", 0),
                    "is_folder": "folder" in item,
                    "download_url": item.get("@microsoft.graph.downloadUrl"),
                })
        return files

    def download_files(
        self,
        folder_path: str = "/",
        extensions: Optional[list[str]] = None,
        token: Optional[str] = None,
    ) -> tuple[str, int]:
        if extensions is None:
            extensions = [".pdf", ".txt", ".docx", ".md"]

        if not token:
            token = self.get_access_token()
        if not token:
            raise RuntimeError("Not authenticated. Connect to OneDrive first.")

        ONEDRIVE_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        files = self.list_files(folder_path, token)
        count = 0

        with httpx.Client(timeout=120) as client:
            for f in files:
                if f["is_folder"]:
                    continue
                ext = Path(f["name"]).suffix.lower()
                if ext not in extensions:
                    continue
                if not f.get("download_url"):
                    continue

                dest = ONEDRIVE_DOWNLOAD_DIR / f["name"]
                resp = client.get(f["download_url"])
                resp.raise_for_status()
                dest.write_bytes(resp.content)
                count += 1
                log.info("Downloaded %s (%d bytes)", f["name"], len(resp.content))

        return str(ONEDRIVE_DOWNLOAD_DIR), count
