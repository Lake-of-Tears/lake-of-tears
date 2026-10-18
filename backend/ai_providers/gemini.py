from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ai_providers.base import AIProvider, ProviderError, ProviderNotConfigured

DEFAULT_CHAT_MODEL = "gemini-3.8-flash"
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_DIMENSION = 768


class GeminiProvider(AIProvider):
    """Google Gemini through the ``google-genai`` SDK (the behaviour Lake of Tears has today)."""

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        chat_model: str = DEFAULT_CHAT_MODEL,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        dimension: int = DEFAULT_DIMENSION,
        client_factory: Callable[[str], Any] | None = None,
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

    def _redact(self, exc: Exception) -> str:
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
        kwargs: dict[str, Any] = {"model": model or self._chat_model, "contents": prompt}
        if config:
            kwargs["config"] = config
        try:
            resp = self._get_client().models.generate_content(**kwargs)
        except Exception as exc:
            raise ProviderError(self._redact(exc)) from exc
        return (getattr(resp, "text", None) or "").strip()

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
