"""Unified DuckDB table read helper for Lake of Tears catalog tables.

Routes Iceberg-format tables to `iceberg_scan(...)` (pinned to the catalog's current metadata
file, so readers always see exactly the committed snapshot) and legacy tables to
`read_parquet(...)`. Kept dependency-free: identical copies live in backend/, ui/ and
pipeline/shared/ because each container ships only its own directory.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

FORMAT_PARQUET = "parquet"
FORMAT_ICEBERG = "iceberg"


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _field(table: Any, name: str) -> Any:
    if isinstance(table, dict):
        return table.get(name)
    return getattr(table, name, None)


def table_format_of(table: Any) -> str:
    """Return the normalized storage format ('parquet' | 'iceberg') of a catalog table."""
    fmt = str(_field(table, "format") or FORMAT_PARQUET).strip().lower()
    return FORMAT_ICEBERG if fmt == FORMAT_ICEBERG else FORMAT_PARQUET


def is_iceberg(table: Any) -> bool:
    return table_format_of(table) == FORMAT_ICEBERG


def table_read_scan(
    s3_path_pattern: str | None,
    *,
    table_format: str | None = FORMAT_PARQUET,
    metadata_location: str | None = None,
    snapshot_id: int | str | None = None,
    as_of_timestamp: datetime | str | None = None,
    union_by_name: bool = False,
    pinned: bool = True,
) -> str:
    """Build the DuckDB table-function expression that reads a catalog table.

    Returns e.g. `read_parquet('s3://…/**/*.parquet')` or
    `iceberg_scan('s3://…/metadata/00003-….metadata.json', snapshot_from_id = 42)`.
    Time travel arguments are only valid for Iceberg tables.
    """
    fmt = (table_format or FORMAT_PARQUET).strip().lower()
    if fmt == FORMAT_ICEBERG:
        target = metadata_location
        if not pinned or not target:
            # Unpinned: resolve the current version through the table's
            # `metadata/version-hint.text`, which the catalog rewrites on every commit. Meant
            # for long-lived views (e.g. Lakeset) that must follow new commits.
            target = (s3_path_pattern or "").rstrip("/") if not pinned else None
        if not target:
            raise ValueError("Iceberg table has no metadata location registered in the catalog")
        args = [_sql_str(target)]
        if snapshot_id is not None:
            args.append(f"snapshot_from_id = {int(snapshot_id)}")
        elif as_of_timestamp is not None:
            ts = (
                as_of_timestamp.isoformat(sep=" ")
                if isinstance(as_of_timestamp, datetime)
                else str(as_of_timestamp)
            )
            args.append(f"snapshot_from_timestamp = CAST({_sql_str(ts)} AS TIMESTAMP)")
        return f"iceberg_scan({', '.join(args)})"

    if snapshot_id is not None or as_of_timestamp is not None:
        raise ValueError("Time travel is only supported for Iceberg tables")
    if not s3_path_pattern:
        raise ValueError("Table has no storage path")
    if union_by_name:
        return f"read_parquet({_sql_str(s3_path_pattern)}, union_by_name = true)"
    return f"read_parquet({_sql_str(s3_path_pattern)})"


def table_scan_for(
    table: Any,
    *,
    snapshot_id: int | str | None = None,
    as_of_timestamp: datetime | str | None = None,
    union_by_name: bool = False,
    pinned: bool = True,
) -> str:
    """`table_read_scan` for a CatalogTable model, API response dict, or catalog manifest dict."""
    return table_read_scan(
        _field(table, "s3_path_pattern"),
        table_format=table_format_of(table),
        metadata_location=_field(table, "iceberg_metadata_location"),
        snapshot_id=snapshot_id,
        as_of_timestamp=as_of_timestamp,
        union_by_name=union_by_name,
        pinned=pinned,
    )


def ensure_iceberg_loaded(con: Any) -> bool:
    """Load DuckDB's official `iceberg` extension on a connection (installing it if needed)."""
    try:
        con.execute("LOAD iceberg;")
        return True
    except Exception:
        try:
            con.execute("INSTALL iceberg; LOAD iceberg;")
            return True
        except Exception:
            return False


def catalogs_have_iceberg(catalogs: list[dict[str, Any]] | None) -> bool:
    for cat in catalogs or []:
        for schema in cat.get("schemas") or []:
            for table in schema.get("tables") or []:
                if is_iceberg(table):
                    return True
    return False


# ── Time travel ───────────────────────────────────────────────────────────────

_IDENT = r'(?:"[^"]+"|[A-Za-z_][\w$]*)'
_TIME_TRAVEL_RE = re.compile(
    rf"""
    (?P<skip>'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/)
    |
    (?P<ref>{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}})
    \s+
    (?:
        (?:FOR\s+SYSTEM_VERSION\s+AS\s+OF|VERSION\s+AS\s+OF)\s+(?P<version>\d+)
        |
        (?:FOR\s+SYSTEM_TIME\s+AS\s+OF|TIMESTAMP\s+AS\s+OF)\s+(?:TIMESTAMP\s+)?'(?P<ts>[^']+)'
    )
    (?P<tail>\s+(?:AS\s+)?(?P<alias>{_IDENT}))?
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)
# Words that can follow a table reference and therefore are not a table alias
_NOT_ALIAS = {
    "where", "join", "inner", "left", "right", "full", "cross", "natural", "on", "using",
    "group", "order", "having", "limit", "offset", "union", "intersect", "except", "window",
    "qualify", "sample", "tablesample", "pivot", "unpivot", "lateral", "asof", "semi", "anti",
    "positional", "fetch", "for", "returning", "set", "values", "select", "from",
}  # fmt: skip


def _safe_ident(name: str | None) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]", "_", (name or "").strip())
    if not slug or slug[0].isdigit():
        slug = f"t_{slug}"
    return slug


def _find_table(
    catalogs: list[dict[str, Any]] | None, parts: list[str]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Resolve `[catalog.][schema.]table` against the catalog manifest → (schema, table)."""
    table_name = _safe_ident(parts[-1]).lower()
    schema_name = _safe_ident(parts[-2]).lower() if len(parts) >= 2 else None
    catalog_name = _safe_ident(parts[-3]).lower() if len(parts) >= 3 else None
    for cat in catalogs or []:
        if catalog_name and catalog_name not in (
            _safe_ident(cat.get("slug")).lower(),
            _safe_ident(cat.get("name")).lower(),
        ):
            continue
        for schema in cat.get("schemas") or []:
            if (
                schema_name
                and schema_name != _safe_ident(schema.get("slug") or schema.get("name")).lower()
            ):
                continue
            for table in schema.get("tables") or []:
                if _safe_ident(table.get("slug") or table.get("name")).lower() == table_name:
                    return schema, table
    return None


def rewrite_time_travel(sql: str, catalogs: list[dict[str, Any]] | None) -> tuple[str, list[str]]:
    """Rewrite Iceberg time-travel clauses into references to snapshot-pinned views.

    Supports `tbl FOR SYSTEM_VERSION AS OF <snapshot_id>`, `tbl VERSION AS OF <snapshot_id>`,
    `tbl FOR SYSTEM_TIME AS OF '<timestamp>'` and `tbl TIMESTAMP AS OF '<timestamp>'`.

    Returns `(sql, view_statements)`: run the view statements on the connection (after the
    regular catalog views) before executing the rewritten SQL. Raises ValueError when the
    referenced table is not an Iceberg table.
    """
    if not sql or " as of " not in " ".join(sql.lower().split()):
        return sql, []

    views: dict[str, str] = {}

    def _replace(match: re.Match[str]) -> str:
        if match.group("skip") is not None:
            return match.group(0)
        ref = match.group("ref")
        parts = [p.strip().strip('"') for p in re.split(r"\s*\.\s*", ref)]
        found = _find_table(catalogs, parts)
        if found is None:
            raise ValueError(f"Time travel: table '{ref}' was not found in the workspace catalog")
        schema, table = found
        if not is_iceberg(table):
            raise ValueError(
                f"Time travel is only supported for Iceberg tables; '{ref}' is a Parquet table"
            )
        schema_ident = _safe_ident(schema.get("slug") or schema.get("name") or "default")
        table_ident = _safe_ident(table.get("slug") or table.get("name"))
        if match.group("version") is not None:
            version = int(match.group("version"))
            view = f"{table_ident}__snapshot_{version}"
            scan = table_scan_for(table, snapshot_id=version)
        else:
            ts = match.group("ts").strip()
            view = f"{table_ident}__asof_{re.sub(r'[^0-9]', '', ts) or 'ts'}"
            scan = table_scan_for(table, as_of_timestamp=ts)
        views[f'"{schema_ident}"."{view}"'] = scan

        alias = match.group("alias")
        tail = match.group("tail") or ""
        if alias is None or alias.strip('"').lower() in _NOT_ALIAS:
            # Keep `table.column` references working by aliasing back to the table name.
            return f'"{schema_ident}"."{view}" AS "{table_ident}"{tail}'
        return f'"{schema_ident}"."{view}"{tail}'

    rewritten = _TIME_TRAVEL_RE.sub(_replace, sql)
    statements = [
        f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {scan}" for name, scan in views.items()
    ]
    return rewritten, statements


def apply_time_travel_views(con: Any, view_statements: list[str]) -> None:
    """Create the snapshot-pinned views produced by `rewrite_time_travel` on a connection."""
    if not view_statements:
        return
    ensure_iceberg_loaded(con)
    for statement in view_statements:
        schema = statement.split('"')[1]
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        con.execute(statement)
