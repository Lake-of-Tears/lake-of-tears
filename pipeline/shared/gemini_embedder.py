"""Deprecated: use ``ai_gateway``.

Kept so older scripts that do ``from gemini_embedder import embed_texts`` keep working. Embeddings
now come from the active provider through the backend AI gateway, not from Gemini directly.
"""

from ai_gateway import embed_query, embed_texts  # noqa: F401
