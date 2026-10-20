import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

import hashlib
from datetime import date, timedelta
from typing import Any

import pandas as pd
from ai_gateway import embed_texts_with_meta
from chunking import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_STRATEGY,
    BaseChunker,
    explode_chunks,
    get_chunker,
)
from storage_writer import get_duckdb_con


def _text_jellyfin(row) -> str:
    parts = [
        str(row[c])
        for c in ("Name", "Type", "ShortOverview")
        if pd.notna(row.get(c)) and str(row.get(c, "")).strip()
    ]
    return " | ".join(parts) or "Jellyfin activity"


def _text_truenas(row) -> str:
    name = row.get("name", "unknown")
    status = row.get("status", "unknown")
    allocated_gb = (row.get("allocated") or 0) / (1024**3)
    free_gb = (row.get("free") or 0) / (1024**3)
    return f"Pool {name}: {status}, {allocated_gb:.1f}GB used, {free_gb:.1f}GB free"


def _text_weather(row) -> str:
    return (
        f"Weather {row.get('timestamp', '')}: {row.get('temperature_2m', '')}F apparent "
        f"{row.get('apparent_temperature', '')}F, {row.get('relative_humidity_2m', '')}% humidity, "
        f"{row.get('precipitation', 0)}in precip, {row.get('wind_speed_10m', '')}mph wind"
    )


def _text_alexa(row) -> str:
    for col in ("utteranceText", "utterance", "query", "text", "description"):
        val = row.get(col)
        if pd.notna(val) and str(val).strip():
            return str(val)
    vals = [str(v) for v in row.values() if isinstance(v, str) and str(v).strip()]
    return " | ".join(vals[:5]) or "Alexa activity"


def _text_truenas_logs(row) -> str:
    # content is pre-formatted by ingest_truenas_logs.py: "[unit] PRIORITY: message"
    return str(row.get("content", "")).strip() or "TrueNAS log entry"


TEXT_FN = {
    "jellyfin": _text_jellyfin,
    "truenas": _text_truenas,
    "weather": _text_weather,
    "alexa": _text_alexa,
    "truenas_logs": _text_truenas_logs,
}

# Sources embedded every day. Each entry is a dict:
#   glob          where the raw Parquet files are (required)
#   text_fn       builds the text of one row; without it the text comes from ``text_column`` or the
#                 first of TEXT_COLUMNS the data has
#   text_column   the column that holds the text (optional)
#   chunking      "recursive" | "document" | "agentic"; default "document" for a source with a
#                 text_fn (structured events) and "recursive" otherwise
#   chunk_size, chunk_overlap   for "recursive", in characters (default 512 and 64)
#   window        for "agentic", characters sent to the model per call (default 4000)
SOURCES: dict[str, dict[str, Any]] = {
    "jellyfin": {
        "glob": "raw/jellyfin/**/*.parquet",
        "text_fn": _text_jellyfin,
        "chunking": "document",
    },
    "truenas": {
        "glob": "raw/truenas/**/*.parquet",
        "text_fn": _text_truenas,
        "chunking": "document",
    },
    "weather": {
        "glob": "raw/weather/**/*.parquet",
        "text_fn": _text_weather,
        "chunking": "document",
    },
    "truenas_logs": {
        "glob": "raw/truenas_logs/**/*.parquet",
        "text_fn": _text_truenas_logs,
        "chunking": "document",
    },
}

# Columns of the embeddings Parquet files, in order (``ingested_at`` is added when writing). The SQL
# assistant's declared schema in backend/ai_service.py and ui/duckdb_ai.py must list the same ones.
OUTPUT_COLUMNS = [
    "id",
    "source",
    "content",
    "embedding",
    "source_date",
    "provider",
    "model",
    "dimension",
    "parent_id",
    "chunk_index",
    "total_chunks",
    "metadata",
]

TEXT_COLUMNS = ("content", "text", "body", "description", "overview", "summary", "title")


def source_config(source: str, parquet_glob: str | None = None) -> dict[str, Any]:
    """The settings for ``source`` with defaults applied (a plain glob string is still accepted)."""
    raw = SOURCES.get(source, {})
    config: dict[str, Any] = {"glob": raw} if isinstance(raw, str) else dict(raw)
    if parquet_glob:
        config["glob"] = parquet_glob
    config.setdefault("text_fn", TEXT_FN.get(source))
    config.setdefault("chunking", "document" if config["text_fn"] else DEFAULT_STRATEGY)
    return config


def build_chunker(config: dict[str, Any]) -> BaseChunker:
    strategy = str(config["chunking"]).lower()
    options: dict[str, Any] = {}
    if strategy == "recursive":
        options = {
            "chunk_size": config.get("chunk_size", DEFAULT_CHUNK_SIZE),
            "chunk_overlap": config.get("chunk_overlap", DEFAULT_CHUNK_OVERLAP),
        }
    elif strategy == "agentic" and "window" in config:
        options = {"window": config["window"]}
    return get_chunker(strategy, **options)


def source_text(df: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.Series, str | None]:
    """The text of each row, and the column it came from (None when built from several)."""
    text_fn = config.get("text_fn")
    if text_fn:
        return df.apply(text_fn, axis=1), None
    column = config.get("text_column") or next((c for c in TEXT_COLUMNS if c in df.columns), None)
    if column:
        return df[column], column
    text_columns = [
        c
        for c in df.columns
        if pd.api.types.is_string_dtype(df[c]) or pd.api.types.is_object_dtype(df[c])
    ]
    return df[text_columns].astype(str).agg(" | ".join, axis=1), None


def chunk_source_frame(df: pd.DataFrame, source: str, config: dict[str, Any]) -> pd.DataFrame:
    """Split each row's text with the source's strategy: one row per chunk, with lineage."""
    texts, text_column = source_text(df, config)
    id_col = next((c for c in ("id", "Id", "ID") if c in df.columns), None)
    if id_col:
        parent_ids = df[id_col].astype(str).tolist()
    else:
        parent_ids = [
            hashlib.md5(str(t).encode(), usedforsecurity=False).hexdigest() for t in texts
        ]
    # year/month/day are added by DuckDB from the year=/month=/day= folder names; source_date has it
    skip = [c for c in (id_col, text_column, "source_date", "year", "month", "day") if c]
    return explode_chunks(df, texts.tolist(), parent_ids, build_chunker(config), skip)


def embed_source(source: str, parquet_glob: str | None = None):
    config = source_config(source, parquet_glob)
    con = get_duckdb_con()
    con.execute("INSTALL vss; LOAD vss;")
    yesterday = str(date.today() - timedelta(days=1))

    try:
        df = con.execute(f"""
            SELECT * FROM read_parquet('s3://datalake/{config["glob"]}')
            WHERE source_date = '{yesterday}'
        """).df()
    except Exception as e:
        print(f"[{source}] No data for {yesterday}: {e}")
        return

    if df.empty:
        print(f"[{source}] Empty for {yesterday}, skipping")
        return

    chunks = chunk_source_frame(df, source, config)
    if chunks.empty:
        print(f"[{source}] No text to embed for {yesterday}, skipping")
        return
    chunks["source"] = source
    chunks["source_date"] = yesterday

    result = embed_texts_with_meta(chunks["content"].tolist())
    chunks["embedding"] = result["embeddings"]
    # Record which model produced the vectors so searches only compare like with like.
    chunks["provider"] = result["provider"]
    chunks["model"] = result["embedding_model"]
    chunks["dimension"] = result["dimension"]

    out_df = chunks[OUTPUT_COLUMNS].copy()
    out_df["ingested_at"] = pd.Timestamp.now()

    con.register("out_df", out_df)
    y, m, d = yesterday[:4], yesterday[5:7], yesterday[8:10]
    con.execute(f"""
        COPY out_df TO 's3://datalake/embeddings/{source}/year={y}/month={m}/day={d}/data.parquet'
        (FORMAT PARQUET)
    """)
    print(
        f"[{source}] Embedded {len(out_df)} chunks from "
        f"{out_df['parent_id'].nunique()} records for {yesterday} ({config['chunking']} chunking)"
    )


if __name__ == "__main__":
    for source in SOURCES:
        embed_source(source)
