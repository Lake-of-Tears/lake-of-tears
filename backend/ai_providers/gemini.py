from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ai_providers.base import AIProvider, ProviderError, ProviderNotConfigured

logger = logging.getLogger(__name__)

DEFAULT_CHAT_MODEL = "gemini-2.5-flash"
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_DIMENSION = 768
GEMINI_CHAT_FALLBACKS: tuple[str, ...] = (
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


class GeminiProvider(AIProvider):
    """Google Gemini through the ``google-genai`` SDK with dynamic fallback and discovery."""

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        chat_model: str = DEFAULT_CHAT_MODEL,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        dimension: int = DEFAULT_DIMENSION,
        client_factory: Callable[[str], Any] | None = None,
        on_fallback: Callable[[str, str, str], None] | None = None,
    ):
        if not (api_key or "").strip():
            raise ProviderNotConfigured(
                "GEMINI_API_KEY is not set. A superadmin can configure it in Settings, Admin Panel."
            )
        self._api_key = api_key.strip()
        self._chat_model = chat_model or DEFAULT_CHAT_MODEL
        self._embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL
        self._dimension = int(dimension or DEFAULT_DIMENSION)
        self._client_factory = client_factory
        self._on_fallback = on_fallback
        self._client: Any = None

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
                self._client = self._client_factory(self._api_key)
            else:
                from google import genai

                self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _redact(self, exc: Exception | str) -> str:
        return str(exc).replace(self._api_key, "***")

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
        for fb in GEMINI_CHAT_FALLBACKS:
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
                        "Gemini model '%s' unavailable; successfully fell back to '%s'",
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
                        "Gemini model '%s' failed (%s); checking fallback candidates",
                        cand,
                        self._redact(exc),
                    )
                    continue
                # If error is not a model deprecation/404 (e.g. invalid auth), raise immediately
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
        """List available Gemini models from Google AI Studio."""
        client = self._get_client()
        chat_models: list[str] = []
        embedding_models: list[str] = []
        try:
            models_iter = client.models.list()
            for m in models_iter:
                name = getattr(m, "name", "") or ""
                if name.startswith("models/"):
                    clean_name = name[7:]
                else:
                    clean_name = name
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
            raise ProviderError(f"Failed to list Gemini models: {self._redact(exc)}") from exc

        return {
            "chat": sorted(
                chat_models,
                key=lambda x: (not x.startswith("gemini-2"), not x.startswith("gemini"), x),
            ),
            "embedding": sorted(embedding_models, key=lambda x: (not x.startswith("gemini"), x)),
        }
