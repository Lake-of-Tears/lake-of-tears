from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import ai_gateway_client
import pytest
from ai_providers import AIProvider
from ai_providers.compat import ProviderClient

ROOT = Path(__file__).parents[1]


def test_ui_and_pipelines_do_not_import_a_vendor_ai_sdk():
    """Everything outside the backend's provider code goes through the AI gateway."""
    pattern = re.compile(
        r"^\s*(from google import genai|import google\.genai|from google\.genai)", re.M
    )
    offenders = [
        str(path.relative_to(ROOT))
        for folder in ("ui", "pipeline")
        for path in (ROOT / folder).rglob("*.py")
        if pattern.search(path.read_text())
    ]
    assert offenders == []


# ── UI gateway client ─────────────────────────────────────────────────────────


def _response(status: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.reason_phrase = "reason"
    return resp


@pytest.fixture
def http(monkeypatch):
    ai_gateway_client.clear_cache()
    calls: list[dict] = []
    replies: list[MagicMock] = []

    def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return replies.pop(0)

    monkeypatch.setattr(ai_gateway_client.httpx, "request", fake_request)
    monkeypatch.setenv("BACKEND_URL", "http://backend:8000")
    monkeypatch.setenv("AUTH_SECRET_KEY", "internal-secret")
    yield SimpleNamespace(calls=calls, replies=replies)
    ai_gateway_client.clear_cache()


def test_generate_content_maps_the_old_client_shape_to_the_gateway(http):
    http.replies.append(_response(200, {"text": "hello"}))
    out = ai_gateway_client.GatewayClient().models.generate_content(
        model="m", contents="hi", config={"system_instruction": "s", "temperature": 0.3}
    )
    assert out.text == "hello"
    call = http.calls[0]
    assert call["url"] == "http://backend:8000/api/internal/ai/generate"
    assert call["headers"] == {"X-Internal-Secret": "internal-secret"}
    assert call["json"] == {"prompt": "hi", "model": "m", "system": "s", "temperature": 0.3}


def test_generate_content_without_a_model_lets_the_provider_choose(http):
    http.replies.append(_response(200, {"text": "x"}))
    ai_gateway_client.GatewayClient().models.generate_content(contents="hi")
    assert http.calls[0]["json"] == {"prompt": "hi"}


def test_embed_content_reports_the_model_and_dimension_the_provider_used(http):
    http.replies.append(
        _response(
            200,
            {"embeddings": [[1.0, 2.0]], "provider": "p", "embedding_model": "e1", "dimension": 2},
        )
    )
    out = ai_gateway_client.GatewayClient().models.embed_content(
        model="ignored", contents="q", config={"task_type": "RETRIEVAL_QUERY"}
    )
    assert out.embeddings[0].values == [1.0, 2.0]
    assert (out.model, out.dimension, out.provider) == ("e1", 2, "p")
    assert http.calls[0]["json"] == {"texts": ["q"], "query": True}


def test_gateway_errors_carry_the_backend_detail(http):
    http.replies.append(_response(503, {"detail": "No AI provider configured"}))
    with pytest.raises(ai_gateway_client.GatewayError, match="No AI provider configured"):
        ai_gateway_client.GatewayClient().models.generate_content(contents="hi")


def test_provider_info_is_cached_and_none_when_unavailable(http):
    info = {"provider": "p", "chat_model": "c", "embedding_model": "e", "dimension": 2}
    http.replies.append(_response(200, info))
    assert ai_gateway_client.is_configured() is True
    assert ai_gateway_client.chat_model() == "c"
    assert len(http.calls) == 1  # second lookup served from the cache
    ai_gateway_client.clear_cache()
    http.replies.append(_response(503, {"detail": "x"}))
    assert ai_gateway_client.is_configured() is False
    assert ai_gateway_client.chat_model() is None


# ── backend provider client ───────────────────────────────────────────────────


class _Provider(AIProvider):
    name = "stub"
    chat_model = "stub-chat"
    embedding_model = "stub-embed"
    dimension = 3

    def __init__(self):
        self.seen: list = []

    def generate(self, prompt, *, system=None, model=None, temperature=None):
        self.seen.append((prompt, system, model, temperature))
        return "text"

    def embed_texts(self, texts, *, query=False):
        self.seen.append((list(texts), query))
        return [[0.0, 1.0, 2.0] for _ in texts]


def test_provider_client_adapts_the_provider_to_the_old_client_shape():
    provider = _Provider()
    client = ProviderClient(provider)
    assert (
        client.models.generate_content(
            model="m", contents="hi", config={"system_instruction": "s", "temperature": 0.1}
        ).text
        == "text"
    )
    assert provider.seen[-1] == ("hi", "s", "m", 0.1)
    out = client.models.embed_content(
        model="ignored", contents="q", config={"task_type": "RETRIEVAL_QUERY"}
    )
    assert provider.seen[-1] == (["q"], True)
    assert out.embeddings[0].values == [0.0, 1.0, 2.0]
    assert (out.model, out.dimension, out.provider) == ("stub-embed", 3, "stub")
    client.models.embed_content(contents=["a", "b"])
    assert provider.seen[-1] == (["a", "b"], False)
