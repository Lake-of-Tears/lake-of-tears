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

AI_PROVIDER_SETTING = "ai_provider"
AI_PROVIDER_CONFIG_SETTING = "ai_provider_config"
DEFAULT_PROVIDER = "gemini"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    secret_fields: tuple[str, ...]
    build: Callable[[dict[str, Any]], AIProvider]


def _build_gemini(cfg: dict[str, Any]) -> AIProvider:
    return GeminiProvider(
        api_key=str(cfg.get("api_key") or ""),
        chat_model=str(cfg.get("chat_model") or DEFAULT_CHAT_MODEL),
        embedding_model=str(cfg.get("embedding_model") or DEFAULT_EMBEDDING_MODEL),
        dimension=int(cfg.get("dimension") or DEFAULT_DIMENSION),
    )


PROVIDERS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec("gemini", "Google Gemini", ("api_key",), _build_gemini),
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


def load_provider_config(db: Session | None = None) -> tuple[str, dict[str, Any]]:
    """Return ``(provider name, decrypted settings)``.

    Precedence: Admin Panel settings, then the older Gemini Admin Panel settings, then
    environment variables (``LLM_PROVIDER``, ``LLM_*``, ``GEMINI_API_KEY``) as defaults.
    """
    with _session(db) as session:
        name = _get_setting(session, AI_PROVIDER_SETTING) or os.getenv("LLM_PROVIDER", "").strip()
        name = (name or DEFAULT_PROVIDER).lower()
        cfg = _decrypt_fields(name, _read_stored(session).get(name, {}))

        if name == "gemini":
            if not cfg.get("api_key"):
                legacy = _get_setting(session, GEMINI_API_KEY_SETTING)
                cfg["api_key"] = decrypt_secret(legacy) if legacy else ""
            if not cfg.get("chat_model"):
                cfg["chat_model"] = _get_setting(session, GEMINI_MODEL_SETTING)

    env_defaults = {
        "api_key": os.getenv("LLM_API_KEY")
        or (os.getenv("GEMINI_API_KEY") if name == "gemini" else ""),
        "chat_model": os.getenv("LLM_MODEL"),
        "embedding_model": os.getenv("LLM_EMBEDDING_MODEL"),
        "dimension": os.getenv("LLM_EMBEDDING_DIMENSION"),
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
