"""Unit tests for OneDrive file selection and paging."""

import pytest

from zettabrain_lite import onedrive
from zettabrain_lite.config import SUPPORTED_EXTENSIONS
from zettabrain_lite.onedrive import OneDriveConnector

REMOTE_FILES = [
    {"name": "flower_price_list.xlsx", "id": "1", "size": 100, "is_folder": False,
     "download_url": "https://example.invalid/1"},
    {"name": "rates.xls", "id": "2", "size": 100, "is_folder": False,
     "download_url": "https://example.invalid/2"},
    {"name": "products.csv", "id": "3", "size": 100, "is_folder": False,
     "download_url": "https://example.invalid/3"},
    {"name": "terms.pdf", "id": "4", "size": 100, "is_folder": False,
     "download_url": "https://example.invalid/4"},
    {"name": "photo.png", "id": "5", "size": 100, "is_folder": False,
     "download_url": "https://example.invalid/5"},
    {"name": "Archive", "id": "6", "size": 0, "is_folder": True, "download_url": None},
]


class _FakeResponse:
    def __init__(self, payload=None, content=b"data"):
        self._payload = payload or {}
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeClient:
    """Stands in for httpx.Client; records which URLs were fetched."""

    def __init__(self, pages=None, **kwargs):
        self.pages = pages or {}
        self.fetched = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        self.fetched.append(url)
        if url in self.pages:
            return _FakeResponse(payload=self.pages[url])
        return _FakeResponse()


@pytest.fixture
def connector(tmp_path, monkeypatch):
    monkeypatch.setattr(onedrive, "ONEDRIVE_TOKEN_CACHE", tmp_path / "cache.json")
    monkeypatch.setattr(onedrive, "ONEDRIVE_DOWNLOAD_DIR", tmp_path / "downloads")
    return OneDriveConnector(client_id="test-client")


class TestDownloadFilter:
    def test_spreadsheets_are_downloaded(self, connector, monkeypatch):
        """Regression: the default list omitted .xlsx, so spreadsheets synced as nothing."""
        monkeypatch.setattr(connector, "list_files", lambda *a, **k: REMOTE_FILES)
        monkeypatch.setattr(onedrive.httpx, "Client", lambda **k: _FakeClient())

        path, count = connector.download_files(token="fake-token")

        downloaded = {p.name for p in (onedrive.ONEDRIVE_DOWNLOAD_DIR).iterdir()}
        assert "flower_price_list.xlsx" in downloaded
        assert "rates.xls" in downloaded
        assert "products.csv" in downloaded
        assert count == 4  # xlsx, xls, csv, pdf

    def test_unsupported_types_are_skipped(self, connector, monkeypatch):
        monkeypatch.setattr(connector, "list_files", lambda *a, **k: REMOTE_FILES)
        monkeypatch.setattr(onedrive.httpx, "Client", lambda **k: _FakeClient())

        connector.download_files(token="fake-token")

        downloaded = {p.name for p in (onedrive.ONEDRIVE_DOWNLOAD_DIR).iterdir()}
        assert "photo.png" not in downloaded
        assert "Archive" not in downloaded

    def test_explicit_extensions_still_honoured(self, connector, monkeypatch):
        monkeypatch.setattr(connector, "list_files", lambda *a, **k: REMOTE_FILES)
        monkeypatch.setattr(onedrive.httpx, "Client", lambda **k: _FakeClient())

        _, count = connector.download_files(extensions=[".pdf"], token="fake-token")
        assert count == 1

    def test_default_matches_the_shared_supported_set(self):
        """OneDrive must accept everything the ingester can read."""
        assert ".xlsx" in SUPPORTED_EXTENSIONS
        assert ".csv" in SUPPORTED_EXTENSIONS


class TestListFilesPaging:
    def test_follows_next_link(self, connector, monkeypatch):
        """A folder larger than one Graph page used to sync only its first page."""
        first = f"{onedrive.GRAPH_BASE}/me/drive/root/children"
        second = "https://graph.microsoft.invalid/page2"
        pages = {
            first: {"value": [REMOTE_FILES[0]], "@odata.nextLink": second},
            second: {"value": [REMOTE_FILES[3]]},
        }
        client = _FakeClient(pages=pages)
        monkeypatch.setattr(onedrive.httpx, "Client", lambda **k: client)

        files = connector.list_files(token="fake-token")

        assert len(files) == 2
        assert second in client.fetched

    def test_single_page_makes_one_request(self, connector, monkeypatch):
        first = f"{onedrive.GRAPH_BASE}/me/drive/root/children"
        client = _FakeClient(pages={first: {"value": REMOTE_FILES[:2]}})
        monkeypatch.setattr(onedrive.httpx, "Client", lambda **k: client)

        files = connector.list_files(token="fake-token")

        assert len(files) == 2
        assert len(client.fetched) == 1
