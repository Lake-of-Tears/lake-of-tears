"""DuckDB SQL Guardrail Engine for S3 Paths.

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

# Table functions capable of reading remote or raw files in DuckDB
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
        # Could be s3:// without bucket prefix if bucket is different or implicit
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

    # Fast substring check: if no s3:// anywhere, skip full AST extraction
    if "s3://" not in cleaned.lower():
        return []

    try:
        statements = sqlglot.parse(cleaned, read="duckdb")
    except Exception as exc:
        # If unparseable but contains s3://, fail closed by extracting regex URIs
        # and raising a ParseError so the guardrail rejects it safely
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
            # Check string literals
            if isinstance(node, exp.Literal) and (node.is_string or isinstance(node.this, str)):
                val = str(node.this).strip().strip("'\"")
                if "s3://" in val.lower():
                    # Handle multiple URIs if in an array or comma-separated string
                    for uri in _S3_URI_RE.findall(val):
                        s3_refs.add(normalize_s3_uri(uri))
                    if val.lower().startswith("s3://"):
                        s3_refs.add(normalize_s3_uri(val))

            # Check table names and identifiers (e.g. FROM "s3://..." or FROM 's3://...')
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
            # Direct pattern fnmatch
            if fnmatch.fnmatch(normalized, pat_clean):
                return True, None

            # Pattern prefix matching
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
        # Check if s3:// is in query text to fail closed
        cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
        if "s3://" in cleaned.lower():
            return (
                False,
                f"Access Denied: SQL query contains S3 references but failed syntax validation: {exc}",
            )
        # If no s3:// present, allow DuckDB to emit native syntax error
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


_DANGEROUS_STATEMENT_RE = re.compile(
    r"^\s*\(?\s*(ATTACH|DETACH|INSTALL|LOAD|PRAGMA|SET|RESET|EXPORT\s+DATABASE|IMPORT\s+DATABASE|CALL)\b",
    re.IGNORECASE | re.MULTILINE,
)

_FILE_PATH_LIKE_RE = re.compile(
    r"(\.(parquet|csv|json|ndjson|jsonl|arrow|ipc|orc|db|duckdb|sqlite|txt|tsv|gz|zst)\b|^(/|\./|\.\./|~|file://|http://|https://|ftp://))",
    re.IGNORECASE,
)


def is_write_sql(sql: str) -> bool:
    """Return True if the SQL statement contains DDL or DML operations (CREATE, INSERT, UPDATE, DELETE, ALTER, DROP, COPY TO)."""
    cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()
    if not cleaned:
        return False

    try:
        statements = [s for s in sqlglot.parse(cleaned, read="duckdb") if s is not None]
        for stmt in statements:
            for node in stmt.walk():
                if isinstance(
                    node,
                    exp.Create
                    | exp.Insert
                    | exp.Update
                    | exp.Delete
                    | exp.Alter
                    | exp.Drop
                    | exp.Merge
                    | exp.TruncateTable,
                ):
                    return True
                if isinstance(node, exp.Copy):
                    # COPY ... TO is a write; COPY tbl FROM is an insert/write into tbl
                    return True
    except Exception:
        pass

    return bool(
        re.match(
            r"^\s*\(?\s*(CREATE|INSERT|UPDATE|DELETE|ALTER|DROP|COPY|TRUNCATE|MERGE|REPLACE)\b",
            cleaned,
            re.IGNORECASE,
        )
    )


def is_s3_write_path_allowed(
    s3_uri: str,
    workspace_slug: str,
    bucket: str = DEFAULT_BUCKET,
) -> tuple[bool, str | None]:
    """Validate that an S3 write target resides strictly within the active workspace prefix."""
    normalized = normalize_s3_uri(s3_uri)
    parsed = urlparse(normalized)
    if parsed.scheme.lower() != "s3":
        return False, f"Access Denied: Non-S3 write target '{s3_uri}' is not permitted."

    uri_bucket = parsed.netloc
    if bucket and uri_bucket and uri_bucket.lower() != bucket.lower():
        return (
            False,
            f"Access Denied: S3 bucket '{uri_bucket}' is not permitted. Only bucket '{bucket}' is accessible.",
        )

    clean_path = parsed.path.lstrip("/")
    clean_slug = (workspace_slug or "default").strip("/")
    ws_prefix = f"workspaces/{clean_slug}/"

    if clean_path.startswith(ws_prefix) and len(clean_path) > len(ws_prefix):
        return True, None

    return (
        False,
        f"Access Denied: S3 write path '{normalized}' must reside strictly within 's3://{bucket}/workspaces/{clean_slug}/'.",
    )


def _extract_file_and_write_targets(sql: str) -> tuple[list[str], list[str]]:
    """Extract all file arguments (from table functions, FROM literals, COPY) and write targets (COPY TO, LOCATION).

    Returns:
        (all_file_refs, write_file_refs)
    """
    cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()
    if not cleaned:
        return [], []

    file_refs: list[str] = []
    write_refs: list[str] = []

    statements = sqlglot.parse(cleaned, read="duckdb")
    for stmt in statements:
        if stmt is None:
            continue
        if len(statements) > 1:
            raise SQLGuardrailViolation(
                "Access Denied: Multi-statement batches are not permitted in a single execution.",
                status_code=400,
            )

        # Check for forbidden command AST types
        for node in stmt.walk():
            if isinstance(node, exp.Command | exp.Pragma | exp.Set | exp.LoadData):
                raise SQLGuardrailViolation(
                    "Access Denied: Administrative or system commands (ATTACH, INSTALL, LOAD, PRAGMA, SET) are not permitted.",
                    status_code=403,
                )

            # Table functions like read_parquet('/etc/passwd'), read_csv(...), etc.
            if isinstance(node, exp.Anonymous | exp.Func):
                fn_name = (
                    (node.name if isinstance(node, exp.Anonymous) else node.sql_name()) or ""
                ).lower()
                if fn_name in TABLE_FILE_FUNCTIONS:
                    for arg_node in node.find_all(exp.Literal | exp.Identifier | exp.Var):
                        if (
                            isinstance(arg_node.parent, exp.EQ | exp.Kwarg | exp.Property)
                            and arg_node.parent.this is arg_node
                        ):
                            continue
                        if isinstance(arg_node, exp.Literal) and not (
                            arg_node.is_string or isinstance(arg_node.this, str)
                        ):
                            continue
                        val = str(arg_node.this).strip().strip("'\"")
                        if val and val.lower() not in ("true", "false", "null"):
                            file_refs.append(val)

            # COPY statement targets
            if isinstance(node, exp.Copy):
                # In sqlglot, Copy.args['files'] contains target/source file literals,
                # and kind=False means COPY ... TO (write), whereas kind=True means COPY ... FROM (read).
                files = node.args.get("files") or []
                is_copy_to = not bool(node.args.get("kind"))
                for f_node in files:
                    for lit in f_node.find_all(exp.Literal):
                        val = str(lit.this).strip().strip("'\"")
                        if val:
                            file_refs.append(val)
                            if is_copy_to:
                                write_refs.append(val)
                    if isinstance(f_node, exp.Literal | exp.Var | exp.Identifier | exp.Table):
                        val = str(f_node.this).strip().strip("'\"")
                        if val and val not in file_refs:
                            file_refs.append(val)
                            if is_copy_to and val not in write_refs:
                                write_refs.append(val)

            # Direct table references that look like file paths (e.g. FROM '/etc/passwd' or FROM 'local.parquet')
            if isinstance(node, exp.Table) and not isinstance(node.this, exp.Func | exp.Anonymous):
                for part in (node.this, getattr(node, "name", None)):
                    if part is not None:
                        t_val = str(part).strip().strip("'\"")
                        if t_val and _FILE_PATH_LIKE_RE.search(t_val):
                            file_refs.append(t_val)

            # Location properties in CREATE TABLE ... LOCATION '...'
            if isinstance(node, exp.LocationProperty):
                val = str(node.this).strip().strip("'\"")
                if val:
                    file_refs.append(val)
                    write_refs.append(val)

    return file_refs, write_refs


def validate_statement_guardrails(
    sql: str,
    workspace_slug: str,
    catalogs: list[dict[str, Any]] | None = None,
    allowed_patterns: list[dict[str, Any] | str] | None = None,
    is_superadmin: bool = False,
    bucket: str = DEFAULT_BUCKET,
) -> tuple[bool, str | None]:
    """Validate SQL statement execution for host filesystem isolation, dangerous commands, S3 reads, and S3 writes."""
    cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()
    if not cleaned:
        return False, "SQL statement cannot be empty."

    if _DANGEROUS_STATEMENT_RE.search(cleaned):
        return (
            False,
            "Access Denied: Administrative or host-level commands (ATTACH, DETACH, INSTALL, LOAD, PRAGMA, SET, EXPORT, IMPORT, CALL) are not permitted.",
        )

    try:
        file_refs, write_refs = _extract_file_and_write_targets(sql)
    except SQLGuardrailViolation as exc:
        return False, exc.message
    except Exception as exc:
        if "s3://" in cleaned.lower() or _FILE_PATH_LIKE_RE.search(cleaned):
            return (
                False,
                f"Access Denied: SQL statement failed syntax validation during security inspection: {exc}",
            )
        return True, None

    # 1. Prevent illegal host filesystem or arbitrary network file access (applies to ALL users)
    for ref in file_refs:
        norm = ref.strip().strip("'\"")
        if not norm.lower().startswith("s3://"):
            return (
                False,
                f"Access Denied: Host filesystem or non-S3 file path '{norm}' is not permitted. Only workspace S3 paths ('s3://{bucket}/workspaces/{workspace_slug}/...') are allowed.",
            )

    # 2. Enforce S3 read boundary
    allowed_read, read_err = validate_sql_s3_paths(
        sql=sql,
        workspace_slug=workspace_slug,
        catalogs=catalogs,
        allowed_patterns=[str(p) for p in (allowed_patterns or [])],
        is_superadmin=is_superadmin,
        bucket=bucket,
    )
    if not allowed_read:
        return False, read_err

    # 3. Enforce S3 write boundary (COPY TO / LOCATION must stay in workspace prefix)
    if not is_superadmin:
        for w_ref in write_refs:
            allowed_write, write_err = is_s3_write_path_allowed(
                s3_uri=w_ref,
                workspace_slug=workspace_slug,
                bucket=bucket,
            )
            if not allowed_write:
                return False, write_err

    return True, None
