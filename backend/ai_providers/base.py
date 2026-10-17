from __future__ import annotations

import time
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
        """Exercise text generation and embeddings with one small call each and report the outcome.

        Never raises: failures come back as ``ok: False`` with a message that holds no credentials.
        """
        result: dict[str, Any] = {
            "ok": False,
            "provider": self.name,
            "model": self.chat_model,
            "message": "",
            "chat": {"ok": False, "model": self.chat_model, "latency_ms": None, "message": ""},
            "embedding": {
                "ok": False,
                "model": self.embedding_model,
                "dimension": self.dimension,
                "latency_ms": None,
                "message": "",
            },
        }
        start = time.perf_counter()
        try:
            reply = self.generate("Reply with the single word OK.").strip()
            result["chat"].update(
                ok=True,
                latency_ms=round((time.perf_counter() - start) * 1000),
                message=f"Replied: {reply or 'OK'}",
            )
        except Exception as exc:
            result["chat"]["message"] = str(exc)

        start = time.perf_counter()
        try:
            vector = self.embed_texts(["Lake of Tears connection test"])[0]
            ok = len(vector) == self.dimension
            result["embedding"].update(
                ok=ok,
                dimension=len(vector),
                latency_ms=round((time.perf_counter() - start) * 1000),
                message=(
                    f"Returned {len(vector)} dimensions."
                    if ok
                    else f"Returned {len(vector)} dimensions but {self.dimension} are configured."
                ),
            )
        except Exception as exc:
            result["embedding"]["message"] = str(exc)

        chat, emb = result["chat"], result["embedding"]
        result["ok"] = bool(chat["ok"] and emb["ok"])
        if result["ok"]:
            result["message"] = (
                f"Connected to {self.name}: {chat['model']} answered in {chat['latency_ms']} ms; "
                f"{emb['model']} returned {emb['dimension']}-dimension vectors in "
                f"{emb['latency_ms']} ms."
            )
        else:
            problems = [
                f"{label}: {part['message']}"
                for label, part in (("chat", chat), ("embeddings", emb))
                if not part["ok"]
            ]
            result["message"] = f"{self.name} connection failed. " + "; ".join(problems)
        return result
