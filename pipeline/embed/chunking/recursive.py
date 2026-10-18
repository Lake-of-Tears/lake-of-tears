"""Recursive character splitting: keep paragraphs, then lines, then sentences, then words whole."""

from __future__ import annotations

from collections.abc import Sequence

from chunking.base import BaseChunker

DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 64
DEFAULT_SEPARATORS = ("\n\n", "\n", ". ", " ", "")


class RecursiveChunker(BaseChunker):
    """Splits on the coarsest separator that occurs in the text and falls back to finer ones for
    pieces that are still too long, so cuts land on paragraph or sentence boundaries where possible.

    Sizes are **characters** (no tokenizer is needed; roughly 4 characters per token in English).
    Every chunk is at most ``chunk_size`` characters, and consecutive chunks share up to
    ``chunk_overlap`` characters of whole pieces so a sentence cut at a boundary is still found.
    """

    name = "recursive"

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        separators: Sequence[str] = DEFAULT_SEPARATORS,
    ):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than 0")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be at least 0 and less than chunk_size")
        if not separators or separators[-1] != "":
            separators = (*separators, "")  # the empty separator (single characters) always ends it
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = tuple(separators)

    def split(self, text: str) -> list[str]:
        if not text.strip():
            return []
        return self._split(text, self.separators)

    # -- internals ---------------------------------------------------------------------
    def _split(self, text: str, separators: tuple[str, ...]) -> list[str]:
        separator, finer = separators[-1], ()
        for index, candidate in enumerate(separators):
            if candidate == "" or candidate in text:
                separator, finer = candidate, separators[index + 1 :]
                break

        chunks: list[str] = []
        small: list[str] = []
        for piece in self._pieces(text, separator):
            if len(piece) <= self.chunk_size:
                small.append(piece)
                continue
            if small:
                chunks.extend(self._merge(small))
                small = []
            chunks.extend(self._split(piece, finer))  # only reached when ``finer`` is non-empty
        if small:
            chunks.extend(self._merge(small))
        return chunks

    @staticmethod
    def _pieces(text: str, separator: str) -> list[str]:
        """Cut after each separator, keeping it on the piece it ends so no text is lost."""
        if separator == "":
            return list(text)
        parts = text.split(separator)
        return [part + separator for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])

    def _merge(self, pieces: list[str]) -> list[str]:
        """Join consecutive pieces up to ``chunk_size``, carrying a tail over as overlap."""
        chunks: list[str] = []
        window: list[str] = []
        total = 0
        for piece in pieces:
            if total + len(piece) > self.chunk_size and window:
                chunk = "".join(window).strip()
                if chunk:
                    chunks.append(chunk)
                while window and (
                    total > self.chunk_overlap or total + len(piece) > self.chunk_size
                ):
                    total -= len(window.pop(0))
            window.append(piece)
            total += len(piece)
        tail = "".join(window).strip()
        if tail and (not chunks or tail != chunks[-1]):
            chunks.append(tail)
        return chunks
