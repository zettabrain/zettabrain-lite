"""The Ollama error path must tell the user what to do, not just a status code."""

import httpx
import pytest

from zettabrain_lite.llm.providers.ollama import OllamaProvider


def _error(status: int, body, json_body: bool = True) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://localhost:11434/api/generate")
    if json_body:
        response = httpx.Response(status, json=body, request=request)
    else:
        response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


@pytest.fixture
def provider():
    return OllamaProvider(model="phi4-mini:latest")


class TestExplainHttpError:
    def test_out_of_memory_names_the_cause_and_the_fix(self, provider):
        exc = _error(500, {"error": "model requires more system memory (3.2 GiB) "
                                    "than is available (1.4 GiB)"})
        msg = provider._explain_http_error(exc)
        assert "does not fit in this machine's memory" in msg
        assert "phi4-mini:latest" in msg
        assert "smaller model" in msg
        assert "3.2 GiB" in msg  # the raw detail is preserved for diagnosis

    def test_missing_model_gives_the_pull_command(self, provider):
        exc = _error(404, {"error": 'model "phi4-mini:latest" not found'})
        msg = provider._explain_http_error(exc)
        assert "not installed" in msg
        assert "ollama pull phi4-mini:latest" in msg

    def test_other_errors_pass_the_detail_through(self, provider):
        msg = provider._explain_http_error(_error(500, {"error": "context canceled"}))
        assert "context canceled" in msg

    def test_non_json_body_still_reported(self, provider):
        msg = provider._explain_http_error(_error(502, "upstream broke", json_body=False))
        assert "upstream broke" in msg

    def test_empty_body_falls_back_to_status(self, provider):
        msg = provider._explain_http_error(_error(500, "", json_body=False))
        assert "HTTP 500" in msg
        assert "phi4-mini:latest" in msg

    def test_detail_is_truncated(self, provider):
        msg = provider._explain_http_error(_error(500, {"error": "x" * 2000}))
        assert len(msg) < 600
