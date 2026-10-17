"""Admin Panel API for choosing and configuring the AI provider (superadmin only)."""

from __future__ import annotations

from typing import Any

import ai_service
from ai_providers import ProviderError, build_provider, save_provider_config
from ai_providers.registry import (
    PROVIDERS,
    active_provider_name,
    describe_providers,
    effective_provider_config,
    load_provider_config,
    validate_provider_updates,
)
from auth import require_superadmin
from database import get_db
from fastapi import APIRouter, Depends, HTTPException
from models import User
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter()


class SaveProviderRequest(BaseModel):
    provider: str
    config: dict[str, Any] = Field(default_factory=dict)
    activate: bool = True


class TestProviderRequest(BaseModel):
    provider: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


def _validated(provider: str, config: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_provider_updates(provider, config)
    except ProviderError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
def get_ai_provider_settings(
    db: Session = Depends(get_db), _: User = Depends(require_superadmin)
) -> dict[str, Any]:
    """Active provider plus each provider's fields and current values (secrets masked)."""
    return describe_providers(db)


@router.put("")
def save_ai_provider_settings(
    req: SaveProviderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
) -> dict[str, Any]:
    """Save one provider's settings (encrypted where secret) and, by default, make it active."""
    if req.provider not in PROVIDERS:
        raise HTTPException(400, f"Unknown AI provider '{req.provider}'")
    updates = _validated(req.provider, req.config)
    save_provider_config(db, req.provider, updates, current_user.id, activate=req.activate)
    db.commit()
    ai_service.clear_model_cache()
    return describe_providers(db)


@router.post("/test")
def test_ai_provider(
    req: TestProviderRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
) -> dict[str, Any]:
    """Try a provider with its saved settings plus any unsaved overrides, without saving."""
    name = req.provider or active_provider_name(db)
    if name not in PROVIDERS:
        raise HTTPException(400, f"Unknown AI provider '{name}'")
    overrides = _validated(name, req.config)
    _, stored = load_provider_config(db, name)
    cfg = effective_provider_config(name, stored, overrides)
    try:
        provider = build_provider(name, cfg)
    except ProviderError as exc:
        return {"ok": False, "provider": name, "message": str(exc)}
    return provider.test_connection()
