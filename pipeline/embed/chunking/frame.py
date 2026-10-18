"""Explode a DataFrame of documents into one row per chunk, with lineage."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

import pandas as pd
from chunking.base import BaseChunker

LINEAGE_COLUMNS = ("parent_id", "chunk_index", "total_chunks")
METADATA_VALUE_LIMIT = 200


def _json_value(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, bool | int | float) or value is None:
        return value
    text = str(value)
    return text if len(text) <= METADATA_VALUE_LIMIT else text[:METADATA_VALUE_LIMIT] + "…"


def row_metadata(row: pd.Series, exclude: Sequence[str]) -> str:
    """The parent row's other fields as compact JSON (long values shortened, missing ones left out)."""
    data = {
        str(key): _json_value(value)
        for key, value in row.items()
        if key not in exclude and _json_value(value) is not None
    }
    return json.dumps(data, ensure_ascii=False, default=str, sort_keys=True)


def explode_chunks(
    df: pd.DataFrame,
    texts: Sequence[object],
    parent_ids: Sequence[str],
    chunker: BaseChunker,
    exclude_metadata: Sequence[str] = (),
) -> pd.DataFrame:
    """One output row per chunk, in document order.

    Columns: ``id``, ``parent_id``, ``chunk_index``, ``total_chunks``, ``content``, ``metadata``.
    A document that is one chunk keeps its own id (so record-level sources look as they always did);
    a longer one gets ``<parent_id>:<chunk_index>``. Documents with no text produce no rows.
    """
    rows: list[dict[str, Any]] = []
    for (_, source_row), text, parent_id in zip(df.iterrows(), texts, parent_ids, strict=True):
        chunks = chunker.chunk(text)
        if not chunks:
            continue
        metadata = row_metadata(source_row, exclude_metadata)
        for index, content in enumerate(chunks):
            rows.append(
                {
                    "id": parent_id if len(chunks) == 1 else f"{parent_id}:{index}",
                    "parent_id": parent_id,
                    "chunk_index": index,
                    "total_chunks": len(chunks),
                    "content": content,
                    "metadata": metadata,
                }
            )
    columns = ["id", "parent_id", "chunk_index", "total_chunks", "content", "metadata"]
    return pd.DataFrame(rows, columns=columns)
