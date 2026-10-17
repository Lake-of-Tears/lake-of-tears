from __future__ import annotations

import json

import httpx
import pytest
from ai_providers import PROVIDERS, ProviderError, ProviderNotConfigured, build_provider
from ai_providers.http_providers import AzureOpenAIProvider, OpenAIProvider, clear_dimension_cache
from ai_providers.registry import describe_providers
from test_ai_http_providers import Server, openai_handler
from test_ai_providers import FakeDB

KEY = "sk-azure-secret-1234"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    clear_dimension_cache()
    for name in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
    clear_dimension_cache()


def test_openai_uses_the_hosted_address_bearer_key_and_1536_default():
    server = Server(openai_handler(dimension=1536))
    p = OpenAIProvider(api_key=KEY, transport=server.transport)
    assert p.chat_model == "gpt-4o-mini"
    assert p.embedding_model == "text-embedding-3-small"
    assert p.dimension == 1536
    assert p.embed_texts(["a", "b"]) == [[0.0] * 1536, [1.0] * 1536]
    req = server.requests[-1]
    assert str(req.url) == "https://api.openai.com/v1/embeddings"
    assert req.headers["authorization"] == f"Bearer {KEY}"


def test_openai_requires_a_key():
    with pytest.raises(ProviderNotConfigured):
        OpenAIProvider(api_key="")


def test_azure_addresses_deployments_with_api_version_and_api_key_header():
    server = Server(openai_handler(dimension=4))
    p = AzureOpenAIProvider(
        endpoint="https://res.openai.azure.com/",
        api_key=KEY,
        chat_deployment="chat-dep",
        embedding_deployment="embed-dep",
        api_version="2024-10-21",
        transport=server.transport,
    )
    assert p.generate("hi", system="be brief", temperature=0.2) == "hello"
    chat = server.requests[-1]
    assert str(chat.url) == (
        "https://res.openai.azure.com/openai/deployments/chat-dep/chat/completions"
        "?api-version=2024-10-21"
    )
    assert chat.headers["api-key"] == KEY
    assert "authorization" not in chat.headers
    assert "model" not in json.loads(chat.content)

    assert p.embed_texts(["x"]) == [[0.0] * 4]
    assert "/deployments/embed-dep/embeddings?api-version=2024-10-21" in str(
        server.requests[-1].url
    )
    assert p.dimension == 4


def test_azure_errors_never_leak_the_key():
    def reject(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}})

    p = AzureOpenAIProvider(
        endpoint="https://res.openai.azure.com",
        api_key=KEY,
        chat_deployment="c",
        embedding_deployment="e",
        transport=httpx.MockTransport(reject),
    )
    with pytest.raises(ProviderError) as exc:
        p.generate("hi")
    assert KEY not in str(exc.value)
    assert "401" in str(exc.value)


def test_registry_builds_both_and_hides_secrets():
    assert {"openai", "azure"} <= set(PROVIDERS)
    assert build_provider("openai", {"api_key": KEY}).dimension == 1536
    azure = build_provider(
        "azure",
        {
            "endpoint": "https://r.openai.azure.com",
            "api_key": KEY,
            "chat_model": "c",
            "embedding_model": "e",
            "dimension": 1536,
        },
    )
    assert azure.dimension == 1536
    with pytest.raises(ProviderNotConfigured):
        build_provider("azure", {"endpoint": "https://r", "api_key": KEY})
    assert KEY not in json.dumps(describe_providers(FakeDB()))


def test_environment_defaults_for_azure_and_openai(monkeypatch):
    from ai_providers import load_provider_config

    monkeypatch.setenv("LLM_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "c")
    monkeypatch.setenv("LLM_EMBEDDING_MODEL", "e")
    name, cfg = load_provider_config(FakeDB())
    assert name == "azure"
    assert cfg["endpoint"] == "https://r.openai.azure.com" and cfg["api_key"] == KEY
    assert build_provider(name, cfg).chat_model == "c"

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    name, cfg = load_provider_config(FakeDB())
    assert build_provider(name, cfg).dimension == 1536
