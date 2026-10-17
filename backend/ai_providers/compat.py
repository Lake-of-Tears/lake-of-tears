from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ai_providers.base import AIProvider


class _Models:
    def __init__(self, provider: AIProvider):
        self._provider = provider

    def generate_content(self, model: str | None = None, contents: str = "", config: Any = None):
        cfg = config or {}
        text = self._provider.generate(
            contents,
            system=cfg.get("system_instruction"),
            model=model,
            temperature=cfg.get("temperature"),
        )
        return SimpleNamespace(text=text)

    def embed_content(self, model: str | None = None, contents: Any = None, config: Any = None):
        """The provider decides the embedding model and dimension; ``model`` is ignored."""
        texts = [contents] if isinstance(contents, str) else list(contents or [])
        query = (config or {}).get("task_type") == "RETRIEVAL_QUERY"
        vectors = self._provider.embed_texts(texts, query=query)
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=v) for v in vectors],
            model=self._provider.embedding_model,
            dimension=self._provider.dimension,
            provider=self._provider.name,
        )


class ProviderClient:
    """Gives an ``AIProvider`` the small ``client.models.*`` shape the AI service was written for."""

    def __init__(self, provider: AIProvider):
        self.provider = provider
        self.models = _Models(provider)
