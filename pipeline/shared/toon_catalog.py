"""TOON (Token-Oriented Object Notation) serialization, parsing, and DDL generation for Lake of Tears catalog assets."""

from __future__ import annotations

import re
from typing import Any

try:
    from pipeline.shared.toon import dump_toon, parse_toon
except ImportError:
    try:
        from toon import dump_toon, parse_toon
    except ImportError:
        from ui.toon import dump_toon, parse_toon


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val).strip()


def table_to_toon(
    table: dict[str, Any] | Any,
    catalog_name: str = "",
    schema_name: str = "",
) -> str:
    """Serialize a CatalogTable model or dict into a standard compact TOON table specification."""
    if hasattr(table, "__dict__"):
        table_dict = {
            "name": getattr(table, "name", ""),
            "slug": getattr(table, "slug", ""),
            "description": getattr(table, "description", None),
            "s3_path_pattern": getattr(table, "s3_path_pattern", None),
            "column_defs": getattr(table, "column_defs", []) or [],
        }
    else:
        table_dict = dict(table)

    tbl_slug = _safe_str(table_dict.get("slug") or table_dict.get("name") or "table")
    tbl_name = _safe_str(table_dict.get("name") or tbl_slug)
    cat_slug = _safe_str(catalog_name)
    sch_slug = _safe_str(schema_name)
    desc = _safe_str(table_dict.get("description"), "Managed lakehouse table")
    s3_path = _safe_str(table_dict.get("s3_path_pattern"))

    raw_cols = table_dict.get("column_defs") or []
    formatted_cols: list[dict[str, Any]] = []

    for col in raw_cols:
        if isinstance(col, dict):
            c_name = _safe_str(col.get("name"))
            c_type = _safe_str(col.get("type"), "VARCHAR").upper()
            c_desc = _safe_str(col.get("description"), "")
            c_nullable = bool(col.get("nullable", True))
        elif hasattr(col, "name"):
            c_name = _safe_str(getattr(col, "name"))
            c_type = _safe_str(getattr(col, "type", "VARCHAR"), "VARCHAR").upper()
            c_desc = _safe_str(getattr(col, "description", ""), "")
            c_nullable = bool(getattr(col, "nullable", True))
        else:
            continue

        if c_name:
            formatted_cols.append({
                "name": c_name,
                "type": c_type,
                "description": c_desc,
                "nullable": c_nullable,
            })

    data: dict[str, Any] = {
        "table": tbl_slug,
        "name": tbl_name,
    }
    if cat_slug:
        data["catalog"] = cat_slug
    if sch_slug:
        data["schema"] = sch_slug
    if desc:
        data["description"] = desc
    if s3_path:
        data["s3_path_pattern"] = s3_path

    if formatted_cols:
        data["columns"] = formatted_cols

    return dump_toon(data)


def schema_to_toon(
    schema: dict[str, Any] | Any,
    catalog_name: str = "",
) -> str:
    """Serialize a CatalogSchema model or dict into a standard compact TOON schema specification."""
    if hasattr(schema, "__dict__"):
        schema_dict = {
            "name": getattr(schema, "name", ""),
            "slug": getattr(schema, "slug", ""),
            "tier": getattr(schema, "tier", "custom"),
            "description": getattr(schema, "description", None),
            "tables": getattr(schema, "tables", []) or [],
        }
    else:
        schema_dict = dict(schema)

    sch_slug = _safe_str(schema_dict.get("slug") or schema_dict.get("name") or "schema")
    sch_name = _safe_str(schema_dict.get("name") or sch_slug)
    tier = _safe_str(schema_dict.get("tier"), "custom")
    desc = _safe_str(schema_dict.get("description"), "Lakehouse catalog schema")
    cat_slug = _safe_str(catalog_name)

    raw_tables = schema_dict.get("tables") or []
    table_rows: list[dict[str, Any]] = []

    for t in raw_tables:
        if isinstance(t, dict):
            t_name = _safe_str(t.get("slug") or t.get("name"))
            cols = t.get("column_defs") or []
            col_count = len(cols) if isinstance(cols, list) else 0
            t_desc = _safe_str(t.get("description"), "")
        elif hasattr(t, "name"):
            t_name = _safe_str(getattr(t, "slug", None) or getattr(t, "name", ""))
            cols = getattr(t, "column_defs", []) or []
            col_count = len(cols) if isinstance(cols, list) else 0
            t_desc = _safe_str(getattr(t, "description", ""), "")
        else:
            continue

        if t_name:
            table_rows.append({
                "name": t_name,
                "columns": col_count,
                "description": t_desc,
            })

    data: dict[str, Any] = {
        "schema": sch_slug,
        "name": sch_name,
    }
    if cat_slug:
        data["catalog"] = cat_slug
    if tier:
        data["tier"] = tier
    if desc:
        data["description"] = desc

    if table_rows:
        data["tables"] = table_rows

    return dump_toon(data)


def toon_to_table_spec(toon_str: str) -> dict[str, Any]:
    """Parse a TOON table specification into normalized schema fields and column definitions."""
    parsed = parse_toon(toon_str)

    table_name = _safe_str(parsed.get("table") or parsed.get("name") or "untitled_table")
    clean_slug = re.sub(r"[^\w\s-]", "", table_name).strip().lower()
    clean_slug = re.sub(r"[-\s]+", "_", clean_slug) or "table"

    desc = _safe_str(parsed.get("description"))
    cat = _safe_str(parsed.get("catalog"))
    sch = _safe_str(parsed.get("schema"))
    s3_path = _safe_str(parsed.get("s3_path_pattern"))

    raw_columns = parsed.get("columns") or []
    columns: list[dict[str, Any]] = []

    for col in raw_columns:
        if isinstance(col, dict):
            c_name = _safe_str(col.get("name"))
            c_type = _safe_str(col.get("type"), "VARCHAR").upper()
            c_desc = _safe_str(col.get("description"))
            c_null = bool(col.get("nullable", True))
            if c_name:
                columns.append({
                    "name": c_name,
                    "type": c_type,
                    "description": c_desc,
                    "nullable": c_null,
                })

    return {
        "table": clean_slug,
        "name": parsed.get("name") or clean_slug,
        "catalog": cat,
        "schema": sch,
        "description": desc,
        "s3_path_pattern": s3_path,
        "columns": columns,
    }


def toon_to_sql_ddl(spec_or_toon: str | dict[str, Any]) -> str:
    """Generate SQL DDL statement from TOON string or parsed table specification."""
    if isinstance(spec_or_toon, str):
        spec = toon_to_table_spec(spec_or_toon)
    else:
        spec = spec_or_toon

    sch = spec.get("schema")
    table = spec.get("table") or "new_table"
    table_ident = f'"{sch}"."{table}"' if sch else f'"{table}"'

    cols = spec.get("columns") or []
    if not cols:
        return f"CREATE TABLE {table_ident} (\n  id VARCHAR\n);"

    col_lines: list[str] = []
    for c in cols:
        name = c.get("name", "col")
        c_type = c.get("type", "VARCHAR").upper()
        nullable = c.get("nullable", True)
        null_clause = "" if nullable else " NOT NULL"
        col_lines.append(f'  "{name}" {c_type}{null_clause}')

    return f"CREATE TABLE {table_ident} (\n" + ",\n".join(col_lines) + "\n);"
