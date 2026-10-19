"""DuckDB connection pool, S3 configuration, and query error classification.

Provides:
- Robust S3/MinIO configuration parsing across local dev, Docker, and Kubernetes.
- Persistent warm connection pool with preloaded extensions and object cache.
- Structured classification of DuckDB query errors with actionable remediation advice.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import re
import threading
from collections.abc import Generator
from typing import Any
from urllib.parse import urlparse

import duckdb

logger = logging.getLogger("lakeui.duckdb_pool")


def parse_s3_endpoint_and_ssl(
    raw_endpoint: str | None = None,
    secure_flag: bool | str | None = None,
) -> tuple[str, bool]:
    """Parse raw endpoint string and determine (host:port, use_ssl).

    Supports formats like:
      - 'minio:9000'
      - 'http://minio:9000'
      - 'https://s3.example.com'
      - 's3.10.0.0.60.nip.io'
    """
    raw = (raw_endpoint or os.getenv("MINIO_ENDPOINT", "localhost:9000")).strip()

    # Determine default SSL from scheme
    derived_ssl = False
    clean_endpoint = raw

    if "://" in raw:
        parsed = urlparse(raw)
        derived_ssl = parsed.scheme.lower() == "https"
        clean_endpoint = parsed.netloc or parsed.path
    elif raw.startswith("//"):
        parsed = urlparse("http:" + raw)
        clean_endpoint = parsed.netloc

    # Explicit secure environment variable override
    if secure_flag is None:
        env_secure = (
            os.getenv("MINIO_SECURE") or os.getenv("MINIO_USE_SSL") or os.getenv("S3_USE_SSL")
        )
        if env_secure is not None:
            use_ssl = env_secure.strip().lower() in ("true", "1", "yes")
        else:
            use_ssl = derived_ssl
    elif isinstance(secure_flag, bool):
        use_ssl = secure_flag
    else:
        use_ssl = str(secure_flag).strip().lower() in ("true", "1", "yes")

    # Remove any trailing slashes or subpaths from the endpoint
    clean_endpoint = clean_endpoint.split("/")[0]

    return clean_endpoint, use_ssl


def get_s3_duckdb_config(
    endpoint: str | None = None,
    access_key: str | None = None,
    secret_key: str | None = None,
    use_ssl: bool | None = None,
    url_style: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """Build DuckDB configuration dict for S3 / MinIO."""
    s3_endpoint, s3_ssl = parse_s3_endpoint_and_ssl(
        raw_endpoint=endpoint or os.getenv("MINIO_ENDPOINT", "localhost:9000"),
        secure_flag=use_ssl,
    )
    s3_access_key = access_key or os.getenv("MINIO_ACCESS_KEY", "")
    s3_secret_key = secret_key or os.getenv("MINIO_SECRET_KEY", "")
    s3_style = url_style or os.getenv("S3_URL_STYLE", "path")
    s3_region = region or os.getenv("AWS_REGION") or os.getenv("MINIO_REGION", "us-east-1")

    return {
        "s3_endpoint": s3_endpoint,
        "s3_access_key_id": s3_access_key,
        "s3_secret_access_key": s3_secret_key,
        "s3_use_ssl": s3_ssl,
        "s3_url_style": s3_style,
        "s3_region": s3_region,
        "enable_object_cache": True,
    }


def load_duckdb_extensions(con: duckdb.DuckDBPyConnection, load_vss: bool = False) -> None:
    """Load httpfs and optional vss extensions with offline-first fallback."""
    # httpfs
    try:
        con.execute("LOAD httpfs;")
    except Exception as exc:
        logger.debug("Direct LOAD httpfs failed (%s); attempting INSTALL...", exc)
        try:
            con.execute("INSTALL httpfs; LOAD httpfs;")
        except Exception as inst_exc:
            logger.warning("Failed to install/load httpfs extension: %s", inst_exc)

    # vss
    if load_vss:
        try:
            con.execute("LOAD vss;")
        except Exception as exc:
            logger.debug("Direct LOAD vss failed (%s); attempting INSTALL...", exc)
            try:
                con.execute("INSTALL vss; LOAD vss;")
            except Exception as inst_exc:
                logger.warning("Failed to install/load vss extension: %s", inst_exc)


_MACRO_WORDS = re.compile(r"macro|function", re.IGNORECASE)


def _qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _qstr(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def snapshot_settings(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Current value of every DuckDB setting, used to undo changes a query made."""
    return {
        name: str(value)
        for name, value in con.execute("SELECT name, value FROM duckdb_settings()").fetchall()
    }


def snapshot_secrets(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {row[0] for row in con.execute("SELECT name FROM duckdb_secrets()").fetchall()}


def reset_connection_state(
    con: duckdb.DuckDBPyConnection,
    baseline_settings: dict[str, str] | None = None,
    baseline_secrets: set[str] | None = None,
    sweep_macros: bool = True,
) -> None:
    """Remove everything a request left on a pooled connection so the next user starts clean.

    SQL Editor users can create schemas, tables, views and macros, attach databases, create secrets
    and change settings, and every request registers its catalog views on the connection. Without
    this, those objects would be visible to whoever gets the connection next. Raises if anything
    cannot be undone, in which case the caller must discard the connection.

    Listing macros scans every built-in function (about 5 ms, ten times the rest of the reset), so
    callers that know a request's SQL contained no macro or function statement may skip it with
    ``sweep_macros=False``.
    """
    current = con.execute("SELECT current_database()").fetchone()[0]
    con.execute(
        f"USE {_qident(current)}.main"
    )  # so the schema being dropped is never the current one

    for (name,) in con.execute(
        "SELECT database_name FROM duckdb_databases() WHERE NOT internal AND database_name <> ?",
        [current],
    ).fetchall():
        con.execute(f"DETACH DATABASE IF EXISTS {_qident(name)}")

    for (name,) in con.execute(
        "SELECT schema_name FROM duckdb_schemas() "
        "WHERE database_name = ? AND NOT internal AND schema_name <> 'main'",
        [current],
    ).fetchall():
        con.execute(f"DROP SCHEMA IF EXISTS {_qident(name)} CASCADE")

    objects = (
        ("VIEW", "duckdb_views()", "view_name", "NOT internal"),
        ("TABLE", "duckdb_tables()", "table_name", "NOT internal"),
        ("SEQUENCE", "duckdb_sequences()", "sequence_name", "TRUE"),
        ("TYPE", "duckdb_types()", "type_name", "NOT internal"),
    )
    for kind, source, column, where in objects:
        for database, schema, name in con.execute(
            f"SELECT database_name, schema_name, {column} FROM {source} WHERE {where}"
        ).fetchall():
            con.execute(
                f"DROP {kind} IF EXISTS {_qident(database)}.{_qident(schema)}.{_qident(name)}"
            )
    if sweep_macros:
        for database, schema, name, kind in con.execute(
            "SELECT database_name, schema_name, function_name, function_type FROM duckdb_functions() "
            "WHERE NOT internal AND function_type IN ('macro', 'table_macro')"
        ).fetchall():
            what = "MACRO TABLE" if kind == "table_macro" else "MACRO"
            con.execute(
                f"DROP {what} IF EXISTS {_qident(database)}.{_qident(schema)}.{_qident(name)}"
            )

    if baseline_secrets is not None:
        for name in snapshot_secrets(con) - baseline_secrets:
            con.execute(f"DROP SECRET IF EXISTS {_qident(name)}")

    if baseline_settings is not None:
        now = snapshot_settings(con)
        for name, old in baseline_settings.items():
            if name in now and now[name] != old:
                con.execute(f"SET {name} = {_qstr(old)}")

    con.execute("SELECT 1").fetchall()  # the connection must still answer


class DuckDBConnectionPool:
    """Thread-safe pool of warm DuckDB connections with S3 and object caching enabled.

    Connections are reused across requests, so each is cleaned (``reset_connection_state``) when
    it goes back; one that cannot be cleaned or no longer answers is closed and replaced.
    """

    def __init__(
        self,
        max_size: int = 4,
        load_vss: bool = True,
        config: dict[str, Any] | None = None,
        connection_factory: Any | None = None,
    ):
        self.max_size = max_size
        self.load_vss = load_vss
        self.custom_config = config
        self.connection_factory = connection_factory
        self._pool: queue.Queue[duckdb.DuckDBPyConnection] = queue.Queue(maxsize=max_size)
        self._created_count = 0  # pooled connections that exist (idle or in use); guarded by _lock
        self._lock = threading.Lock()
        self._baselines: dict[int, tuple[dict[str, str], set[str]]] = {}

    def _create_connection(self) -> duckdb.DuckDBPyConnection:
        """Create and warm up a fresh DuckDB connection."""
        if self.connection_factory is not None:
            con = self.connection_factory()
        else:
            cfg = get_s3_duckdb_config()
            if self.custom_config:
                cfg.update(self.custom_config)
            con = duckdb.connect(config=cfg)
            load_duckdb_extensions(con, load_vss=self.load_vss)
        self._baselines[id(con)] = (snapshot_settings(con), snapshot_secrets(con))
        return con

    def _reserve_slot(self) -> bool:
        with self._lock:
            if self._created_count < self.max_size:
                self._created_count += 1
                return True
            return False

    def _release_slot(self) -> None:
        with self._lock:
            if self._created_count > 0:
                self._created_count -= 1

    def _discard(self, con: duckdb.DuckDBPyConnection, counted: bool) -> None:
        self._baselines.pop(id(con), None)
        try:
            con.close()
        except Exception:
            pass
        if counted:
            self._release_slot()

    def prewarm(self, count: int | None = None) -> None:
        """Pre-populate warm connections in the pool."""
        target = min(count or self.max_size, self.max_size)
        while True:
            with self._lock:
                if self._created_count >= target:
                    return
                self._created_count += 1
            try:
                self._pool.put_nowait(self._create_connection())
            except Exception as exc:
                self._release_slot()
                logger.error("Error pre-warming DuckDB connection: %s", exc)
                return

    def _take(self, timeout: float) -> tuple[duckdb.DuckDBPyConnection, bool]:
        """Return ``(connection, counted)``; ``counted`` is False for a temporary overflow one."""
        try:
            return self._pool.get_nowait(), True
        except queue.Empty:
            pass
        if self._reserve_slot():
            try:
                return self._create_connection(), True
            except Exception:
                self._release_slot()
                raise
        try:
            return self._pool.get(timeout=timeout), True
        except queue.Empty:
            logger.warning("DuckDB pool exhausted; creating temporary overflow connection")
            return self._create_connection(), False

    @contextlib.contextmanager
    def acquire(
        self, timeout: float = 10.0, sql: str | None = None
    ) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        """Acquire a warm connection, creating one on demand up to ``max_size``.

        ``sql`` is the text about to be run on it: when it cannot define a macro or function the
        slow macro sweep is skipped on release. Leave it out to always sweep.
        """
        sweep_macros = sql is None or bool(_MACRO_WORDS.search(sql))
        con, counted = self._take(timeout)
        try:
            con.execute("SELECT 1")  # quick health check before handing it out
        except Exception:
            self._baselines.pop(id(con), None)
            try:
                con.close()
            except Exception:
                pass
            try:  # the slot stays reserved for the replacement
                con = self._create_connection()
            except Exception:
                if counted:
                    self._release_slot()
                raise
        try:
            yield con
        finally:
            self._give_back(con, counted, sweep_macros)

    def _give_back(
        self, con: duckdb.DuckDBPyConnection, counted: bool, sweep_macros: bool = True
    ) -> None:
        if not counted:
            self._discard(con, counted=False)
            return
        try:
            settings, secrets = self._baselines.get(id(con), (None, None))
            reset_connection_state(con, settings, secrets, sweep_macros)
        except Exception as exc:
            # a connection that cannot be cleaned or no longer answers is replaced, never reused
            logger.warning("Discarding DuckDB connection that could not be reset: %s", exc)
            self._discard(con, counted=True)
            return
        try:
            self._pool.put_nowait(con)
        except queue.Full:
            self._discard(con, counted=True)

    def close_all(self) -> None:
        """Drain and close all pooled connections."""
        while True:
            try:
                con = self._pool.get_nowait()
            except queue.Empty:
                break
            self._discard(con, counted=True)
        with self._lock:
            self._created_count = 0


def classify_query_error(
    exc: Exception,
    sql: str = "",
    endpoint: str = "",
    bucket: str = "datalake",
) -> dict[str, Any]:
    """Analyze a DuckDB query exception and return structured diagnostic details."""
    err_str = str(exc)

    # 1. No files matched glob (Empty Dataset)
    no_files_match = re.search(
        r'No files found that match the pattern "([^"]+)"', err_str, re.IGNORECASE
    )
    if no_files_match:
        matched_path = no_files_match.group(1)
        # Extract folder prefix for storage navigation
        prefix = ""
        m_s3 = re.search(r"s3://[^/]+/(.*?)(\*\*|\*|\.parquet)", matched_path)
        if m_s3:
            prefix = m_s3.group(1).rstrip("/")

        return {
            "error_type": "EMPTY_DATASET",
            "title": "Empty Dataset (No Parquet Files Found)",
            "message": f"No files were found matching `{matched_path}`.",
            "suggestion": (
                "The target S3 prefix does not contain any Parquet files yet. "
                "Ensure your data pipeline has run or upload files via the Storage Browser."
            ),
            "storage_prefix": prefix,
            "details": err_str,
        }

    # 2. S3 Connection or Authentication Failure
    s3_conn_indicators = [
        "Could not establish connection",
        "Connection refused",
        "HTTP 403",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "Access Denied",
        "CERTIFICATE_VERIFY_FAILED",
        "Host not found",
        "Failed to connect",
        "NoSuchBucket",
    ]
    if any(ind.lower() in err_str.lower() for ind in s3_conn_indicators):
        ep_hint = f" at `{endpoint}`" if endpoint else ""
        return {
            "error_type": "S3_CONNECTION",
            "title": "S3 / MinIO Connection or Authentication Error",
            "message": f"Could not access S3 storage{ep_hint}.",
            "suggestion": (
                "Verify that the MinIO service is running, credentials (MINIO_ACCESS_KEY / MINIO_SECRET_KEY) "
                "are correct, and SSL / endpoint settings match your environment."
            ),
            "details": err_str,
        }

    # 3. Read-Only SQL violation
    if "only read-only select queries are allowed" in err_str.lower():
        return {
            "error_type": "READ_ONLY",
            "title": "Query Blocked by Read-Only Policy",
            "message": "Only read-only queries (SELECT, WITH, PRAGMA) are permitted in this mode.",
            "suggestion": "Remove any DDL or data modification statements (DROP, DELETE, UPDATE, INSERT) from your query.",
            "details": err_str,
        }

    # 4. Gemini AI UDF Errors
    if (
        "gemini" in err_str.lower()
        or "ai_query" in err_str.lower()
        or "ai_summarize" in err_str.lower()
    ):
        return {
            "error_type": "AI_UDF",
            "title": "Gemini AI Function Execution Error",
            "message": "A failure occurred while executing an AI UDF (ai_query, ai_summarize, ai_classify, ai_extract).",
            "suggestion": "Check that GEMINI_API_KEY is configured in the Admin Panel or environment, and verify quota limits.",
            "details": err_str,
        }

    # 5. SQL Syntax or Catalog Binder errors
    syntax_indicators = [
        "Parser Error",
        "Binder Error",
        "Catalog Error",
        "syntax error",
        "Referenced column",
        "Table with name",
    ]
    if any(ind.lower() in err_str.lower() for ind in syntax_indicators):
        return {
            "error_type": "SYNTAX_OR_BINDER",
            "title": "SQL Syntax or Schema Error",
            "message": "The SQL query contains syntax errors or references non-existent tables/columns.",
            "suggestion": "Click 'Fix with AI' to let Gemini resolve the schema or syntax issue, or inspect available tables in the sidebar.",
            "details": err_str,
        }

    # 6. Fallback Generic
    return {
        "error_type": "GENERIC",
        "title": "Query Execution Error",
        "message": "DuckDB encountered an unexpected error while executing this query.",
        "suggestion": "Review the technical error details below or use 'Fix with AI' to analyze the query.",
        "details": err_str,
    }
