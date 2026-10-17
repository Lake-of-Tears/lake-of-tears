from __future__ import annotations

import json
import uuid

import ai_admin_router
import auth
import pytest
from ai_providers import AIProvider, ProviderError
from ai_providers.registry import (
    AI_PROVIDER_CONFIG_SETTING,
    AI_PROVIDER_SETTING,
    effective_provider_config,
    validate_provider_updates,
)
from database import get_db
from fastapi import FastAPI
from fastapi.testclient import TestClient
from models import User
from test_ai_providers import KEY, FakeDB, fake_client, gemini  # noqa: F401  (fixtures/helpers)

BASE = "/api/admin/ai-provider"


def _user(role: str) -> User:
    return User(
        id=uuid.uuid4(), email=f"{role}@lake.local", display_name=role, role=role, is_active=True
    )


@pytest.fixture
def admin(monkeypatch):
    """A client acting as a superadmin, with an in-memory settings store."""
    db = FakeDB()
    app = FastAPI()
    app.include_router(ai_admin_router.router, prefix=BASE)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[auth.get_current_user] = lambda: _user("superadmin")
    monkeypatch.setattr(ai_admin_router.ai_service, "clear_model_cache", lambda: None)
    for name in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(app)
    client.db = db  # type: ignore[attr-defined]
    return client


# ── access control ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("put", {"provider": "gemini", "config": {}}), ("post", {})],
)
def test_only_superadmins_can_use_the_provider_endpoints(method, body):
    app = FastAPI()
    app.include_router(ai_admin_router.router, prefix=BASE)
    app.dependency_overrides[get_db] = lambda: FakeDB()
    app.dependency_overrides[auth.get_current_user] = lambda: _user("user")
    client = TestClient(app)
    path = BASE + ("/test" if method == "post" else "")
    kwargs = {"json": body} if body is not None else {}
    assert getattr(client, method)(path, **kwargs).status_code == 403


# ── describe ──────────────────────────────────────────────────────────────────


def test_describe_lists_fields_and_never_returns_secrets(admin):
    admin.put(BASE, json={"provider": "gemini", "config": {"api_key": KEY, "chat_model": "m"}})
    data = admin.get(BASE).json()
    assert data["active"] == "gemini"
    gemini_info = data["providers"][0]
    assert gemini_info["name"] == "gemini"
    assert [f["name"] for f in gemini_info["fields"]] == [
        "api_key",
        "chat_model",
        "embedding_model",
        "dimension",
    ]
    assert {f["name"]: f["kind"] for f in gemini_info["fields"]}["api_key"] == "secret"
    assert gemini_info["configured"] is True
    assert gemini_info["config"]["api_key_configured"] is True
    assert gemini_info["config"]["chat_model"] == "m"
    assert KEY not in json.dumps(data)


def test_describe_reports_unconfigured_provider(admin):
    info = admin.get(BASE).json()["providers"][0]
    assert info["configured"] is False
    assert info["config"]["api_key_configured"] is False


# ── save ──────────────────────────────────────────────────────────────────────


def test_save_encrypts_secrets_and_activates_the_provider(admin):
    resp = admin.put(
        BASE,
        json={
            "provider": "gemini",
            "config": {"api_key": KEY, "dimension": "768", "chat_model": " c "},
        },
    )
    assert resp.status_code == 200
    stored = admin.db.rows[AI_PROVIDER_CONFIG_SETTING].value_str
    assert KEY not in stored
    cfg = json.loads(stored)["gemini"]
    assert cfg["api_key"].startswith("v1:")
    assert (cfg["dimension"], cfg["chat_model"]) == (768, "c")
    assert admin.db.rows[AI_PROVIDER_SETTING].value_str == "gemini"
    assert KEY not in resp.text


def test_save_can_keep_or_clear_a_secret(admin):
    admin.put(BASE, json={"provider": "gemini", "config": {"api_key": KEY}})
    admin.put(BASE, json={"provider": "gemini", "config": {"api_key": "", "chat_model": "x"}})
    assert admin.get(BASE).json()["providers"][0]["config"]["api_key_configured"] is True
    admin.put(BASE, json={"provider": "gemini", "config": {"api_key": None}})
    assert admin.get(BASE).json()["providers"][0]["config"]["api_key_configured"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "nope", "config": {}},
        {"provider": "gemini", "config": {"bogus": "x"}},
        {"provider": "gemini", "config": {"dimension": "abc"}},
        {"provider": "gemini", "config": {"dimension": 0}},
        {"provider": "gemini", "config": {"dimension": 70000}},
    ],
)
def test_save_rejects_invalid_input(admin, body):
    assert admin.put(BASE, json=body).status_code == 400
    assert admin.db.rows == {}


def test_validate_updates_normalises_values():
    assert validate_provider_updates(
        "gemini", {"chat_model": "  ", "dimension": "", "api_key": "  ", "embedding_model": " e "}
    ) == {"chat_model": None, "dimension": None, "embedding_model": "e"}
    with pytest.raises(ProviderError, match="Unknown setting"):
        validate_provider_updates("gemini", {"nope": 1})


def test_effective_config_applies_overrides_without_dropping_the_stored_secret():
    stored = {"api_key": "stored", "chat_model": "old"}
    cfg = effective_provider_config(
        "gemini", stored, {"api_key": "", "chat_model": "new", "dimension": 512}
    )
    assert cfg == {"api_key": "stored", "chat_model": "new", "dimension": 512}
    assert effective_provider_config("gemini", stored, {"chat_model": None}) == {
        "api_key": "stored"
    }


# ── test connection ───────────────────────────────────────────────────────────


class _Stub(AIProvider):
    name = "stub"
    chat_model = "stub-chat"
    embedding_model = "stub-embed"
    dimension = 3

    def generate(self, prompt, **kwargs):
        return "OK"

    def embed_texts(self, texts, *, query=False):
        return [[0.0, 0.0, 0.0]]


def test_test_endpoint_uses_saved_settings_plus_unsaved_overrides_without_saving(
    admin, monkeypatch
):
    admin.put(BASE, json={"provider": "gemini", "config": {"api_key": KEY, "chat_model": "saved"}})
    before = admin.db.rows[AI_PROVIDER_CONFIG_SETTING].value_str
    seen = {}

    def fake_build(name, cfg):
        seen.update(name=name, cfg=cfg)
        return _Stub()

    monkeypatch.setattr(ai_admin_router, "build_provider", fake_build)
    result = admin.post(
        BASE + "/test", json={"provider": "gemini", "config": {"chat_model": "trial"}}
    ).json()
    assert result["ok"] is True
    assert seen["cfg"]["chat_model"] == "trial"  # unsaved override
    assert seen["cfg"]["api_key"] == KEY  # stored secret kept
    assert admin.db.rows[AI_PROVIDER_CONFIG_SETTING].value_str == before  # nothing saved


def test_test_endpoint_reports_a_missing_key_without_raising(admin):
    result = admin.post(BASE + "/test", json={"provider": "gemini"}).json()
    assert result["ok"] is False
    assert "GEMINI_API_KEY" in result["message"]


def test_test_endpoint_rejects_unknown_provider_and_bad_fields(admin):
    assert admin.post(BASE + "/test", json={"provider": "nope"}).status_code == 400
    assert (
        admin.post(BASE + "/test", json={"provider": "gemini", "config": {"x": 1}}).status_code
        == 400
    )


# ── AIProvider.test_connection ────────────────────────────────────────────────


def test_connection_checks_chat_and_embeddings_with_latency():
    result = gemini(fake_client(text="OK")).test_connection()
    assert result["ok"] is True
    assert result["chat"]["ok"] and result["embedding"]["ok"]
    assert isinstance(result["chat"]["latency_ms"], int)
    assert result["embedding"]["dimension"] == 768
    assert "gemini-embedding-001" in result["message"]


def test_connection_flags_a_wrong_embedding_dimension():
    result = gemini(fake_client(dimension=3072)).test_connection()
    assert result["ok"] is False
    assert result["chat"]["ok"] is True
    assert "3072" in result["embedding"]["message"]


def test_connection_reports_which_half_failed_without_the_key():
    client = fake_client()
    client.models.generate_content.side_effect = RuntimeError(f"bad key {KEY}")
    result = gemini(client).test_connection()
    assert result["ok"] is False
    assert result["embedding"]["ok"] is True
    assert result["chat"]["ok"] is False
    assert KEY not in json.dumps(result)
