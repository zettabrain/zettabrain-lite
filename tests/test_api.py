"""Basic API smoke tests for ZettaBrain Lite."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app():
    from zettabrain_lite.server import app

    return app


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_status_endpoint(client):
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "ollama" in data
    assert "running" in data["ollama"]


@pytest.mark.asyncio
async def test_models_endpoint(client):
    resp = await client.get("/api/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    assert isinstance(data["models"], list)


@pytest.mark.asyncio
async def test_settings_get(client):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_history_chat(client):
    resp = await client.get("/api/history/chat")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_history_generation(client):
    resp = await client.get("/api/history/generation")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_static_index(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "ZettaBrain" in resp.text
