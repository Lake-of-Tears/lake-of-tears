"""DuckDB SQL Guardrail Engine for S3 Paths (UI Service).

Enforces AST-level SQL validation on DuckDB queries to prevent unauthorized
raw S3 reads outside the active workspace prefix or approved catalog boundaries.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import posixpath
import re
from typing import Any
from urllib.parse import urlparse

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = os.getenv("MINIO_BUCKET", "datalake")
SYSTEM_ALLOWED_PREFIXES = ("raw/",)
_GLOB_CHARS = re.compile(r"[*?\[]")
_S3_URI_RE = re.compile(r"s3://[^\s'\",;)\]]+", re.IGNORECASE)

TABLE_FILE_FUNCTIONS = {
    "read_parquet",
    "parquet_scan",
    "scan_parquet",
    "read_csv",
    "read_csv_auto",
    "scan_csv",
    "read_json",
    "read_json_auto",
    "read_ndjson",
    "read_ndjson_auto",
    "scan_json",
    "read_text",
    "read_blob",
    "glob",
    "delta_scan",
    "iceberg_scan",
}


class SQLGuardrailViolation(Exception):
    """Raised when a query attempts to read unauthorized S3 paths outside workspace boundary."""

    def __init__(
        self, message: str, offending_path: str | None = None, status_code: int = 403
    ) -> None:
        super().__init__(message)
        self.message = message
        self.offending_path = offending_path
        self.status_code = status_code


def s3_prefix_for_pattern(pattern: str, bucket: str = DEFAULT_BUCKET) -> str | None:
    """Return the key prefix (ending in '/') covered by a table's s3_path_pattern.

    Example:
        's3://datalake/workspaces/marketing/sales/**/*.parquet' -> 'workspaces/marketing/sales/'
    """
    pattern = pattern.strip()
    scheme = f"s3://{bucket}/"
    if not pattern.startswith(scheme):
        parsed = urlparse(pattern)
        if parsed.scheme.lower() == "s3":
            key = parsed.path.lstrip("/")
        else:
            return None
    else:
        key = pattern[len(scheme) :]

    glob = _GLOB_CHARS.search(key)
    if glob:
        key = key[: glob.start()]
    if "/" not in key:
        return None
    prefix = key.rsplit("/", 1)[0].strip("/")
    return f"{prefix}/" if prefix else None


def extract_s3_patterns_from_catalogs(catalogs: list[dict[str, Any]] | None) -> list[str]:
    """Extract all configured s3_path_pattern and s3_vector_path strings from workspace catalog metadata."""
    patterns: list[str] = []
    if not catalogs:
        return patterns

    for cat in catalogs:
        for schema in cat.get("schemas") or []:
            for table in schema.get("tables") or []:
                pat = (table.get("s3_path_pattern") or "").strip()
                if pat:
                    patterns.append(pat)
                vec_cfg = table.get("vector_index") or {}
                if isinstance(vec_cfg, dict):
                    vec_path = (vec_cfg.get("s3_vector_path") or "").strip()
                    if vec_path:
                        patterns.append(vec_path)
    return patterns


def normalize_s3_uri(uri: str) -> str:
    """Normalize an S3 URI, collapsing dot segments (/../ and /./) to resolve directory traversal."""
    parsed = urlparse(uri.strip().strip("'\""))
    if parsed.scheme.lower() != "s3":
        return uri.strip().strip("'\"")
    norm_path = posixpath.normpath(parsed.path)
    if parsed.path.endswith("/") and not norm_path.endswith("/"):
        norm_path += "/"
    return f"{parsed.scheme.lower()}://{parsed.netloc}{norm_path}"


def extract_s3_references(sql: str) -> list[str]:
    """Extract all S3 URIs referenced in a SQL query AST or query text.

    Finds references in:
    - DuckDB table functions (read_parquet, read_csv, delta_scan, glob, etc.)
    - Direct table references (FROM 's3://...', JOIN 's3://...')
    - String literals, Identifiers, and function arguments starting with or containing 's3://'
    """
    cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()
    if not cleaned:
        return []

    s3_refs: set[str] = set()

    if "s3://" not in cleaned.lower():
        return []

    try:
        statements = sqlglot.parse(cleaned, read="duckdb")
    except Exception as exc:
        regex_matches = _S3_URI_RE.findall(cleaned)
        if regex_matches:
            for match in regex_matches:
                s3_refs.add(normalize_s3_uri(match))
            raise SQLGuardrailViolation(
                f"Access Denied: SQL query contains S3 references but failed syntax validation: {exc}",
                status_code=403,
            ) from exc
        raise

    for stmt in statements:
        if stmt is None:
            continue
        for node in stmt.walk():
            if isinstance(node, exp.Literal) and (node.is_string or isinstance(node.this, str)):
                val = str(node.this).strip().strip("'\"")
                if "s3://" in val.lower():
                    for uri in _S3_URI_RE.findall(val):
                        s3_refs.add(normalize_s3_uri(uri))
                    if val.lower().startswith("s3://"):
                        s3_refs.add(normalize_s3_uri(val))

            elif isinstance(node, exp.Identifier | exp.Var):
                val = str(node.this).strip().strip("'\"")
                if val.lower().startswith("s3://"):
                    s3_refs.add(normalize_s3_uri(val))

            elif isinstance(node, exp.Table):
                t_name = str(node.this).strip().strip("'\"")
                if t_name.lower().startswith("s3://"):
                    s3_refs.add(normalize_s3_uri(t_name))
                if (
                    hasattr(node, "name")
                    and node.name
                    and str(node.name).lower().startswith("s3://")
                ):
                    s3_refs.add(normalize_s3_uri(str(node.name)))

    return sorted(s3_refs)


def is_s3_path_allowed(
    s3_uri: str,
    workspace_slug: str,
    allowed_patterns: list[str] | None = None,
    bucket: str = DEFAULT_BUCKET,
) -> tuple[bool, str | None]:
    """Validate whether an S3 URI resides strictly within the active workspace jail,

    an approved catalog pattern, or an allowed system prefix.
    """
    normalized = normalize_s3_uri(s3_uri)
    parsed = urlparse(normalized)
    if parsed.scheme.lower() != "s3":
        return False, f"Access Denied: Non-S3 storage URI '{s3_uri}' is not permitted."

    uri_bucket = parsed.netloc
    if bucket and uri_bucket and uri_bucket.lower() != bucket.lower():
        return (
            False,
            f"Access Denied: S3 bucket '{uri_bucket}' is not permitted. Only bucket '{bucket}' is accessible.",
        )

    clean_path = parsed.path.lstrip("/")
    clean_slug = (workspace_slug or "default").strip("/")
    ws_prefix = f"workspaces/{clean_slug}/"

    # 1. Active workspace prefix check
    if clean_path.startswith(ws_prefix) or clean_path == f"workspaces/{clean_slug}":
        return True, None

    # 2. System allowed prefixes (raw/, embeddings/)
    for sys_pref in SYSTEM_ALLOWED_PREFIXES:
        if clean_path.startswith(sys_pref):
            return True, None

    # 3. Approved catalog patterns and prefixes
    if allowed_patterns:
        for pat in allowed_patterns:
            pat_clean = pat.strip()
            if fnmatch.fnmatch(normalized, pat_clean):
                return True, None

            pat_pref = s3_prefix_for_pattern(pat_clean, bucket=bucket)
            if pat_pref and clean_path.startswith(pat_pref):
                return True, None

    return (
        False,
        f"Access Denied: S3 path '{normalized}' is outside permitted prefix 'workspaces/{clean_slug}/' and approved catalogs.",
    )


def validate_sql_s3_paths(
    sql: str,
    workspace_slug: str,
    catalogs: list[dict[str, Any]] | None = None,
    allowed_patterns: list[str] | None = None,
    is_superadmin: bool = False,
    bucket: str = DEFAULT_BUCKET,
) -> tuple[bool, str | None]:
    """Validate that all raw S3 reads in a SQL query stay within active workspace boundary.

    Returns:
        (True, None) if permitted.
        (False, error_message) if access is denied.
    """
    if is_superadmin:
        return True, None

    all_allowed = list(allowed_patterns or [])
    if catalogs:
        all_allowed.extend(extract_s3_patterns_from_catalogs(catalogs))

    try:
        s3_uris = extract_s3_references(sql)
    except SQLGuardrailViolation as exc:
        return False, exc.message
    except Exception as exc:
        cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
        if "s3://" in cleaned.lower():
            return (
                False,
                f"Access Denied: SQL query contains S3 references but failed syntax validation: {exc}",
            )
        return True, None

    for uri in s3_uris:
        allowed, reason = is_s3_path_allowed(
            s3_uri=uri,
            workspace_slug=workspace_slug,
            allowed_patterns=all_allowed,
            bucket=bucket,
        )
        if not allowed:
            return False, reason

    return True, None


def enforce_sql_s3_guardrails(
    sql: str,
    workspace_slug: str,
    catalogs: list[dict[str, Any]] | None = None,
    allowed_patterns: list[str] | None = None,
    is_superadmin: bool = False,
    bucket: str = DEFAULT_BUCKET,
) -> None:
    """Enforce SQL guardrails, raising SQLGuardrailViolation if query attempts unauthorized S3 reads."""
    allowed, reason = validate_sql_s3_paths(
        sql=sql,
        workspace_slug=workspace_slug,
        catalogs=catalogs,
        allowed_patterns=allowed_patterns,
        is_superadmin=is_superadmin,
        bucket=bucket,
    )
    if not allowed:
        raise SQLGuardrailViolation(
            reason or "Access Denied: S3 path violates workspace boundaries."
        )
