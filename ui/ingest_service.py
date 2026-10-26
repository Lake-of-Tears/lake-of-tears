from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger("lakeui.ingest_service")

# Supported file formats
SUPPORTED_EXTENSIONS = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".json": "json",
    ".ndjson": "ndjson",
    ".jsonl": "ndjson",
    ".parquet": "parquet",
    ".pq": "parquet",
}

# Airflow DAG mapping for interactive connector cards
CONNECTOR_DAG_MAPPING = {
    "weather": "ingest_weather",
    "truenas": "ingest_truenas",
    "truenas_logs": "ingest_truenas_logs",
    "jellyfin": "ingest_jellyfin",
    "alexa": "ingest_alexa",
    "tmdb": "ingest_tmdb_movies",
}


def slugify(text: str) -> str:
    """Convert a name or filename into a valid SQL and path identifier."""
    # Strip extension if present
    base = Path(text).stem if "." in text else text
    s = re.sub(r"[^\w\s-]", "", base).strip().lower()
    s = re.sub(r"[-\s]+", "_", s)
    return s or "table"


def detect_file_format(filename: str) -> str:
    """Detect file format based on extension."""
    lower = filename.lower()
    for ext, fmt in SUPPORTED_EXTENSIONS.items():
        if lower.endswith(ext):
            return fmt
    raise ValueError(
        f"Unsupported file format for '{filename}'. Supported extensions: {', '.join(sorted(SUPPORTED_EXTENSIONS.keys()))}"
    )


def _safe_val(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, int | float | bool | str):
        return v
    if isinstance(v, datetime | date):
        return v.isoformat()
    return str(v)


def preview_uploaded_file(
    content: bytes,
    filename: str,
    max_preview_rows: int = 10,
) -> dict[str, Any]:
    """Parse uploaded file using DuckDB, extract inferred column schema and sample rows."""
    fmt = detect_file_format(filename)
    suffix = Path(filename).suffix or f".{fmt}"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        con = duckdb.connect()
        # Build DuckDB reader SQL
        escaped_tmp = tmp_path.replace("'", "''")
        if fmt == "csv":
            read_sql = f"read_csv_auto('{escaped_tmp}', sample_size=1000)"
        elif fmt == "tsv":
            read_sql = f"read_csv_auto('{escaped_tmp}', delim='\t', sample_size=1000)"
        elif fmt in ("json", "ndjson"):
            read_sql = f"read_json_auto('{escaped_tmp}', sample_size=1000)"
        elif fmt == "parquet":
            read_sql = f"read_parquet('{escaped_tmp}')"
        else:
            raise ValueError(f"Unsupported format: {fmt}")

        # Inferred columns & types
        desc_rows = con.execute(f"DESCRIBE SELECT * FROM {read_sql}").fetchall()
        columns = [{"name": r[0], "type": r[1]} for r in desc_rows]

        # Preview sample rows
        preview_data = con.execute(f"SELECT * FROM {read_sql} LIMIT {max_preview_rows}").fetchall()
        safe_rows = [[_safe_val(cell) for cell in row] for row in preview_data]

        suggested_name = slugify(filename)

        return {
            "ok": True,
            "filename": filename,
            "format": fmt,
            "suggested_table_name": suggested_name,
            "columns": columns,
            "rows": safe_rows,
            "preview_count": len(safe_rows),
        }
    except Exception as exc:
        logger.warning("Failed to preview file '%s': %s", filename, exc)
        return {
            "ok": False,
            "filename": filename,
            "error": f"Failed to parse file: {exc}",
        }
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def convert_to_parquet_file(
    content: bytes,
    filename: str,
) -> tuple[str, list[dict[str, str]], int]:
    """Convert input file content to a local temporary Parquet file and return (parquet_path, columns, row_count)."""
    fmt = detect_file_format(filename)
    suffix = Path(filename).suffix or f".{fmt}"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as in_tmp:
        in_tmp.write(content)
        in_tmp_path = in_tmp.name

    out_tmp_fd, out_tmp_path = tempfile.mkstemp(suffix=".parquet")
    os.close(out_tmp_fd)

    try:
        con = duckdb.connect()
        escaped_in = in_tmp_path.replace("'", "''")
        escaped_out = out_tmp_path.replace("'", "''")

        if fmt == "csv":
            read_sql = f"read_csv_auto('{escaped_in}', sample_size=2000)"
        elif fmt == "tsv":
            read_sql = f"read_csv_auto('{escaped_in}', delim='\t', sample_size=2000)"
        elif fmt in ("json", "ndjson"):
            read_sql = f"read_json_auto('{escaped_in}', sample_size=2000)"
        elif fmt == "parquet":
            read_sql = f"read_parquet('{escaped_in}')"
        else:
            raise ValueError(f"Unsupported format: {fmt}")

        # Inferred schema
        desc_rows = con.execute(f"DESCRIBE SELECT * FROM {read_sql}").fetchall()
        columns = [{"name": r[0], "type": r[1]} for r in desc_rows]

        # Convert and write Parquet
        con.execute(f"COPY (SELECT * FROM {read_sql}) TO '{escaped_out}' (FORMAT PARQUET)")
        row_count = con.execute(f"SELECT count(*) FROM read_parquet('{escaped_out}')").fetchone()[0]

        return out_tmp_path, columns, row_count
    finally:
        if os.path.exists(in_tmp_path):
            try:
                os.remove(in_tmp_path)
            except OSError:
                pass


def build_managed_hive_path(
    workspace_slug: str,
    catalog_slug: str,
    schema_slug: str,
    table_slug: str,
    target_date: date | None = None,
    mode: str = "create_new",
) -> tuple[str, str]:
    """Construct the managed Hive date-partitioned S3 key and wildcard pattern.

    Returns: (exact_object_key, s3_wildcard_pattern)
    """
    clean_ws = (workspace_slug or "default").strip("/")
    cat = slugify(catalog_slug)
    sch = slugify(schema_slug)
    tbl = slugify(table_slug)

    d = target_date or date.today()
    y, m, day = d.strftime("%Y"), d.strftime("%m"), d.strftime("%d")

    filename = "data.parquet"
    if mode == "append":
        suffix = uuid.uuid4().hex[:8]
        filename = f"data_{suffix}.parquet"

    key = f"workspaces/{clean_ws}/{cat}/{sch}/{tbl}/year={y}/month={m}/day={day}/{filename}"
    pattern = f"s3://datalake/workspaces/{clean_ws}/{cat}/{sch}/{tbl}/**/*.parquet"
    return key, pattern


def validate_and_jail_s3_path(
    raw_path: str,
    workspace_slug: str,
    bucket: str = "datalake",
) -> str:
    """Validate that an S3 path strictly resides within the active workspace prefix.

    Supports relative workspace paths (e.g., 'raw/weather/') or explicit ('workspaces/<slug>/raw/weather/').
    Expands directories/slashes to `**/*.parquet`.
    """
    clean_slug = (workspace_slug or "default").replace("\\", "/").strip("/ ")
    if not clean_slug or ".." in clean_slug:
        raise ValueError("Invalid workspace slug")

    raw = (raw_path or "").strip()
    if not raw:
        raise ValueError("Storage path cannot be empty")

    # Remove s3://bucket/ prefix if present
    prefix_s3 = f"s3://{bucket}/"
    if raw.startswith(prefix_s3):
        raw = raw[len(prefix_s3) :]
    elif raw.startswith("s3://"):
        raise ValueError(f"Path must reside in bucket '{bucket}'")

    raw = raw.lstrip("/")
    expected_ws_prefix = f"workspaces/{clean_slug}/"

    if raw.startswith("workspaces/"):
        if not raw.startswith(expected_ws_prefix):
            raise ValueError(
                f"Cross-workspace path access denied. Must reside in 'workspaces/{clean_slug}/'."
            )
        jailed = raw
    else:
        # Prepend workspace prefix
        jailed = f"workspaces/{clean_slug}/{raw}"

    # Directory traversal check
    parts = [p for p in jailed.split("/") if p]
    if ".." in parts:
        raise ValueError("Directory traversal ('..') is strictly prohibited")

    # Format trailing directory or extension
    if jailed.endswith("/"):
        jailed = f"{jailed}**/*.parquet"
    elif not jailed.endswith(".parquet") and not jailed.endswith("*.parquet"):
        jailed = f"{jailed}/**/*.parquet"

    return f"s3://{bucket}/{jailed}"


def inspect_s3_parquet_path(
    s3_uri: str,
    con: duckdb.DuckDBPyConnection | None = None,
) -> list[dict[str, str]]:
    """Run DuckDB schema inference against an S3 Parquet path."""
    close_con = False
    if con is None:
        con = duckdb.connect()
        close_con = True

    try:
        escaped_uri = s3_uri.replace("'", "''")
        desc_rows = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{escaped_uri}') LIMIT 0"
        ).fetchall()
        return [{"name": r[0], "type": r[1]} for r in desc_rows]
    finally:
        if close_con:
            con.close()
