from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any


class ProviderError(Exception):
    """A provider call failed. The message must never contain credentials."""


class ProviderNotConfigured(ProviderError):  # noqa: N818  (reads better than ...Error)
    """No usable provider configuration (for example a missing API key)."""


@dataclass(frozen=True)
class ProviderInfo:
    """Non-secret description of the active provider."""

    provider: str
    chat_model: str
    embedding_model: str
    dimension: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LLMProvider(ABC):
    """Text generation."""

    @property
    @abstractmethod
    def chat_model(self) -> str: ...

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        """Return the model's text answer. ``model`` overrides the configured chat model."""


class EmbeddingProvider(ABC):
    """Vector embeddings."""

    @property
    @abstractmethod
    def embedding_model(self) -> str: ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Length of every vector this provider returns."""

    @abstractmethod
    def embed_texts(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        """Embed ``texts``. ``query=True`` embeds a search question rather than stored documents."""


class AIProvider(LLMProvider, EmbeddingProvider):
    """A provider that can both generate text and embed it."""

    name: str

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            provider=self.name,
            chat_model=self.chat_model,
            embedding_model=self.embedding_model,
            dimension=self.dimension,
        )

    def test_connection(self) -> dict[str, Any]:
        """Make one small live call and report the outcome without raising."""
        try:
            reply = self.generate("Reply with the single word OK.").strip()
            return {
                "ok": True,
                "provider": self.name,
                "model": self.chat_model,
                "message": f"Connected to {self.chat_model} ({reply or 'OK'}).",
            }
        except Exception as exc:
            return {
                "ok": False,
                "provider": self.name,
                "model": self.chat_model,
                "message": f"{self.name} connection failed: {exc}",
            }
