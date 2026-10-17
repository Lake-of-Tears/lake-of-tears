"""Internal AI gateway: the UI and the pipelines call these endpoints instead of a model vendor.

Credentials stay in the backend. These routes accept only the shared internal secret
(``X-Internal-Secret``); a user's session token is not enough, and no response contains a key.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from ai_providers import AIProvider, ProviderError, ProviderNotConfigured, get_active_provider
from auth import SECRET_KEY
from database import get_db
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.ai.gateway")

router = APIRouter()

MAX_EMBED_BATCH = 256


def require_internal_secret(x_internal_secret: str | None = Header(default=None)) -> None:
    if not x_internal_secret or not hmac.compare_digest(
        x_internal_secret.encode("utf-8"), SECRET_KEY.encode("utf-8")
    ):
        raise HTTPException(401, "Not authorized")


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_EMBED_BATCH)
    query: bool = False


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    system: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)


def _provider(db: Session) -> AIProvider:
    try:
        return get_active_provider(db)
    except ProviderNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/info", dependencies=[Depends(require_internal_secret)])
def provider_info(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Which provider, models and embedding dimension are active (no secrets)."""
    return _provider(db).info().as_dict()


@router.post("/embed", dependencies=[Depends(require_internal_secret)])
def embed(req: EmbedRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    provider = _provider(db)
    try:
        vectors = provider.embed_texts(req.texts, query=req.query)
    except ProviderError as exc:
        logger.warning("embed failed (%s): %s", provider.name, exc)
        raise HTTPException(502, str(exc)) from exc
    return {"embeddings": vectors, **provider.info().as_dict()}


@router.post("/generate", dependencies=[Depends(require_internal_secret)])
def generate(req: GenerateRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    provider = _provider(db)
    try:
        text = provider.generate(
            req.prompt, system=req.system, model=req.model, temperature=req.temperature
        )
    except ProviderError as exc:
        logger.warning("generate failed (%s): %s", provider.name, exc)
        raise HTTPException(502, str(exc)) from exc
    return {"text": text, **provider.info().as_dict()}
