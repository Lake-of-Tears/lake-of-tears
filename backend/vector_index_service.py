from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from ai_providers import AIProvider, ProviderError, get_active_provider
from ai_service import create_duckdb_connection
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


def _record_sync_error(table: CatalogTable, db: Session, error_msg: str) -> None:
    try:
        updated_cfg = dict(table.vector_index or {})
        updated_cfg["status"] = "error"
        updated_cfg["last_error"] = error_msg
        table.vector_index = updated_cfg
        db.commit()
        db.refresh(table)
    except Exception:
        logger.exception(
            "Failed to update vector index error status for table %s",
            getattr(table, "slug", "unknown"),
        )


def sync_table_vector_index(
    table: CatalogTable,
    db: Session,
    dest_s3: str | None = None,
    batch_size: int = 50,
    provider: AIProvider | None = None,
) -> dict[str, Any]:
    """Incrementally synchronize vector embeddings for a catalog table.

    Reads raw data from `table.s3_path_pattern`, composes text chunks using
    the configured source column or template, embeds it with the AI provider selected in the
    Admin Panel (or ``provider``), and writes Parquet files to
    `s3://datalake/embeddings/<table_slug>/data.parquet` with the provider, model and dimension that made
    each row. The model and dimension come from the provider, not from the table settings.
    """
    s3_path = (table.s3_path_pattern or "").strip()
    if not s3_path:
        err = "Table has no S3 storage path configured"
        _record_sync_error(table, db, err)
        return {"ok": False, "error": err}

    vector_cfg = table.vector_index or {}
    source_col = (vector_cfg.get("source_column") or "overview").strip()
    format_template = (vector_cfg.get("format_template") or "").strip()

    try:
        provider = provider or get_active_provider(db)
        dim = provider.dimension
    except ProviderError as exc:
        err = str(exc)
        _record_sync_error(table, db, err)
        return {"ok": False, "error": err}
    model = provider.embedding_model

    con = create_duckdb_connection(load_vss=True, register_ai=False)

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
                err = f"Source column '{source_col}' not found in raw table schema"
                _record_sync_error(table, db, err)
                return {
                    "ok": False,
                    "error": err,
                }

        # Query non-empty records
        query = f"""
            SELECT * FROM read_parquet('{escaped_path}')
            WHERE "{source_col}" IS NOT NULL AND trim("{source_col}") != ''
        """
        raw_rows = con.execute(query).fetchall()
        now_dt = datetime.now(UTC)
        if not raw_rows:
            updated_cfg = dict(vector_cfg)
            updated_cfg["enabled"] = True
            updated_cfg["source_column"] = source_col
            updated_cfg["format_template"] = format_template
            updated_cfg["provider"] = provider.name
            updated_cfg["model"] = model
            updated_cfg["dimension"] = dim
            updated_cfg["status"] = "synced"
            updated_cfg["total_vectors"] = 0
            updated_cfg["last_synced_at"] = now_dt.isoformat()
            updated_cfg.pop("last_error", None)
            table.vector_index = updated_cfg
            db.commit()
            db.refresh(table)
            return {
                "ok": True,
                "total_vectors": 0,
                "status": "synced",
                "message": "No records found to embed",
            }

        table_slug = _safe_identifier(table.slug)
        y, m, d = now_dt.strftime("%Y"), now_dt.strftime("%m"), now_dt.strftime("%d")
        if not dest_s3:
            dest_s3 = f"s3://{MINIO_BUCKET}/embeddings/{table_slug}/data.parquet"

        # Create temporary table in DuckDB to hold batch embeddings
        con.execute("""
            CREATE TEMP TABLE _vector_sync_batch (
                id VARCHAR,
                source VARCHAR,
                summary VARCHAR,
                content VARCHAR,
                embedding DOUBLE[],
                source_date VARCHAR,
                provider VARCHAR,
                model VARCHAR,
                dimension BIGINT,
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

            embeddings = provider.embed_texts(texts)
            if len(embeddings) != len(texts):
                raise ProviderError(
                    f"{model} returned {len(embeddings)} vectors for {len(texts)} texts"
                )

            insert_rows = []
            for idx, r in enumerate(batch):
                rec_id = str(r[id_idx]) if id_idx is not None else f"{table_slug}_{i + idx}"
                chunk_text = texts[idx]
                emb_vec = embeddings[idx]
                insert_rows.append(
                    (
                        rec_id,
                        table_slug,
                        chunk_text,
                        chunk_text,
                        emb_vec,
                        f"{y}-{m}-{d}",
                        provider.name,
                        model,
                        len(emb_vec),
                    )
                )

            con.executemany(
                """
                INSERT INTO _vector_sync_batch
                    (id, source, summary, content, embedding, source_date, provider, model,
                     dimension, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now())
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
        updated_cfg["provider"] = provider.name
        updated_cfg["model"] = model
        updated_cfg["dimension"] = dim
        updated_cfg["status"] = "synced"
        updated_cfg["total_vectors"] = total
        updated_cfg["last_synced_at"] = now_dt.isoformat()
        updated_cfg["s3_vector_path"] = dest_s3
        updated_cfg.pop("last_error", None)

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
        _record_sync_error(table, db, str(exc))
        return {"ok": False, "error": str(exc)}
    finally:
        con.close()
