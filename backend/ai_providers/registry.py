from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from auth import (
    GEMINI_API_KEY_SETTING,
    GEMINI_MODEL_SETTING,
    decrypt_secret,
    encrypt_secret,
    mask_secret,
)
from database import SessionLocal
from models import SystemSetting
from sqlalchemy.orm import Session

from ai_providers.base import AIProvider, ProviderError
from ai_providers.gemini import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_DIMENSION,
    DEFAULT_EMBEDDING_MODEL,
    GeminiProvider,
)
from ai_providers.http_providers import OllamaProvider, OpenAICompatibleProvider

AI_PROVIDER_SETTING = "ai_provider"
AI_PROVIDER_CONFIG_SETTING = "ai_provider_config"
DEFAULT_PROVIDER = "gemini"


@dataclass(frozen=True)
class FieldSpec:
    """One setting of a provider, described so the Admin Panel can render it generically."""

    name: str
    label: str
    kind: str = "text"  # "secret" | "text" | "int"
    required: bool = False
    default: Any = None
    help: str = ""
    placeholder: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    fields: tuple[FieldSpec, ...]
    build: Callable[[dict[str, Any]], AIProvider]

    @property
    def secret_fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.kind == "secret")


def _build_gemini(cfg: dict[str, Any]) -> AIProvider:
    return GeminiProvider(
        api_key=str(cfg.get("api_key") or ""),
        chat_model=str(cfg.get("chat_model") or DEFAULT_CHAT_MODEL),
        embedding_model=str(cfg.get("embedding_model") or DEFAULT_EMBEDDING_MODEL),
        dimension=int(cfg.get("dimension") or DEFAULT_DIMENSION),
    )


def _http_fields(base_url_placeholder: str, base_url_help: str) -> tuple[FieldSpec, ...]:
    return (
        FieldSpec(
            "base_url",
            "Base URL",
            required=True,
            placeholder=base_url_placeholder,
            help=base_url_help,
        ),
        FieldSpec(
            "api_key",
            "API key / token",
            "secret",
            help="Optional. Sent as a bearer token, for servers behind an authenticating proxy.",
        ),
        FieldSpec("chat_model", "Chat model", required=True, placeholder="llama3.2"),
        FieldSpec(
            "embedding_model", "Embedding model", required=True, placeholder="nomic-embed-text"
        ),
        FieldSpec(
            "dimension",
            "Embedding dimension",
            "int",
            help="Optional. Leave blank to detect it from the model automatically.",
        ),
        FieldSpec(
            "query_prefix",
            "Query prefix",
            help="Optional text put before search questions, for models trained with one "
            "(for example `search_query: ` for nomic-embed-text).",
        ),
        FieldSpec(
            "document_prefix",
            "Document prefix",
            help="Optional text put before stored documents (for example `search_document: `).",
        ),
    )


def _build_http(cls):
    def build(cfg: dict[str, Any]) -> AIProvider:
        return cls(
            base_url=str(cfg.get("base_url") or ""),
            chat_model=str(cfg.get("chat_model") or ""),
            embedding_model=str(cfg.get("embedding_model") or ""),
            api_key=str(cfg.get("api_key") or ""),
            dimension=int(cfg["dimension"]) if cfg.get("dimension") else None,
            query_prefix=str(cfg.get("query_prefix") or ""),
            document_prefix=str(cfg.get("document_prefix") or ""),
        )

    return build


PROVIDERS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        "gemini",
        "Google Gemini",
        (
            FieldSpec(
                "api_key",
                "API key",
                "secret",
                required=True,
                help="From Google AI Studio. Stored encrypted; never shown again.",
            ),
            FieldSpec(
                "chat_model",
                "Chat model",
                default=DEFAULT_CHAT_MODEL,
                help="Used by SQL AI functions, the SQL assistant, Genie and pipelines.",
            ),
            FieldSpec("embedding_model", "Embedding model", default=DEFAULT_EMBEDDING_MODEL),
            FieldSpec(
                "dimension",
                "Embedding dimension",
                "int",
                default=DEFAULT_DIMENSION,
                help="Length of each vector. Searches only compare vectors of the same size.",
            ),
        ),
        _build_gemini,
    ),
    "ollama": ProviderSpec(
        "ollama",
        "Ollama (local models)",
        _http_fields(
            "http://ollama:11434",
            "Where Ollama listens, as seen from the backend (for example http://ollama:11434).",
        ),
        _build_http(OllamaProvider),
    ),
    "openai_compatible": ProviderSpec(
        "openai_compatible",
        "OpenAI-compatible server",
        _http_fields(
            "http://vllm:8000/v1",
            "Any server that implements the OpenAI API (vLLM, LocalAI, LM Studio, Groq, DeepSeek, ...). "
            "`/v1` is added if missing.",
        ),
        _build_http(OpenAICompatibleProvider),
    ),
}


def build_provider(name: str, cfg: dict[str, Any]) -> AIProvider:
    spec = PROVIDERS.get(name)
    if spec is None:
        raise ProviderError(
            f"Unknown AI provider '{name}'. Available: {', '.join(sorted(PROVIDERS))}"
        )
    return spec.build(cfg)


@contextmanager
def _session(db: Session | None):
    if db is not None:
        yield db
        return
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _get_setting(db: Session, key: str) -> str:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return (row.value_str or "").strip() if row else ""


def _set_setting(db: Session, key: str, value: str | None, user_id: uuid.UUID | None) -> None:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row:
        row.value_str = value
        row.updated_at = datetime.now(UTC)
        row.updated_by = user_id
    else:
        db.add(SystemSetting(key=key, value=None, value_str=value, updated_by=user_id))


def _read_stored(db: Session) -> dict[str, dict[str, Any]]:
    raw = _get_setting(db, AI_PROVIDER_CONFIG_SETTING)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _decrypt_fields(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    for field in PROVIDERS[name].secret_fields if name in PROVIDERS else ():
        if out.get(field):
            out[field] = decrypt_secret(str(out[field]))
    return out


def load_provider_config(
    db: Session | None = None, name: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Return ``(provider name, decrypted settings)``.

    Precedence: Admin Panel settings, then the older Gemini Admin Panel settings, then
    environment variables (``LLM_PROVIDER``, ``LLM_*``, ``GEMINI_API_KEY``) as defaults.
    Pass ``name`` to read another provider's settings instead of the active one.
    """
    with _session(db) as session:
        active = (
            _get_setting(session, AI_PROVIDER_SETTING) or os.getenv("LLM_PROVIDER", "").strip()
        ) or DEFAULT_PROVIDER
        name = (name or active).lower()
        cfg = _decrypt_fields(name, _read_stored(session).get(name, {}))

        if name == "gemini":
            if not cfg.get("api_key"):
                legacy = _get_setting(session, GEMINI_API_KEY_SETTING)
                cfg["api_key"] = decrypt_secret(legacy) if legacy else ""
            if not cfg.get("chat_model"):
                cfg["chat_model"] = _get_setting(session, GEMINI_MODEL_SETTING)

    if name != active.lower():
        return name, cfg  # environment defaults describe the active provider only
    env_defaults = {
        "api_key": os.getenv("LLM_API_KEY")
        or (os.getenv("GEMINI_API_KEY") if name == "gemini" else ""),
        "chat_model": os.getenv("LLM_MODEL"),
        "embedding_model": os.getenv("LLM_EMBEDDING_MODEL"),
        "dimension": os.getenv("LLM_EMBEDDING_DIMENSION"),
        "base_url": os.getenv("LLM_BASE_URL"),
    }
    for field, value in env_defaults.items():
        if not cfg.get(field) and value:
            cfg[field] = value.strip() if isinstance(value, str) else value
    return name, cfg


def get_active_provider(db: Session | None = None) -> AIProvider:
    """Build the provider currently selected in the Admin Panel (or the environment)."""
    name, cfg = load_provider_config(db)
    return build_provider(name, cfg)


def save_provider_config(
    db: Session,
    name: str,
    updates: dict[str, Any],
    user_id: uuid.UUID | None = None,
    activate: bool = True,
) -> None:
    """Store settings for one provider and (by default) make it the active one.

    Secret fields are encrypted at rest. For a secret field: a missing key or an empty string keeps
    the stored value, and ``None`` removes it. The caller commits.
    """
    spec = PROVIDERS.get(name)
    if spec is None:
        raise ProviderError(f"Unknown AI provider '{name}'")
    stored = _read_stored(db)
    current = dict(stored.get(name, {}))
    for field, value in updates.items():
        if field in spec.secret_fields:
            if value is None:
                current.pop(field, None)
            elif str(value).strip():
                current[field] = encrypt_secret(str(value).strip())
        elif value is None:
            current.pop(field, None)
        else:
            current[field] = value
    stored[name] = current
    _set_setting(db, AI_PROVIDER_CONFIG_SETTING, json.dumps(stored), user_id)
    if activate:
        _set_setting(db, AI_PROVIDER_SETTING, name, user_id)


def public_provider_config(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Settings safe to show in the UI: secrets are masked and reported as configured or not."""
    spec = PROVIDERS.get(name)
    secrets = spec.secret_fields if spec else ()
    out: dict[str, Any] = {"provider": name}
    for field, value in cfg.items():
        if field in secrets:
            out[f"{field}_configured"] = bool(value)
            out[f"{field}_masked"] = mask_secret(str(value)) if value else None
        else:
            out[field] = value
    for field in secrets:
        out.setdefault(f"{field}_configured", False)
        out.setdefault(f"{field}_masked", None)
    return out


def active_provider_name(db: Session | None = None) -> str:
    with _session(db) as session:
        name = _get_setting(session, AI_PROVIDER_SETTING) or os.getenv("LLM_PROVIDER", "").strip()
    return (name or DEFAULT_PROVIDER).lower()


def validate_provider_updates(name: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Check and normalise settings submitted for provider ``name``.

    Unknown fields are rejected. Integers are parsed (and must be positive). A blank text field
    resets it to its default (``None``); a blank secret is dropped so the stored one is kept; a
    secret sent as ``None`` clears it. Raises ``ProviderError`` with a user-readable message.
    """
    spec = PROVIDERS.get(name)
    if spec is None:
        raise ProviderError(f"Unknown AI provider '{name}'")
    fields = {f.name: f for f in spec.fields}
    cleaned: dict[str, Any] = {}
    for key, raw in updates.items():
        field = fields.get(key)
        if field is None:
            raise ProviderError(f"Unknown setting '{key}' for {spec.label}")
        if field.kind == "secret":
            if raw is None:
                cleaned[key] = None
            elif str(raw).strip():
                cleaned[key] = str(raw).strip()
        elif field.kind == "int":
            if raw is None or str(raw).strip() == "":
                cleaned[key] = None
                continue
            try:
                value = int(str(raw).strip())
            except ValueError as exc:
                raise ProviderError(f"{field.label} must be a whole number") from exc
            if not 1 <= value <= 65536:
                raise ProviderError(f"{field.label} must be between 1 and 65536")
            cleaned[key] = value
        else:
            text = "" if raw is None else str(raw).strip()
            if len(text) > 2000:
                raise ProviderError(f"{field.label} is too long")
            cleaned[key] = text or None
    return cleaned


def effective_provider_config(
    name: str, stored: dict[str, Any], overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Stored settings with unsaved ``overrides`` applied (a blank secret keeps the stored one)."""
    spec = PROVIDERS[name]
    cfg = dict(stored)
    for key, value in (overrides or {}).items():
        if key in spec.secret_fields and not (value or "").strip():
            continue
        if value is None or value == "":
            cfg.pop(key, None)
        else:
            cfg[key] = value
    return cfg


def describe_providers(db: Session | None = None) -> dict[str, Any]:
    """Everything the Admin Panel needs: the active provider and each provider's fields and values."""
    active = active_provider_name(db)
    providers = []
    for name, spec in PROVIDERS.items():
        _, cfg = load_provider_config(db, name)
        shown = public_provider_config(name, cfg)
        providers.append(
            {
                "name": name,
                "label": spec.label,
                "fields": [
                    {
                        "name": f.name,
                        "label": f.label,
                        "kind": f.kind,
                        "required": f.required,
                        "default": f.default,
                        "help": f.help,
                        "placeholder": f.placeholder,
                    }
                    for f in spec.fields
                ],
                "config": shown,
                "configured": all(bool(cfg.get(f.name)) for f in spec.fields if f.required),
            }
        )
    return {"active": active, "providers": providers}
