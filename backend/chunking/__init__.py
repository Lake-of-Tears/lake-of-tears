"""Pluggable chunking strategies for the embedding pipeline.

    from chunking import get_chunker
    chunker = get_chunker("recursive", chunk_size=512, chunk_overlap=64)
    chunker.chunk(text)            # -> list of chunk strings

Strategies: ``recursive`` (default for unstructured text), ``document`` (one row is one chunk, for
structured events) and ``agentic`` (an LLM writes standalone propositions).
"""

from __future__ import annotations

from typing import Any

from chunking.agentic import AgenticChunker
from chunking.base import BaseChunker
from chunking.document import DocumentChunker
from chunking.frame import LINEAGE_COLUMNS, explode_chunks, row_metadata
from chunking.recursive import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    RecursiveChunker,
)

DEFAULT_STRATEGY = "recursive"

STRATEGIES: dict[str, type[BaseChunker]] = {
    "recursive": RecursiveChunker,
    "document": DocumentChunker,
    "agentic": AgenticChunker,
}


def get_chunker(name: str | None = None, **options: Any) -> BaseChunker:
    """Build a strategy by name. Options a strategy does not take are an error, not ignored."""
    key = (name or DEFAULT_STRATEGY).strip().lower()
    cls = STRATEGIES.get(key)
    if cls is None:
        raise ValueError(f"Unknown chunking strategy '{name}'. Available: {', '.join(STRATEGIES)}")
    try:
        return cls(**options)
    except TypeError as exc:
        raise ValueError(f"Invalid options for the '{key}' chunking strategy: {exc}") from exc


__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_STRATEGY",
    "LINEAGE_COLUMNS",
    "STRATEGIES",
    "AgenticChunker",
    "BaseChunker",
    "DocumentChunker",
    "RecursiveChunker",
    "explode_chunks",
    "get_chunker",
    "row_metadata",
]
