from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import ai_gateway_router
import auth
import pytest
from ai_providers import (
    PROVIDERS,
    AIProvider,
    ProviderError,
    ProviderNotConfigured,
    build_provider,
    load_provider_config,
    public_provider_config,
    save_provider_config,
)
from ai_providers.gemini import GeminiProvider
from ai_providers.registry import AI_PROVIDER_CONFIG_SETTING, AI_PROVIDER_SETTING
from database import get_db
from fastapi import FastAPI
from fastapi.testclient import TestClient
from models import SystemSetting

KEY = "AIzaSy-test-key-1234"


class FakeDB:
    """Just enough of a SQLAlchemy session for the system_settings table."""

    def __init__(self) -> None:
        self.rows: dict[str, SystemSetting] = {}

    def query(self, model):
        assert model is SystemSetting
        rows = self.rows

        class _Query:
            def filter(self, cond):
                key = cond.right.value

                class _First:
                    def first(self):
                        return rows.get(key)

                return _First()

        return _Query()

    def add(self, obj) -> None:
        self.rows[obj.key] = obj


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_EMBEDDING_MODEL",
        "LLM_EMBEDDING_DIMENSION",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def fake_client(dimension: int = 768, text: str = "OK") -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value.text = text
    client.models.embed_content.side_effect = lambda model, contents, config: MagicMock(
        embeddings=[MagicMock(values=[0.1] * dimension) for _ in contents]
    )
    return client


def gemini(client: MagicMock, **kwargs: Any) -> GeminiProvider:
    return GeminiProvider(KEY, client_factory=lambda _key: client, **kwargs)


# ── GeminiProvider ────────────────────────────────────────────────────────────


def test_gemini_embeds_documents_and_queries_with_the_right_task_and_dimension():
    client = fake_client()
    provider = gemini(client)
    assert len(provider.embed_texts(["a", "b"])) == 2
    doc_cfg = client.models.embed_content.call_args.kwargs["config"]
    assert doc_cfg == {"task_type": "RETRIEVAL_DOCUMENT", "output_dimensionality": 768}
    provider.embed_texts(["q"], query=True)
    assert client.models.embed_content.call_args.kwargs["config"]["task_type"] == "RETRIEVAL_QUERY"
    assert client.models.embed_content.call_args.kwargs["model"] == "gemini-embedding-001"


def test_gemini_embed_rejects_vectors_of_the_wrong_length():
    provider = gemini(fake_client(dimension=3072))
    with pytest.raises(ProviderError, match="3072 dimensions, expected 768"):
        provider.embed_texts(["a"])


def test_gemini_embed_of_nothing_makes_no_call():
    client = fake_client()
    assert gemini(client).embed_texts([]) == []
    client.models.embed_content.assert_not_called()


def test_gemini_generate_passes_model_system_prompt_and_temperature():
    client = fake_client(text="  hello ")
    provider = gemini(client, chat_model="chat-default")
    assert provider.generate("hi", system="be brief", temperature=0.2) == "hello"
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs == {
        "model": "chat-default",
        "contents": "hi",
        "config": {"system_instruction": "be brief", "temperature": 0.2},
    }
    provider.generate("hi", model="other")
    assert client.models.generate_content.call_args.kwargs["model"] == "other"
    assert "config" not in client.models.generate_content.call_args.kwargs


def test_gemini_requires_a_key():
    with pytest.raises(ProviderNotConfigured):
        GeminiProvider("   ")


def test_provider_errors_never_contain_the_api_key():
    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError(f"401 for key {KEY}")
    provider = gemini(client)
    with pytest.raises(ProviderError) as exc:
        provider.generate("hi")
    assert KEY not in str(exc.value)
    result = provider.test_connection()
    assert result["ok"] is False
    assert KEY not in json.dumps(result)


def test_test_connection_reports_success():
    result = gemini(fake_client(text="OK")).test_connection()
    assert result["ok"] is True
    assert result["provider"] == "gemini"


def test_info_describes_the_provider_without_secrets():
    info = gemini(fake_client(), chat_model="m", dimension=1024).info().as_dict()
    assert info == {
        "provider": "gemini",
        "chat_model": "m",
        "embedding_model": "gemini-embedding-001",
        "dimension": 1024,
    }


# ── registry and settings ─────────────────────────────────────────────────────


def test_unknown_provider_is_rejected():
    with pytest.raises(ProviderError, match="Unknown AI provider 'nope'"):
        build_provider("nope", {})
    with pytest.raises(ProviderError):
        save_provider_config(FakeDB(), "nope", {})


def test_saved_secrets_are_encrypted_and_load_returns_them_decrypted():
    db = FakeDB()
    save_provider_config(db, "gemini", {"api_key": KEY, "chat_model": "m1", "dimension": 768})
    stored = db.rows[AI_PROVIDER_CONFIG_SETTING].value_str
    assert KEY not in stored
    assert json.loads(stored)["gemini"]["api_key"].startswith("v1:")
    assert db.rows[AI_PROVIDER_SETTING].value_str == "gemini"
    name, cfg = load_provider_config(db)
    assert name == "gemini"
    assert cfg["api_key"] == KEY
    assert cfg["chat_model"] == "m1"


def test_saving_blank_secret_keeps_it_and_none_clears_it():
    db = FakeDB()
    save_provider_config(db, "gemini", {"api_key": KEY})
    save_provider_config(db, "gemini", {"api_key": "", "chat_model": "m2"})
    assert load_provider_config(db)[1]["api_key"] == KEY
    save_provider_config(db, "gemini", {"api_key": None})
    assert load_provider_config(db)[1].get("api_key", "") == ""


def test_admin_panel_settings_beat_legacy_gemini_settings_which_beat_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    db = FakeDB()
    # 1) nothing stored: environment defaults
    name, cfg = load_provider_config(db)
    assert (name, cfg["api_key"], cfg["chat_model"]) == ("gemini", "env-key", "env-model")
    # 2) the older Gemini Admin Panel settings
    db.add(
        SystemSetting(key=auth.GEMINI_API_KEY_SETTING, value_str=auth.encrypt_secret("legacy-key"))
    )
    db.add(SystemSetting(key=auth.GEMINI_MODEL_SETTING, value_str="legacy-model"))
    _, cfg = load_provider_config(db)
    assert (cfg["api_key"], cfg["chat_model"]) == ("legacy-key", "legacy-model")
    # 3) the new provider settings win
    save_provider_config(db, "gemini", {"api_key": "new-key", "chat_model": "new-model"})
    _, cfg = load_provider_config(db)
    assert (cfg["api_key"], cfg["chat_model"]) == ("new-key", "new-model")


def test_environment_can_pick_the_provider_but_the_admin_panel_overrides_it(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "unknown-from-env")
    assert load_provider_config(FakeDB())[0] == "unknown-from-env"
    db = FakeDB()
    save_provider_config(db, "gemini", {"api_key": KEY})
    assert load_provider_config(db)[0] == "gemini"


def test_public_config_masks_secrets():
    shown = public_provider_config("gemini", {"api_key": KEY, "chat_model": "m"})
    assert KEY not in json.dumps(shown)
    assert shown["api_key_configured"] is True
    assert shown["api_key_masked"].endswith(KEY[-4:])
    assert shown["chat_model"] == "m"
    empty = public_provider_config("gemini", {})
    assert empty["api_key_configured"] is False
    assert empty["api_key_masked"] is None


def test_gemini_is_registered():
    assert PROVIDERS["gemini"].secret_fields == ("api_key",)


# ── internal gateway ──────────────────────────────────────────────────────────


class StubProvider(AIProvider):
    name = "stub"
    chat_model = "stub-chat"
    embedding_model = "stub-embed"
    dimension = 4

    def generate(self, prompt, *, system=None, model=None, temperature=None):
        return f"answer:{prompt}:{system}:{model}:{temperature}"

    def embed_texts(self, texts, *, query=False):
        return [[float(len(t)), 1.0 if query else 0.0, 0.0, 0.0] for t in texts]


@pytest.fixture
def gateway(monkeypatch):
    app = FastAPI()
    app.include_router(ai_gateway_router.router, prefix="/api/internal/ai")
    app.dependency_overrides[get_db] = lambda: FakeDB()
    monkeypatch.setattr(ai_gateway_router, "get_active_provider", lambda db: StubProvider())
    return TestClient(app)


AUTH = {"X-Internal-Secret": auth.SECRET_KEY}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/internal/ai/info", None),
        ("post", "/api/internal/ai/embed", {"texts": ["a"]}),
        ("post", "/api/internal/ai/generate", {"prompt": "hi"}),
    ],
)
def test_gateway_requires_the_internal_secret(gateway, method, path, body):
    call = getattr(gateway, method)
    kwargs = {"json": body} if body is not None else {}
    assert call(path, **kwargs).status_code == 401
    assert call(path, headers={"X-Internal-Secret": "wrong"}, **kwargs).status_code == 401
    # a signed-in user's session token must not be enough
    import jwt

    token = jwt.encode({"sub": "00000000-0000-0000-0000-000000000001"}, auth.SECRET_KEY, "HS256")
    assert call(path, headers={"Authorization": f"Bearer {token}"}, **kwargs).status_code == 401
    assert call(path, cookies={"lake_token": token}, **kwargs).status_code == 401


def test_gateway_info_embed_and_generate(gateway):
    assert gateway.get("/api/internal/ai/info", headers=AUTH).json() == {
        "provider": "stub",
        "chat_model": "stub-chat",
        "embedding_model": "stub-embed",
        "dimension": 4,
    }
    emb = gateway.post(
        "/api/internal/ai/embed", headers=AUTH, json={"texts": ["abc", "de"], "query": True}
    ).json()
    assert emb["embeddings"] == [[3.0, 1.0, 0.0, 0.0], [2.0, 1.0, 0.0, 0.0]]
    assert emb["dimension"] == 4
    gen = gateway.post(
        "/api/internal/ai/generate",
        headers=AUTH,
        json={"prompt": "hi", "system": "s", "model": "m", "temperature": 0.5},
    ).json()
    assert gen["text"] == "answer:hi:s:m:0.5"
    assert gen["provider"] == "stub"


def test_gateway_validates_input(gateway):
    assert (
        gateway.post("/api/internal/ai/embed", headers=AUTH, json={"texts": []}).status_code == 422
    )
    too_many = {"texts": ["x"] * (ai_gateway_router.MAX_EMBED_BATCH + 1)}
    assert gateway.post("/api/internal/ai/embed", headers=AUTH, json=too_many).status_code == 422
    assert (
        gateway.post("/api/internal/ai/generate", headers=AUTH, json={"prompt": ""}).status_code
        == 422
    )


def test_gateway_maps_provider_failures(gateway, monkeypatch):
    def not_configured(db):
        raise ProviderNotConfigured("no key")

    monkeypatch.setattr(ai_gateway_router, "get_active_provider", not_configured)
    assert gateway.get("/api/internal/ai/info", headers=AUTH).status_code == 503

    class Failing(StubProvider):
        def generate(self, *args, **kwargs):
            raise ProviderError("upstream down")

        def embed_texts(self, *args, **kwargs):
            raise ProviderError("upstream down")

    monkeypatch.setattr(ai_gateway_router, "get_active_provider", lambda db: Failing())
    assert (
        gateway.post("/api/internal/ai/generate", headers=AUTH, json={"prompt": "x"}).status_code
        == 502
    )
    assert (
        gateway.post("/api/internal/ai/embed", headers=AUTH, json={"texts": ["x"]}).status_code
        == 502
    )
