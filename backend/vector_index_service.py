from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from ai_service import create_duckdb_connection
from auth import get_stored_gemini_api_key
from google import genai
from models import CatalogTable
from sqlalchemy.orm import Session

logger = logging.getLogger("lakehouse.vector_index")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", os.getenv("MINIO_ROOT_USER", "admin"))
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", os.getenv("MINIO_ROOT_PASSWORD", "password123"))
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "datalake")


def _safe_identifier(name: str | None) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]", "_", (name or "").strip())
    if not slug or slug[0].isdigit():
        slug = f"t_{slug}"
    return slug


def sync_table_vector_index(
    table: CatalogTable,
    db: Session,
    api_key: str | None = None,
    dest_s3: str | None = None,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Incrementally synchronize vector embeddings for a catalog table.

    Reads raw data from `table.s3_path_pattern`, composes text chunks using
    the configured source column or template, generates 768-dim embeddings via Gemini,
    and writes Parquet files to `s3://datalake/embeddings/<table_slug>/...`.
    """
    s3_path = (table.s3_path_pattern or "").strip()
    if not s3_path:
        return {"ok": False, "error": "Table has no S3 storage path configured"}

    vector_cfg = table.vector_index or {}
    source_col = (vector_cfg.get("source_column") or "overview").strip()
    format_template = (vector_cfg.get("format_template") or "").strip()
    model = (vector_cfg.get("model") or "gemini-embedding-001").strip()
    dim = int(vector_cfg.get("dimension") or 768)

    active_key = api_key or get_stored_gemini_api_key(db)
    if not active_key:
        return {"ok": False, "error": "Gemini API key is not configured"}

    con = create_duckdb_connection(load_vss=True, register_ai=False)
    client = genai.Client(api_key=active_key)

    try:
        escaped_path = s3_path.replace("'", "''")
        # Check column existence in source
        desc_rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{escaped_path}')").fetchall()
        col_names = [d[0] for d in desc_rows]
        if source_col not in col_names:
            # Fall back to first varchar column if configured column not found
            varchar_cols = [d[0] for d in desc_rows if "VARCHAR" in d[1].upper()]
            if varchar_cols:
                source_col = varchar_cols[0]
            else:
                return {
                    "ok": False,
                    "error": f"Source column '{source_col}' not found in raw table schema",
                }

        # Query non-empty records
        query = f"""
            SELECT * FROM read_parquet('{escaped_path}')
            WHERE "{source_col}" IS NOT NULL AND trim("{source_col}") != ''
        """
        raw_rows = con.execute(query).fetchall()
        if not raw_rows:
            return {
                "ok": True,
                "total_vectors": 0,
                "status": "synced",
                "message": "No records found to embed",
            }

        table_slug = _safe_identifier(table.slug)
        now_dt = datetime.now(UTC)
        y, m, d = now_dt.strftime("%Y"), now_dt.strftime("%m"), now_dt.strftime("%d")
        if not dest_s3:
            dest_s3 = f"s3://{MINIO_BUCKET}/embeddings/{table_slug}/year={y}/month={m}/day={d}/data.parquet"

        # Create temporary table in DuckDB to hold batch embeddings
        con.execute("""
            CREATE TEMP TABLE _vector_sync_batch (
                id VARCHAR,
                source VARCHAR,
                summary VARCHAR,
                content VARCHAR,
                embedding DOUBLE[768],
                source_date VARCHAR,
                ingested_at TIMESTAMP
            )
        """)

        total = len(raw_rows)
        id_idx = col_names.index("id") if "id" in col_names else None
        source_idx = col_names.index(source_col)

        for i in range(0, total, batch_size):
            batch = raw_rows[i : i + batch_size]
            texts = []
            for r in batch:
                row_dict = dict(zip(col_names, r))
                if format_template:
                    try:
                        text = format_template.format(
                            **{k: v if v is not None else "" for k, v in row_dict.items()}
                        )
                    except Exception:
                        text = str(r[source_idx] or "")
                else:
                    text = str(r[source_idx] or "")
                texts.append(text)

            embed_resp = client.models.embed_content(
                model=model,
                contents=texts,
                config={"output_dimensionality": dim, "task_type": "RETRIEVAL_DOCUMENT"},
            )
            embeddings = [e.values for e in embed_resp.embeddings]

            insert_rows = []
            for idx, r in enumerate(batch):
                rec_id = str(r[id_idx]) if id_idx is not None else f"{table_slug}_{i + idx}"
                chunk_text = texts[idx]
                emb_vec = embeddings[idx]
                insert_rows.append(
                    (rec_id, table_slug, chunk_text, chunk_text, emb_vec, f"{y}-{m}-{d}")
                )

            con.executemany(
                """
                INSERT INTO _vector_sync_batch (id, source, summary, content, embedding, source_date, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, now())
                """,
                insert_rows,
            )

        # Write to destination S3 Parquet
        con.execute(f"COPY _vector_sync_batch TO '{dest_s3}' (FORMAT PARQUET)")

        # Update metadata in database
        updated_cfg = dict(vector_cfg)
        updated_cfg["enabled"] = True
        updated_cfg["source_column"] = source_col
        updated_cfg["format_template"] = format_template
        updated_cfg["model"] = model
        updated_cfg["dimension"] = dim
        updated_cfg["status"] = "synced"
        updated_cfg["total_vectors"] = total
        updated_cfg["last_synced_at"] = now_dt.isoformat()
        updated_cfg["s3_vector_path"] = dest_s3

        table.vector_index = updated_cfg
        db.commit()
        db.refresh(table)

        return {
            "ok": True,
            "total_vectors": total,
            "status": "synced",
            "s3_vector_path": dest_s3,
            "last_synced_at": now_dt.isoformat(),
        }
    except Exception as exc:
        logger.exception("Vector sync failed for table %s: %s", table.slug, exc)
        return {"ok": False, "error": str(exc)}
    finally:
        con.close()
