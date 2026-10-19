"""Record-level chunking: one row is one chunk."""

from __future__ import annotations

from chunking.base import BaseChunker


class DocumentChunker(BaseChunker):
    """Passthrough. Right for structured events (weather, metrics, log lines) where each row is
    already a self-contained unit and cutting it would only lose meaning."""

    name = "document"

    def split(self, text: str) -> list[str]:
        return [text] if text.strip() else []
