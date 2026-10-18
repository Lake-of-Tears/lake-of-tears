"""Talks to the backend AI gateway (``/api/internal/ai``) instead of a model vendor.

The UI holds no AI credentials: the backend uses whichever provider the Admin Panel selected.
``GatewayClient`` mimics the small part of the old ``google.genai`` client that the SQL assistant,
Genie and the AI SQL functions use (``client.models.generate_content`` and ``embed_content``), so
those code paths did not need rewriting.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import Any

import httpx

LEGACY_EMBEDDING_MODEL = "gemini-embedding-001"
_INFO_TTL = 30.0
_info_cache: tuple[dict[str, Any] | None, float] | None = None


class GatewayError(RuntimeError):
    """The AI gateway refused or failed a request."""


def _base() -> tuple[str, str]:
    return (
        os.getenv("BACKEND_URL", "http://backend:8000").rstrip("/"),
        os.getenv("AUTH_SECRET_KEY", "dev-secret-please-change-in-production"),
    )


def _call(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 120) -> dict:
    base, secret = _base()
    try:
        resp = httpx.request(
            method,
            f"{base}/api/internal/ai{path}",
            json=body,
            headers={"X-Internal-Secret": secret},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise GatewayError(f"AI gateway unreachable: {exc}") from exc
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail") or resp.reason_phrase
        except Exception:
            detail = resp.reason_phrase
        raise GatewayError(str(detail))
    return resp.json()


def provider_info() -> dict[str, Any] | None:
    """Active provider, models and embedding dimension, or ``None`` if no provider is usable.

    Cached briefly so that checking "is AI configured?" before each request stays cheap.
    """
    global _info_cache
    now = time.monotonic()
    if _info_cache is not None and now - _info_cache[1] < _INFO_TTL:
        return _info_cache[0]
    try:
        info: dict[str, Any] | None = _call("GET", "/info", timeout=3)
    except GatewayError:
        info = None
    _info_cache = (info, now)
    return info


def clear_cache() -> None:
    global _info_cache
    _info_cache = None


def is_configured() -> bool:
    return provider_info() is not None


def chat_model() -> str | None:
    info = provider_info()
    return info["chat_model"] if info else None


class _Models:
    def generate_content(self, model: str | None = None, contents: str = "", config: Any = None):
        cfg = config or {}
        body: dict[str, Any] = {"prompt": contents}
        if model:
            body["model"] = model
        if cfg.get("system_instruction"):
            body["system"] = cfg["system_instruction"]
        if cfg.get("temperature") is not None:
            body["temperature"] = cfg["temperature"]
        return SimpleNamespace(text=_call("POST", "/generate", body)["text"])

    def embed_content(self, model: str | None = None, contents: Any = None, config: Any = None):
        """The provider decides the embedding model and dimension; ``model`` is ignored."""
        texts = [contents] if isinstance(contents, str) else list(contents or [])
        query = (config or {}).get("task_type") == "RETRIEVAL_QUERY"
        resp = _call("POST", "/embed", {"texts": texts, "query": query})
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=v) for v in resp["embeddings"]],
            model=resp["embedding_model"],
            dimension=resp["dimension"],
            provider=resp["provider"],
        )


class GatewayClient:
    """Drop-in for the parts of ``genai.Client`` Lake of Tears used. Takes no credentials."""

    def __init__(self, api_key: str | None = None) -> None:
        self.models = _Models()


def vector_search_sql(dimension: int, has_model_column: bool) -> str:
    """Nearest-neighbour SQL over ``embeddings/**`` for one embedding dimension (and model).

    Parameters, in order: query vector, parquet path, model name (only if ``has_model_column``),
    row limit. Files written before models were recorded have no ``model`` column and count as
    ``LEGACY_EMBEDDING_MODEL``. Keep identical to ``pipeline/shared/ai_gateway.py`` (a test checks).
    """
    dim = int(dimension)
    model_filter = (
        f"AND coalesce(model, '{LEGACY_EMBEDDING_MODEL}') = ?" if has_model_column else ""
    )
    return f"""
        SELECT source, ingested_at, content,
               CASE WHEN len(embedding) = {dim}
                    THEN array_cosine_similarity(embedding::DOUBLE[{dim}], ?::DOUBLE[{dim}])
               END AS score
        FROM read_parquet(?, union_by_name = true)
        WHERE len(embedding) = {dim} {model_filter}
        ORDER BY score DESC
        LIMIT ?
    """
