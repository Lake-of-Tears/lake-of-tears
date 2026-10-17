import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

import hashlib
from datetime import date, timedelta

import pandas as pd
from ai_gateway import embed_texts_with_meta
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

SOURCES = {
    "jellyfin": "raw/jellyfin/**/*.parquet",
    "truenas": "raw/truenas/**/*.parquet",
    "weather": "raw/weather/**/*.parquet",
    "truenas_logs": "raw/truenas_logs/**/*.parquet",
}


def embed_source(source: str, parquet_glob: str):
    con = get_duckdb_con()
    con.execute("INSTALL vss; LOAD vss;")
    yesterday = str(date.today() - timedelta(days=1))

    try:
        df = con.execute(f"""
            SELECT * FROM read_parquet('s3://datalake/{parquet_glob}')
            WHERE source_date = '{yesterday}'
        """).df()
    except Exception as e:
        print(f"[{source}] No data for {yesterday}: {e}")
        return

    if df.empty:
        print(f"[{source}] Empty for {yesterday}, skipping")
        return

    df["content"] = df.apply(TEXT_FN[source], axis=1)
    df["source"] = source
    df["source_date"] = yesterday

    id_col = next((c for c in ("id", "Id", "ID") if c in df.columns), None)
    if id_col:
        df["id"] = df[id_col].astype(str)
    else:
        df["id"] = df["content"].apply(
            lambda t: hashlib.md5(t.encode(), usedforsecurity=False).hexdigest()
        )

    result = embed_texts_with_meta(df["content"].tolist())
    df["embedding"] = result["embeddings"]
    # Record which model produced the vectors so searches only compare like with like.
    df["provider"] = result["provider"]
    df["model"] = result["embedding_model"]
    df["dimension"] = result["dimension"]

    out_df = df[
        [
            "id",
            "source",
            "content",
            "embedding",
            "source_date",
            "provider",
            "model",
            "dimension",
        ]
    ].copy()
    out_df["ingested_at"] = pd.Timestamp.now()

    con.register("out_df", out_df)
    y, m, d = yesterday[:4], yesterday[5:7], yesterday[8:10]
    con.execute(f"""
        COPY out_df TO 's3://datalake/embeddings/{source}/year={y}/month={m}/day={d}/data.parquet'
        (FORMAT PARQUET)
    """)
    print(f"[{source}] Embedded {len(out_df)} records for {yesterday}")


if __name__ == "__main__":
    for source, glob in SOURCES.items():
        embed_source(source, glob)
