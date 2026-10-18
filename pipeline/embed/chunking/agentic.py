"""Proposition-based chunking: an LLM rewrites a document as standalone statements."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

from chunking.base import BaseChunker
from chunking.recursive import RecursiveChunker

logger = logging.getLogger("embed.chunking.agentic")

DEFAULT_WINDOW = 4000

SYSTEM_PROMPT = (
    "You split documents into propositions for a search index. A proposition is one short, "
    "self-contained statement that makes sense on its own: replace pronouns and references with "
    "the names they stand for, keep numbers, dates and names exactly, do not add facts that are "
    "not in the text, and do not drop facts that are. Answer with a JSON array of strings and "
    "nothing else."
)


def parse_propositions(reply: str) -> list[str] | None:
    """The JSON array of strings in an LLM reply (tolerating code fences and chatter), or None."""
    text = reply.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        return None
    return [item.strip() for item in data if item.strip()]


class AgenticChunker(BaseChunker):
    """Slow and costly (one LLM call per ``window`` characters) but keeps each fact whole and
    resolvable on its own: good for contracts, papers and transcripts, wasteful for anything else.

    ``generate`` is ``ai_gateway.generate`` by default, so it uses whichever AI provider the Admin
    Panel selects. A window the model answers badly is split by the recursive strategy instead, so
    one bad reply never fails a pipeline run.
    """

    name = "agentic"

    def __init__(
        self,
        window: int = DEFAULT_WINDOW,
        generate: Callable[..., str] | None = None,
        fallback: BaseChunker | None = None,
    ):
        if window <= 0:
            raise ValueError("window must be greater than 0")
        self.window = window
        self._generate = generate
        self._windows = RecursiveChunker(chunk_size=window, chunk_overlap=0)
        self._fallback = fallback or RecursiveChunker()

    def _llm(self) -> Callable[..., str]:
        if self._generate is None:
            from ai_gateway import generate  # shared pipeline client, found at run time

            self._generate = generate
        return self._generate

    def split(self, text: str) -> list[str]:
        propositions: list[str] = []
        for window in self._windows.split(text):
            parsed = None
            try:
                reply = self._llm()(
                    "Split this text into propositions:\n\n" + window,
                    system=SYSTEM_PROMPT,
                    temperature=0.0,
                )
                parsed = parse_propositions(reply)
            except Exception as exc:
                logger.warning("Agentic chunking could not use the model: %s", exc)
            if parsed:
                propositions.extend(parsed)
            else:
                logger.warning("Agentic chunking fell back to recursive splitting for one window")
                propositions.extend(self._fallback.split(window))
        return propositions
