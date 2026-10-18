"""Client for the Lake of Tears AI gateway (backend ``/api/internal/ai``).

Pipelines never talk to a model vendor or hold its credentials: they ask the backend, which uses
whichever provider the Admin Panel has selected. Only the standard library is needed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

EMBED_BATCH_SIZE = 100


class AIGatewayError(RuntimeError):
    """The gateway refused or failed a request."""


def _config() -> tuple[str, str]:
    base = os.getenv("BACKEND_URL", "http://backend:8000").rstrip("/")
    secret = os.getenv("AUTH_SECRET_KEY", "dev-secret-please-change-in-production")
    return base, secret


def _request(
    method: str, path: str, body: dict[str, Any] | None = None, timeout: int = 120
) -> dict:
    base, secret = _config()
    req = urllib.request.Request(  # noqa: S310 - fixed internal backend URL
        f"{base}/api/internal/ai{path}",
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json", "X-Internal-Secret": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except Exception:
            detail = ""
        raise AIGatewayError(f"AI gateway returned {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise AIGatewayError(f"AI gateway unreachable at {base}: {exc.reason}") from exc


def provider_info() -> dict[str, Any]:
    """Active provider, chat model, embedding model and embedding dimension."""
    return _request("GET", "/info", timeout=10)


def embed_texts_with_meta(texts: list[str], query: bool = False) -> dict[str, Any]:
    """Embed ``texts``; returns ``{"embeddings", "provider", "embedding_model", "dimension"}``."""
    vectors: list[list[float]] = []
    meta: dict[str, Any] = {}
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        part = texts[start : start + EMBED_BATCH_SIZE]
        resp = _request("POST", "/embed", {"texts": part, "query": query})
        batch_meta = {k: resp[k] for k in ("provider", "embedding_model", "dimension")}
        if meta and batch_meta != meta:
            raise AIGatewayError("The AI provider changed while embedding; run the job again.")
        meta = batch_meta
        vectors.extend(resp["embeddings"])
    return {"embeddings": vectors, **meta}


def embed_texts(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    return embed_texts_with_meta(texts, query=task_type == "RETRIEVAL_QUERY")["embeddings"]


def embed_query(text: str) -> list[float]:
    return embed_texts_with_meta([text], query=True)["embeddings"][0]


def generate(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> str:
    body: dict[str, Any] = {"prompt": prompt}
    if system:
        body["system"] = system
    if model:
        body["model"] = model
    if temperature is not None:
        body["temperature"] = temperature
    return _request("POST", "/generate", body)["text"]


def vector_search_sql(dimension: int, has_model_column: bool) -> str:
    """Nearest-neighbour SQL over ``embeddings/**``: only vectors of ``dimension`` (and, when the
    files record it, of the active ``model``) are compared.

    Parameters, in order: query vector, parquet path, model name (only if ``has_model_column``).
    Older files have no ``model`` column and count as ``LEGACY_MODEL``.
    """
    dim = int(dimension)
    model_filter = f"AND coalesce(model, '{LEGACY_MODEL}') = ?" if has_model_column else ""
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


LEGACY_MODEL = "gemini-embedding-001"
