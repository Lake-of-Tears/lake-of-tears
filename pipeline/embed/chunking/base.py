"""The interface every chunking strategy implements."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseChunker(ABC):
    """Turns one document's text into the pieces that are embedded separately.

    A strategy only decides where to cut. Lineage (parent id, chunk index, total) and the
    exploding of a DataFrame into one row per chunk are handled by ``chunking.frame``.
    """

    name: str

    @abstractmethod
    def split(self, text: str) -> list[str]:
        """Return the chunks of ``text``, in order. Blank input gives an empty list."""

    def chunk(self, text: object) -> list[str]:
        """``split`` for any value: ``None`` and NaN are empty, blank chunks are dropped."""
        if text is None or (isinstance(text, float) and text != text):
            return []
        return [piece for piece in self.split(str(text)) if piece.strip()]
