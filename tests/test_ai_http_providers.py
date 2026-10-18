from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from ai_providers import (
    PROVIDERS,
    ProviderError,
    ProviderNotConfigured,
    build_provider,
    load_provider_config,
)
from ai_providers.http_providers import (
    OllamaProvider,
    OpenAICompatibleProvider,
    clear_dimension_cache,
)
from ai_providers.registry import describe_providers, save_provider_config
from test_ai_providers import FakeDB

KEY = "sk-local-secret-9876"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    clear_dimension_cache()
    for name in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
    clear_dimension_cache()


class Server:
    """Records requests and answers from a handler, through httpx's mock transport."""

    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self.handler = handler
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def openai_handler(dimension: int = 4):
    def handle(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "  hello  "}}]})
        if req.url.path.endswith("/embeddings"):
            # deliberately out of order: the provider must sort by "index"
            items = [
                {"index": i, "embedding": [float(i)] * dimension} for i in range(len(body["input"]))
            ]
            return httpx.Response(200, json={"data": list(reversed(items))})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    return handle


def ollama_handler(dimension: int = 4):
    def handle(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if req.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"role": "assistant", "content": " hi "}})
        if req.url.path == "/api/embed":
            return httpx.Response(
                200,
                json={"embeddings": [[float(i)] * dimension for i, _ in enumerate(body["input"])]},
            )
        return httpx.Response(404, json={"error": "not found"})

    return handle


def oai(server: Server, **kwargs: Any) -> OpenAICompatibleProvider:
    cfg = {
        "base_url": "http://llm:8000",
        "chat_model": "chat",
        "embedding_model": "embed",
        **kwargs,
    }
    return OpenAICompatibleProvider(transport=server.transport, **cfg)


def ollama(server: Server, **kwargs: Any) -> OllamaProvider:
    cfg = {
        "base_url": "http://ollama:11434",
        "chat_model": "llama3.2",
        "embedding_model": "nomic",
        **kwargs,
    }
    return OllamaProvider(transport=server.transport, **cfg)


# ── OpenAI-compatible ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://h:8000", "http://h:8000/v1/chat/completions"),
        ("http://h:8000/", "http://h:8000/v1/chat/completions"),
        ("http://h:8000/v1", "http://h:8000/v1/chat/completions"),
        ("https://api.groq.com/openai/v1/", "https://api.groq.com/openai/v1/chat/completions"),
    ],
)
def test_openai_compatible_normalises_the_base_url(given, expected):
    server = Server(openai_handler())
    oai(server, base_url=given).generate("hi")
    assert str(server.requests[0].url) == expected


def test_openai_compatible_chat_request_and_response():
    server = Server(openai_handler())
    text = oai(server, api_key=KEY).generate(
        "question", system="be brief", temperature=0.2, model="other"
    )
    assert text == "hello"
    assert server.body() == {
        "model": "other",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "question"},
        ],
        "temperature": 0.2,
    }
    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_no_authorization_header_without_an_api_key():
    server = Server(openai_handler())
    oai(server).generate("hi")
    assert "authorization" not in server.requests[0].headers


def test_openai_compatible_embeddings_are_returned_in_input_order_with_prefixes():
    server = Server(openai_handler())
    provider = oai(server, query_prefix="q: ", document_prefix="d: ", dimension=4)
    docs = provider.embed_texts(["a", "b", "c"])
    assert [v[0] for v in docs] == [0.0, 1.0, 2.0]
    assert server.body()["input"] == ["d: a", "d: b", "d: c"]
    provider.embed_texts(["question"], query=True)
    assert server.body()["input"] == ["q: question"]
    assert server.body()["model"] == "embed"


def test_dimension_is_detected_once_per_model_when_not_configured():
    server = Server(openai_handler(dimension=7))
    provider = oai(server)
    assert provider.dimension == 7
    assert provider.dimension == 7
    assert len(server.requests) == 1  # measured once, then cached
    assert oai(Server(openai_handler(dimension=99))).dimension == 7  # same model, served from cache


def test_a_configured_dimension_is_used_without_a_probe_and_enforced():
    server = Server(openai_handler(dimension=4))
    provider = oai(server, dimension=8)
    assert provider.dimension == 8
    assert server.requests == []
    with pytest.raises(ProviderError, match="returned 4 dimensions, expected 8"):
        provider.embed_texts(["x"])


def test_embed_of_nothing_makes_no_request():
    server = Server(openai_handler())
    assert oai(server).embed_texts([]) == []
    assert server.requests == []


def test_http_errors_carry_a_hint_and_never_the_key():
    def failing(req):
        return httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}})

    with pytest.raises(ProviderError) as exc:
        oai(Server(failing), api_key=KEY).generate("hi")
    assert "HTTP 401" in str(exc.value) and "API key" in str(exc.value)
    assert KEY not in str(exc.value)

    def missing(req):
        return httpx.Response(404, json={"error": {"message": "model 'chat' not found"}})

    with pytest.raises(ProviderError, match="pulled/loaded"):
        oai(Server(missing)).generate("hi")


def test_connection_errors_name_the_server():
    def refuse(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError, match="Could not connect to http://llm:8000/v1"):
        oai(Server(refuse)).generate("hi")


def test_unexpected_response_shapes_are_reported():
    server = Server(lambda req: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(ProviderError, match="/chat/completions"):
        oai(server).generate("hi")
    with pytest.raises(ProviderError, match="/embeddings"):
        oai(server, dimension=4).embed_texts(["x"])


def test_vectors_of_different_lengths_are_rejected():
    def uneven(req):
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [1.0, 2.0]}]
            },
        )

    with pytest.raises(ProviderError, match="different lengths"):
        oai(Server(uneven)).embed_texts(["a", "b"])


# ── Ollama ────────────────────────────────────────────────────────────────────


def test_ollama_chat_uses_the_native_api_without_streaming():
    server = Server(ollama_handler())
    assert ollama(server).generate("q", system="s", temperature=0.5) == "hi"
    assert str(server.requests[0].url) == "http://ollama:11434/api/chat"
    assert server.body() == {
        "model": "llama3.2",
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
        "stream": False,
        "options": {"temperature": 0.5},
    }


def test_ollama_embeds_in_one_batch_with_prefixes():
    server = Server(ollama_handler(dimension=3))
    provider = ollama(server, query_prefix="search_query: ", document_prefix="search_document: ")
    vectors = provider.embed_texts(["a", "b"])
    assert len(vectors) == 2 and len(vectors[0]) == 3
    assert str(server.requests[0].url) == "http://ollama:11434/api/embed"
    assert server.body() == {
        "model": "nomic",
        "input": ["search_document: a", "search_document: b"],
    }
    provider.embed_texts(["q"], query=True)
    assert server.body()["input"] == ["search_query: q"]


def test_ollama_reports_a_missing_model():
    server = Server(
        lambda req: httpx.Response(
            404, json={"error": 'model "llama3.2" not found, try pulling it first'}
        )
    )
    with pytest.raises(ProviderError, match="not found, try pulling it first"):
        ollama(server).generate("hi")


def test_connection_test_works_for_both_flavours():
    for provider in (
        oai(Server(openai_handler(dimension=5))),
        ollama(Server(ollama_handler(dimension=5))),
    ):
        result = provider.test_connection()
        assert result["ok"] is True, result
        assert result["embedding"]["dimension"] == 5


# ── registry and settings ─────────────────────────────────────────────────────


def test_both_providers_are_registered_with_required_fields():
    for name in ("ollama", "openai_compatible"):
        fields = {f.name: f for f in PROVIDERS[name].fields}
        assert fields["base_url"].required and fields["chat_model"].required
        assert fields["embedding_model"].required
        assert fields["api_key"].kind == "secret" and not fields["api_key"].required
        assert fields["dimension"].kind == "int" and not fields["dimension"].required
        assert set(fields) == {
            "base_url",
            "api_key",
            "chat_model",
            "embedding_model",
            "dimension",
            "query_prefix",
            "document_prefix",
        }


@pytest.mark.parametrize("name", ["ollama", "openai_compatible"])
def test_building_without_required_settings_says_what_is_missing(name):
    with pytest.raises(ProviderNotConfigured, match="base URL"):
        build_provider(name, {})
    with pytest.raises(ProviderNotConfigured, match="chat model"):
        build_provider(name, {"base_url": "http://x"})
    with pytest.raises(ProviderNotConfigured, match="embedding model"):
        build_provider(name, {"base_url": "http://x", "chat_model": "c"})
    provider = build_provider(
        name, {"base_url": "http://x", "chat_model": "c", "embedding_model": "e"}
    )
    assert (provider.name, provider.chat_model, provider.embedding_model) == (name, "c", "e")


def test_saved_token_is_encrypted_and_the_provider_can_be_activated():
    db = FakeDB()
    save_provider_config(
        db,
        "ollama",
        {
            "base_url": "http://ollama:11434",
            "api_key": KEY,
            "chat_model": "llama3.2",
            "embedding_model": "nomic",
        },
    )
    stored = db.rows["ai_provider_config"].value_str
    assert KEY not in stored
    name, cfg = load_provider_config(db)
    assert name == "ollama" and cfg["api_key"] == KEY and cfg["base_url"] == "http://ollama:11434"


def test_environment_variables_can_bootstrap_a_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5")
    name, cfg = load_provider_config(FakeDB())
    assert (name, cfg["base_url"], cfg["chat_model"]) == (
        "openai_compatible",
        "http://vllm:8000/v1",
        "qwen2.5",
    )
    # the generic settings describe the active provider only
    _, other = load_provider_config(FakeDB(), "ollama")
    assert "base_url" not in other


def test_the_admin_description_lists_every_provider():
    info = describe_providers(FakeDB())
    assert [p["name"] for p in info["providers"]] == [
        "gemini",
        "openai",
        "azure",
        "bedrock",
        "ollama",
        "openai_compatible",
    ]
    ollama_info = next(p for p in info["providers"] if p["name"] == "ollama")
    assert ollama_info["configured"] is False
    assert {f["name"] for f in ollama_info["fields"]} >= {
        "base_url",
        "chat_model",
        "embedding_model",
    }


# ── a configured provider whose server is down ────────────────────────────────


def _unreachable(req):
    raise httpx.ConnectError("refused")


def test_info_reports_a_missing_dimension_instead_of_failing_when_the_server_is_down():
    info = oai(Server(_unreachable)).info()
    assert info.dimension is None
    assert (info.provider, info.chat_model) == ("openai_compatible", "chat")


def test_connection_test_reports_an_unreachable_server_without_raising():
    result = ollama(Server(_unreachable)).test_connection()
    assert result["ok"] is False
    assert "Could not connect" in result["chat"]["message"]
    assert "Could not connect" in result["embedding"]["message"]
    assert result["embedding"]["dimension"] is None


def test_a_successful_embedding_teaches_the_provider_its_dimension_without_a_probe():
    server = Server(openai_handler(dimension=6))
    provider = oai(server)
    provider.embed_texts(["x"])
    assert len(server.requests) == 1
    assert provider.dimension == 6
    assert len(server.requests) == 1  # no extra probe request
