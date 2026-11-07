"""Google Cloud Vertex AI provider for Lake of Tears.

Enterprise generative AI via Vertex AI using the unified `google-genai` SDK.
Supports dual-mode authentication:
1. Pasted Google Cloud Service Account Key JSON (stored encrypted in the database).
2. Ambient Google Cloud Application Default Credentials (ADC) / GKE Workload Identity.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from typing import Any

from ai_providers.base import AIProvider, ProviderError, ProviderNotConfigured

logger = logging.getLogger(__name__)

DEFAULT_CHAT_MODEL = "gemini-2.5-flash"
DEFAULT_EMBEDDING_MODEL = "text-embedding-005"
DEFAULT_DIMENSION = 768
DEFAULT_LOCATION = "us-central1"

VERTEX_CHAT_FALLBACKS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
)


def _is_model_unavailable_error(exc: Exception) -> bool:
    """Check whether an exception indicates the requested model ID is retired or missing."""
    msg = str(exc).lower()
    return any(
        term in msg
        for term in (
            "404",
            "not found",
            "not_found",
            "deprecated",
            "discontinued",
            "is not supported",
            "unsupported model",
            "invalid model",
            "model_not_found",
        )
    )


def normalize_vertex_model_name(name: str) -> str:
    """Strip common publisher prefixes from model names for clean display."""
    clean = (name or "").strip()
    for prefix in ("publishers/google/models/", "models/"):
        if clean.startswith(prefix):
            return clean[len(prefix) :]
    return clean


class VertexAIProvider(AIProvider):
    """Google Cloud Vertex AI provider using ``google-genai`` with enterprise authentication."""

    name = "vertex"
    label = "Google Cloud Vertex AI"

    def __init__(
        self,
        project_id: str = "",
        location: str = DEFAULT_LOCATION,
        service_account_json: str = "",
        chat_model: str = DEFAULT_CHAT_MODEL,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        dimension: int = DEFAULT_DIMENSION,
        client: Any = None,
        client_factory: Callable[..., Any] | None = None,
        on_fallback: Callable[[str, str, str], None] | None = None,
    ):
        self._service_account_json = (service_account_json or "").strip()
        self._location = (
            location
            or os.getenv("GCP_LOCATION")
            or os.getenv("GOOGLE_CLOUD_LOCATION")
            or DEFAULT_LOCATION
        ).strip()
        self._chat_model = (chat_model or DEFAULT_CHAT_MODEL).strip()
        self._embedding_model = (embedding_model or DEFAULT_EMBEDDING_MODEL).strip()
        self._dimension = int(dimension or DEFAULT_DIMENSION)
        self._client = client
        self._client_factory = client_factory
        self._on_fallback = on_fallback

        # Resolve credentials & project ID
        self._credentials = None
        self._project_id = (
            project_id or os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT") or ""
        ).strip()

        if self._service_account_json:
            try:
                info = json.loads(self._service_account_json)
                if not isinstance(info, dict):
                    raise ValueError("Service account JSON must be an object")
                if not self._project_id and info.get("project_id"):
                    self._project_id = str(info["project_id"]).strip()
                from google.oauth2 import service_account

                self._credentials = service_account.Credentials.from_service_account_info(
                    info,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
            except Exception as exc:
                raise ProviderNotConfigured(
                    f"Invalid Google Cloud Service Account JSON: {exc}"
                ) from exc
        elif not self._project_id and self._client is None and self._client_factory is None:
            # Try to resolve project from ambient environment
            try:
                import google.auth

                _, env_project = google.auth.default()
                if env_project:
                    self._project_id = env_project.strip()
            except Exception:
                pass

        if not self._project_id and self._client is None and self._client_factory is None:
            raise ProviderNotConfigured(
                "Google Cloud Vertex AI: project_id is required. Enter a Project ID or provide a Service Account JSON with project_id."
            )

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def location(self) -> str:
        return self._location

    @property
    def chat_model(self) -> str:
        return self._chat_model

    @property
    def embedding_model(self) -> str:
        return self._embedding_model

    @property
    def dimension(self) -> int:
        return self._dimension

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory(
                    project=self._project_id,
                    location=self._location,
                    credentials=self._credentials,
                )
            else:
                from google import genai

                self._client = genai.Client(
                    vertexai=True,
                    project=self._project_id,
                    location=self._location,
                    credentials=self._credentials,
                )
        return self._client

    def _redact(self, exc: Exception | str) -> str:
        text = str(exc)
        if self._service_account_json:
            text = text.replace(self._service_account_json, "***")
        # Mask any RSA or PKCS8 private keys if found in message
        return re.sub(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]+?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
            "***",
            text,
        )

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        config: dict[str, Any] = {}
        if system:
            config["system_instruction"] = system
        if temperature is not None:
            config["temperature"] = temperature

        primary = model or self._chat_model
        candidates = [primary]
        for fb in VERTEX_CHAT_FALLBACKS:
            if fb not in candidates:
                candidates.append(fb)

        last_exc: Exception | None = None
        for cand in candidates:
            kwargs: dict[str, Any] = {"model": cand, "contents": prompt}
            if config:
                kwargs["config"] = config
            try:
                resp = self._get_client().models.generate_content(**kwargs)
                if cand != primary:
                    logger.warning(
                        "Vertex AI model '%s' unavailable; successfully fell back to '%s'",
                        primary,
                        cand,
                    )
                    if self._on_fallback is not None:
                        try:
                            self._on_fallback(
                                primary, cand, self._redact(last_exc or "Model unavailable")
                            )
                        except Exception:
                            pass
                return (getattr(resp, "text", None) or "").strip()
            except Exception as exc:
                last_exc = exc
                if _is_model_unavailable_error(exc):
                    logger.warning(
                        "Vertex AI model '%s' failed (%s); checking fallback candidates",
                        cand,
                        self._redact(exc),
                    )
                    continue
                # If error is not a 404/deprecation (e.g. auth/permission failure), raise immediately
                raise ProviderError(self._redact(exc)) from exc

        if last_exc is not None:
            raise ProviderError(self._redact(last_exc)) from last_exc
        return ""

    def embed_texts(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        config = {
            "task_type": "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT",
            "output_dimensionality": self._dimension,
        }
        try:
            result = self._get_client().models.embed_content(
                model=self._embedding_model, contents=texts, config=config
            )
        except Exception as exc:
            raise ProviderError(self._redact(exc)) from exc

        vectors = [list(e.values) for e in result.embeddings]
        for vec in vectors:
            if len(vec) != self._dimension:
                raise ProviderError(
                    f"{self._embedding_model} returned {len(vec)} dimensions, "
                    f"expected {self._dimension}"
                )
        return vectors

    def discover_models(self) -> dict[str, list[str]]:
        """List available base models from the Vertex AI Model Garden."""
        client = self._get_client()
        chat_models: list[str] = []
        embedding_models: list[str] = []
        try:
            models_iter = client.models.list(config={"query_base": True})
            for m in models_iter:
                name = getattr(m, "name", "") or ""
                clean_name = normalize_vertex_model_name(name)
                if not clean_name:
                    continue

                methods = getattr(m, "supported_actions", None)
                if not isinstance(methods, list | tuple | set):
                    methods = getattr(m, "supported_generation_methods", None)
                if not isinstance(methods, list | tuple | set):
                    methods = []
                is_generate = "generateContent" in methods
                is_embed = "embedContent" in methods
                if not methods:
                    if "embed" in clean_name.lower():
                        is_embed = True
                    elif "gemini" in clean_name.lower():
                        is_generate = True

                if is_generate and clean_name not in chat_models:
                    chat_models.append(clean_name)
                if is_embed and clean_name not in embedding_models:
                    embedding_models.append(clean_name)
        except Exception as exc:
            raise ProviderError(f"Failed to list Vertex AI models: {self._redact(exc)}") from exc

        return {
            "chat": sorted(
                chat_models,
                key=lambda x: (not x.startswith("gemini-2"), not x.startswith("gemini"), x),
            ),
            "embedding": sorted(
                embedding_models,
                key=lambda x: (not x.startswith("text-embedding"), not x.startswith("gemini"), x),
            ),
        }
