"""Dedicated Statement Execution REST API (/api/sql/statements).

Modeled after the Databricks Statement Execution API to provide a unified
interface for the Angler CLI (`angler sql exec`) and autonomous LLM agents
to execute analytical queries (SELECT) and DDL/DML statements (CREATE TABLE,
CTAS, INSERT, ALTER, DROP, COPY) with workspace isolation guardrails,
catalog synchronization, result chunking/pagination, and cancellation.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import ai_service
import duckdb
import sqlglot
from auth import get_ai_workspace_and_role, get_current_user
from database import SessionLocal, get_db
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, status
from models import Catalog, CatalogSchema, CatalogTable, User, Workspace
from schemas import (
    StatementChunkInfo,
    StatementColumnInfo,
    StatementErrorInfo,
    StatementExecutionRequest,
    StatementExecutionResponse,
    StatementManifest,
    StatementResultChunk,
    StatementSchemaInfo,
    StatementStatus,
)
from sql_guardrails import (
    DEFAULT_BUCKET,
    is_write_sql,
    validate_statement_guardrails,
)
from sqlalchemy.orm import Session
from sqlglot import exp

logger = logging.getLogger("lake.backend.sql.router")

router = APIRouter()

MAX_STORED_STATEMENTS = 500
STATEMENT_TTL_SECONDS = 3600
DEFAULT_CHUNK_BYTE_LIMIT = 1048576  # 1 MiB per chunk


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return slug or "default"


@dataclass
class StoredStatement:
    statement_id: str
    workspace_id: str
    workspace_slug: str
    user_id: str
    statement: str
    catalog: str
    schema_name: str
    warehouse_id: str
    format: str
    status: StatementStatus
    manifest: StatementManifest | None = None
    chunks: list[StatementResultChunk] = field(default_factory=list)
    execution_ms: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    created_ts: float = field(default_factory=time.monotonic)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    connection: duckdb.DuckDBPyConnection | None = None


class StatementStore:
    """Thread-safe bounded TTL store for statement execution state and result chunks."""

    def __init__(
        self, max_items: int = MAX_STORED_STATEMENTS, ttl_seconds: int = STATEMENT_TTL_SECONDS
    ) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[str, StoredStatement] = OrderedDict()
        self._max_items = max_items
        self._ttl_seconds = ttl_seconds

    def _evict_expired_unlocked(self) -> None:
        now = time.monotonic()
        expired = [
            sid
            for sid, item in self._items.items()
            if (now - item.created_ts) > self._ttl_seconds
            and item.status.state not in ("PENDING", "RUNNING")
        ]
        for sid in expired:
            self._items.pop(sid, None)
        while len(self._items) > self._max_items:
            self._items.popitem(last=False)

    def put(self, stmt: StoredStatement) -> None:
        with self._lock:
            self._evict_expired_unlocked()
            self._items[stmt.statement_id] = stmt
            self._items.move_to_end(stmt.statement_id)

    def get(self, statement_id: str) -> StoredStatement | None:
        with self._lock:
            self._evict_expired_unlocked()
            return self._items.get(statement_id)

    def list_for_workspace(
        self,
        workspace_id: str,
        workspace_slug: str,
        limit: int = 50,
        state_filter: str | None = None,
    ) -> list[StoredStatement]:
        with self._lock:
            self._evict_expired_unlocked()
            matched = [
                item
                for item in reversed(self._items.values())
                if (item.workspace_id == workspace_id or item.workspace_slug == workspace_slug)
                and (not state_filter or item.status.state == state_filter.upper())
            ]
            return matched[:limit]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


statement_store = StatementStore()


def _resolve_sql_context(
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[Workspace, str, User]:
    slug = workspace_slug or x_workspace_slug
    ws_id = workspace_id or x_workspace_id or lake_active_workspace or lake_workspace_id
    ws, role = get_ai_workspace_and_role(
        workspace_id=ws_id,
        workspace_slug=slug,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    return ws, role, current_user


def _prepare_parameters(
    sql: str, parameters: list[Any] | None
) -> tuple[str, dict[str, Any] | list[Any] | None]:
    """Normalize Databricks-style named or positional parameters for DuckDB execution."""
    if not parameters:
        return sql, None

    # Check if parameters are named objects: [{"name": "x", "value": 10, "type": "INTEGER"}]
    is_named = all(isinstance(p, dict) and p.get("name") is not None for p in parameters)
    if is_named:
        named_dict: dict[str, Any] = {}
        rewritten_sql = sql
        for p in parameters:
            name = str(p["name"]).lstrip(":$")
            val = _coerce_param_value(p.get("value"), p.get("type"))
            named_dict[name] = val
            # Convert Databricks :param syntax to DuckDB $param outside string literals
            rewritten_sql = _replace_colon_param_outside_quotes(rewritten_sql, name)
        return rewritten_sql, named_dict

    # Positional parameters
    pos_list: list[Any] = []
    for p in parameters:
        if isinstance(p, dict) and "value" in p:
            pos_list.append(_coerce_param_value(p.get("value"), p.get("type")))
        else:
            pos_list.append(p)
    return sql, pos_list


def _coerce_param_value(value: Any, type_hint: str | None) -> Any:
    if value is None or not type_hint:
        return value
    t = type_hint.strip().upper()
    try:
        if t in ("INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT"):
            return int(value)
        if t in ("FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL"):
            return float(value)
        if t in ("BOOL", "BOOLEAN"):
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes")
            return bool(value)
        if t in ("STRING", "VARCHAR", "TEXT"):
            return str(value)
    except Exception:
        return value
    return value


def _replace_colon_param_outside_quotes(sql: str, param_name: str) -> str:
    """Replace :param_name with $param_name outside of single/double quoted strings."""
    pattern = re.compile(
        r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")|:(?<!::)\b" + re.escape(param_name) + r"\b"
    )

    def _repl(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        return f"${param_name}"

    return pattern.sub(_repl, sql)


def _extract_ddl_dml_target(
    sql: str, default_schema: str
) -> tuple[str | None, str | None, str | None]:
    """Inspect SQL AST to identify managed DDL/DML target table.

    Returns:
        (operation, schema_slug, table_slug) where operation is one of
        'CREATE', 'INSERT', 'UPDATE', 'DELETE', 'ALTER', 'DROP', or None.
    """
    cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()
    if not cleaned:
        return None, None, None

    try:
        stmt = sqlglot.parse_one(cleaned, read="duckdb")
    except Exception:
        return None, None, None

    if isinstance(stmt, exp.Create):
        kind = (stmt.args.get("kind") or "").upper()
        if kind != "TABLE":
            return None, None, None
        # Ignore TEMPORARY tables
        props = stmt.args.get("properties")
        if props and any(isinstance(p, exp.TemporaryProperty) for p in props.expressions):
            return None, None, None
        if re.match(r"^\s*CREATE\s+(OR\s+REPLACE\s+)?TEMP(ORARY)?\s+TABLE\b", cleaned, re.I):
            return None, None, None
        tbl = stmt.find(exp.Table)
        if tbl and tbl.name:
            sch = tbl.db or default_schema or "default"
            return "CREATE", ai_service._safe_identifier(sch), ai_service._safe_identifier(tbl.name)

    elif isinstance(stmt, exp.Insert | exp.Update | exp.Delete | exp.Alter):
        op = stmt.__class__.__name__.upper()
        tbl = stmt.find(exp.Table)
        if tbl and tbl.name and not str(tbl.name).lower().startswith("s3://"):
            sch = tbl.db or default_schema or "default"
            return op, ai_service._safe_identifier(sch), ai_service._safe_identifier(tbl.name)

    elif isinstance(stmt, exp.Drop):
        kind = (stmt.args.get("kind") or "").upper()
        if kind == "TABLE":
            tbl = stmt.find(exp.Table)
            if tbl and tbl.name:
                sch = tbl.db or default_schema or "default"
                return (
                    "DROP",
                    ai_service._safe_identifier(sch),
                    ai_service._safe_identifier(tbl.name),
                )

    return None, None, None


def _find_catalog_table_s3_path(
    catalogs: list[dict[str, Any]] | None,
    schema_slug: str,
    table_slug: str,
) -> str | None:
    """Find the existing S3 path pattern for a registered catalog table."""
    if not catalogs:
        return None
    for cat in catalogs:
        for sch in cat.get("schemas") or []:
            s_slug = ai_service._safe_identifier(sch.get("slug") or sch.get("name") or "default")
            if s_slug.lower() != schema_slug.lower() and schema_slug.lower() != "default":
                continue
            for tbl in sch.get("tables") or []:
                t_slug = ai_service._safe_identifier(tbl.get("slug") or tbl.get("name") or "")
                if t_slug.lower() == table_slug.lower():
                    return (tbl.get("s3_path_pattern") or "").strip() or None
    return None


def _export_table_to_workspace_s3(
    con: duckdb.DuckDBPyConnection,
    workspace_slug: str,
    catalog_slug: str,
    schema_slug: str,
    table_slug: str,
    bucket: str = DEFAULT_BUCKET,
) -> str:
    """Export a DuckDB table to workspace S3 Parquet storage and return the S3 URI."""
    clean_ws = (workspace_slug or "default").strip("/")
    clean_cat = _slugify(catalog_slug or "main")
    clean_sch = _slugify(schema_slug or "default")
    clean_tbl = _slugify(table_slug)
    s3_uri = f"s3://{bucket}/workspaces/{clean_ws}/{clean_cat}/{clean_sch}/{clean_tbl}/data.parquet"
    escaped_uri = s3_uri.replace("'", "''")
    try:
        con.execute(
            f'COPY "{schema_slug}"."{table_slug}" TO \'{escaped_uri}\' (FORMAT PARQUET, OVERWRITE_OR_IGNORE TRUE)'
        )
    except Exception as exc:
        logger.debug(
            "DuckDB S3 COPY fallback (MinIO offline or unit test environment) for %s: %s",
            s3_uri,
            exc,
        )
    return s3_uri


def _sync_catalog_table_metadata(
    db: Session,
    workspace: Workspace,
    user: User,
    catalog_slug: str,
    schema_slug: str,
    table_slug: str,
    s3_uri: str | None,
    column_defs: list[dict[str, Any]],
    operation: str,
) -> None:
    """Synchronize DDL/DML table creation, alteration, or drop with Postgres CatalogTable metadata."""
    try:
        cat_slug = _slugify(catalog_slug or "main")
        sch_slug = _slugify(schema_slug or "default")
        tbl_slug = _slugify(table_slug)

        catalog = (
            db.query(Catalog)
            .filter(
                Catalog.owner_workspace_id == workspace.id,
                Catalog.deleted_at.is_(None),
            )
            .order_by(Catalog.is_default.desc(), Catalog.created_at)
            .first()
        )
        if not catalog:
            catalog = Catalog(
                id=uuid.uuid4(),
                name=catalog_slug or f"{workspace.name} Catalog",
                slug=f"{workspace.slug}_{cat_slug}",
                description="Auto-created catalog from SQL Statement Execution API",
                owner_workspace_id=workspace.id,
                created_by=user.id,
                is_default=True,
            )
            db.add(catalog)
            db.flush()

        schema_obj = db.query(CatalogSchema).filter_by(catalog_id=catalog.id, slug=sch_slug).first()
        if not schema_obj and operation != "DROP":
            tier = sch_slug if sch_slug in ("bronze", "silver", "gold") else "custom"
            schema_obj = CatalogSchema(
                id=uuid.uuid4(),
                catalog_id=catalog.id,
                name=schema_slug or "default",
                slug=sch_slug,
                tier=tier,
            )
            db.add(schema_obj)
            db.flush()

        if not schema_obj:
            return

        existing_table = (
            db.query(CatalogTable).filter_by(schema_id=schema_obj.id, slug=tbl_slug).first()
        )

        if operation == "DROP":
            if existing_table is not None:
                import catalog_trash_service

                catalog_trash_service.soft_delete_table(
                    db=db,
                    table=existing_table,
                    schema=schema_obj,
                    catalog=catalog,
                    workspace=workspace,
                    user_id=getattr(user, "id", None),
                )
                db.commit()
            return

        if existing_table is not None:
            if s3_uri:
                existing_table.s3_path_pattern = s3_uri
            if column_defs:
                existing_table.column_defs = column_defs
        else:
            new_table = CatalogTable(
                id=uuid.uuid4(),
                schema_id=schema_obj.id,
                name=table_slug,
                slug=tbl_slug,
                description=f"Created via SQL Statement Execution ({operation})",
                s3_path_pattern=s3_uri,
                column_defs=column_defs,
            )
            db.add(new_table)

        db.commit()
    except Exception as exc:
        logger.warning("Catalog metadata sync skipped or failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass


def _build_chunks_and_manifest(
    statement_id: str,
    columns: list[StatementColumnInfo],
    raw_rows: list[Any],
    row_limit: int,
    byte_limit: int,
    chunk_size: int,
    fmt: str,
) -> tuple[StatementManifest, list[StatementResultChunk]]:
    """Serialize rows, enforce row/byte limits, and partition into paginated chunks."""
    accepted_rows: list[list[Any]] = []
    row_byte_sizes: list[int] = []
    total_bytes = 0
    truncated = False

    for idx, raw_row in enumerate(raw_rows):
        if idx >= row_limit:
            truncated = True
            break
        serialized_row = [ai_service._serialize_cell(cell) for cell in raw_row]
        row_bytes = len(json.dumps(serialized_row, default=str).encode("utf-8"))
        if accepted_rows and (total_bytes + row_bytes) > byte_limit:
            truncated = True
            break
        accepted_rows.append(serialized_row)
        row_byte_sizes.append(row_bytes)
        total_bytes += row_bytes

    col_names = [c.name for c in columns]
    chunks: list[StatementResultChunk] = []
    chunk_infos: list[StatementChunkInfo] = []

    if not accepted_rows:
        # Always include chunk 0 even for empty result sets / DDL
        empty_chunk = _format_chunk(
            statement_id=statement_id,
            chunk_index=0,
            row_offset=0,
            rows_slice=[],
            col_names=col_names,
            fmt=fmt,
            has_next=False,
        )
        chunks.append(empty_chunk)
        chunk_infos.append(
            StatementChunkInfo(
                chunk_index=0,
                row_offset=0,
                row_count=0,
                byte_count=0,
            )
        )
    else:
        cur_rows: list[list[Any]] = []
        cur_bytes = 0
        cur_offset = 0

        raw_slices: list[tuple[int, list[list[Any]], int]] = []
        for r_idx, (row, r_bytes) in enumerate(zip(accepted_rows, row_byte_sizes, strict=False)):
            if cur_rows and (
                len(cur_rows) >= chunk_size or (cur_bytes + r_bytes) > DEFAULT_CHUNK_BYTE_LIMIT
            ):
                raw_slices.append((cur_offset, cur_rows, cur_bytes))
                cur_offset = r_idx
                cur_rows = []
                cur_bytes = 0
            cur_rows.append(row)
            cur_bytes += r_bytes

        if cur_rows:
            raw_slices.append((cur_offset, cur_rows, cur_bytes))

        total_slices = len(raw_slices)
        for c_idx, (offset, rows_slice, slice_bytes) in enumerate(raw_slices):
            has_next = (c_idx + 1) < total_slices
            chunk_obj = _format_chunk(
                statement_id=statement_id,
                chunk_index=c_idx,
                row_offset=offset,
                rows_slice=rows_slice,
                col_names=col_names,
                fmt=fmt,
                has_next=has_next,
            )
            chunks.append(chunk_obj)
            chunk_infos.append(
                StatementChunkInfo(
                    chunk_index=c_idx,
                    row_offset=offset,
                    row_count=len(rows_slice),
                    byte_count=slice_bytes,
                )
            )

    manifest = StatementManifest(
        format=fmt,
        schema=StatementSchemaInfo(column_count=len(columns), columns=columns),
        total_chunk_count=len(chunks),
        chunks=chunk_infos,
        total_row_count=len(accepted_rows),
        total_byte_count=total_bytes,
        truncated=truncated,
    )
    return manifest, chunks


def _format_chunk(
    statement_id: str,
    chunk_index: int,
    row_offset: int,
    rows_slice: list[list[Any]],
    col_names: list[str],
    fmt: str,
    has_next: bool,
) -> StatementResultChunk:
    next_idx = (chunk_index + 1) if has_next else None
    next_link = (
        f"/api/sql/statements/{statement_id}/result/chunks/{next_idx}"
        if next_idx is not None
        else None
    )

    if fmt == "JSON_OBJECT":
        objs = [dict(zip(col_names, row, strict=False)) for row in rows_slice]
        return StatementResultChunk(
            chunk_index=chunk_index,
            row_offset=row_offset,
            row_count=len(rows_slice),
            next_chunk_index=next_idx,
            next_chunk_internal_link=next_link,
            data_array=rows_slice,
            data_objects=objs,
        )

    if fmt == "CSV":
        buf = io.StringIO()
        writer = csv.writer(buf)
        if chunk_index == 0 and col_names:
            writer.writerow(col_names)
        for row in rows_slice:
            writer.writerow(["" if v is None else v for v in row])
        return StatementResultChunk(
            chunk_index=chunk_index,
            row_offset=row_offset,
            row_count=len(rows_slice),
            next_chunk_index=next_idx,
            next_chunk_internal_link=next_link,
            data_array=rows_slice,
            csv_data=buf.getvalue(),
        )

    return StatementResultChunk(
        chunk_index=chunk_index,
        row_offset=row_offset,
        row_count=len(rows_slice),
        next_chunk_index=next_idx,
        next_chunk_internal_link=next_link,
        data_array=rows_slice,
    )


def _execute_statement_on_duckdb(
    stored: StoredStatement,
    req: StatementExecutionRequest,
    catalogs: list[dict[str, Any]],
    workspace: Workspace,
    user: User,
    db: Session | None = None,
) -> None:
    """Execute the validated SQL statement in DuckDB, handling DDL/DML sync and chunking."""
    start_ts = time.perf_counter()
    con: duckdb.DuckDBPyConnection | None = None
    try:
        if stored.cancel_event.is_set():
            stored.status = StatementStatus(state="CANCELED")
            return

        con = ai_service.create_duckdb_connection(
            load_vss=False,
            register_ai=True,
        )
        stored.connection = con

        # Register workspace catalog views
        ai_service.register_catalog_views(con, catalogs)

        # Configure session schema context
        target_schema = ai_service._safe_identifier(req.schema_name or "default")
        if target_schema and target_schema.lower() != "main":
            try:
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{target_schema}"')
                con.execute(f'USE "{target_schema}"')
            except Exception:
                pass

        # Inspect for DDL/DML target table to materialize existing view before mutation
        op, ddl_schema, ddl_table = _extract_ddl_dml_target(
            ai_service.strip_catalog_qualifiers(req.statement, catalogs), target_schema
        )
        if ddl_schema and ddl_schema.lower() != "main":
            try:
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{ddl_schema}"')
            except Exception:
                pass

        if op in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP") and ddl_schema and ddl_table:
            existing_s3 = _find_catalog_table_s3_path(catalogs, ddl_schema, ddl_table)
            if existing_s3:
                esc_s3 = existing_s3.replace("'", "''")
                try:
                    con.execute(f'DROP VIEW IF EXISTS "{ddl_schema}"."{ddl_table}"')
                    if op != "DROP":
                        con.execute(
                            f'CREATE TABLE "{ddl_schema}"."{ddl_table}" AS '
                            f"SELECT * FROM read_parquet('{esc_s3}')"
                        )
                except Exception:
                    pass
        elif op == "CREATE" and ddl_schema and ddl_table:
            try:
                con.execute(f'DROP VIEW IF EXISTS "{ddl_schema}"."{ddl_table}"')
                con.execute(f'DROP VIEW IF EXISTS "{ddl_table}"')
            except Exception:
                pass

        if stored.cancel_event.is_set():
            stored.status = StatementStatus(state="CANCELED")
            return

        statement_sql = ai_service.strip_catalog_qualifiers(req.statement, catalogs)
        prepared_sql, bound_params = _prepare_parameters(statement_sql, req.parameters)
        if bound_params is not None:
            rel = con.execute(prepared_sql, bound_params)
        else:
            rel = con.execute(prepared_sql)

        if stored.cancel_event.is_set():
            stored.status = StatementStatus(state="CANCELED")
            return

        desc_list = rel.description or []
        columns = [
            StatementColumnInfo(
                name=str(d[0]),
                type_text=str(d[1]) if len(d) > 1 and d[1] is not None else "VARCHAR",
                type_name=str(d[1]) if len(d) > 1 and d[1] is not None else "VARCHAR",
                position=idx,
            )
            for idx, d in enumerate(desc_list)
        ]

        raw_rows = rel.fetchmany(req.row_limit + 1) if desc_list else []
        try:
            con.complete()
        except Exception:
            pass

        # If DDL/DML modified or created a managed table, export to workspace S3 & sync CatalogTable
        if op in ("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER") and ddl_schema and ddl_table:
            col_defs: list[dict[str, Any]] = []
            try:
                desc_rows = con.execute(f'DESCRIBE "{ddl_schema}"."{ddl_table}"').fetchall()
                col_defs = [
                    {
                        "name": str(r[0]),
                        "type": str(r[1]),
                        "nullable": str(r[2]).upper() != "NO" if len(r) > 2 else True,
                    }
                    for r in desc_rows
                ]
            except Exception:
                pass

            s3_uri = _export_table_to_workspace_s3(
                con=con,
                workspace_slug=workspace.slug,
                catalog_slug=req.catalog,
                schema_slug=ddl_schema,
                table_slug=ddl_table,
                bucket=DEFAULT_BUCKET,
            )
            if db is not None:
                _sync_catalog_table_metadata(
                    db=db,
                    workspace=workspace,
                    user=user,
                    catalog_slug=req.catalog,
                    schema_slug=ddl_schema,
                    table_slug=ddl_table,
                    s3_uri=s3_uri,
                    column_defs=col_defs,
                    operation=op,
                )
        elif op == "DROP" and ddl_schema and ddl_table and db is not None:
            _sync_catalog_table_metadata(
                db=db,
                workspace=workspace,
                user=user,
                catalog_slug=req.catalog,
                schema_slug=ddl_schema,
                table_slug=ddl_table,
                s3_uri=None,
                column_defs=[],
                operation="DROP",
            )

        manifest, chunks = _build_chunks_and_manifest(
            statement_id=stored.statement_id,
            columns=columns,
            raw_rows=raw_rows,
            row_limit=req.row_limit,
            byte_limit=req.byte_limit,
            chunk_size=req.chunk_size,
            fmt=req.format,
        )

        elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        stored.execution_ms = elapsed_ms

        if stored.cancel_event.is_set():
            stored.status = StatementStatus(state="CANCELED")
            return

        stored.manifest = manifest
        stored.chunks = chunks
        stored.status = StatementStatus(state="SUCCEEDED")

    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        stored.execution_ms = elapsed_ms
        if stored.cancel_event.is_set() or "interrupt" in str(exc).lower():
            stored.status = StatementStatus(state="CANCELED")
        else:
            stored.status = StatementStatus(
                state="FAILED",
                error=StatementErrorInfo(
                    error_code="SYNTAX_OR_EXECUTION_ERROR",
                    message=str(exc),
                ),
            )
    finally:
        stored.connection = None
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _to_execution_response(stored: StoredStatement) -> StatementExecutionResponse:
    first_chunk = stored.chunks[0] if stored.chunks else None
    return StatementExecutionResponse(
        statement_id=stored.statement_id,
        status=stored.status,
        manifest=stored.manifest,
        result=first_chunk if stored.status.state == "SUCCEEDED" else None,
        execution_ms=stored.execution_ms,
        workspace_slug=stored.workspace_slug,
        catalog=stored.catalog,
        schema=stored.schema_name,
        created_at=stored.created_at,
    )


def _assert_statement_workspace_access(stored: StoredStatement, ws: Workspace, user: User) -> None:
    is_superadmin = getattr(user, "role", None) == "superadmin"
    if is_superadmin:
        return
    if stored.workspace_id != str(ws.id) and stored.workspace_slug != ws.slug:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Statement belongs to a different workspace.",
        )


@router.post(
    "/statements",
    response_model=StatementExecutionResponse,
    response_model_exclude_none=False,
)
def execute_statement(
    req: StatementExecutionRequest,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_sql_context),
    db: Session = Depends(get_db),
) -> StatementExecutionResponse:
    """Execute a SQL query or DDL/DML statement with workspace isolation guardrails."""
    ws, role, user = ctx
    is_superadmin = getattr(user, "role", None) == "superadmin"

    cleaned_sql = (req.statement or "").strip()
    if not cleaned_sql:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SQL statement cannot be empty.",
        )

    # 1. Enforce workspace RBAC for DDL/DML write statements
    is_write = is_write_sql(cleaned_sql)
    if is_write and not is_superadmin and role not in ("write", "admin", "owner", "superadmin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Workspace viewer role is read-only. Write or admin role is required for DDL/DML statements.",
        )

    # 1b. Intercept UNDROP TABLE / UNDROP SCHEMA statements before DuckDB engine
    if cleaned_sql.lstrip().upper().startswith("UNDROP"):
        import catalog_trash_service

        is_undrop, msg = catalog_trash_service.try_handle_undrop_statement(
            sql=cleaned_sql,
            workspace=ws,
            user=user,
            default_catalog=req.catalog,
            default_schema=req.schema_name,
            db=db,
        )
        if is_undrop:
            statement_id = str(uuid.uuid4())
            is_err = (msg or "").startswith("Error:") or (
                (msg or "").startswith("UNDROP") and "failed:" in (msg or "")
            )
            if is_err:
                stored = StoredStatement(
                    statement_id=statement_id,
                    workspace_id=str(ws.id),
                    workspace_slug=ws.slug,
                    user_id=str(user.id),
                    statement=cleaned_sql,
                    catalog=req.catalog or "main",
                    schema_name=req.schema_name or "default",
                    warehouse_id=req.warehouse_id or "default",
                    format=req.format,
                    status=StatementStatus(
                        state="FAILED",
                        error=StatementErrorInfo(
                            error_code="EXECUTION_ERROR",
                            message=msg or "UNDROP failed",
                        ),
                    ),
                    execution_ms=5.0,
                )
            else:
                cols = [StatementColumnInfo(name="result", type_text="VARCHAR")]
                manifest, chunks = _build_chunks_and_manifest(
                    statement_id=statement_id,
                    columns=cols,
                    raw_rows=[[msg]],
                    row_limit=req.row_limit,
                    byte_limit=req.byte_limit,
                    chunk_size=req.chunk_size,
                    fmt=req.format,
                )
                stored = StoredStatement(
                    statement_id=statement_id,
                    workspace_id=str(ws.id),
                    workspace_slug=ws.slug,
                    user_id=str(user.id),
                    statement=cleaned_sql,
                    catalog=req.catalog or "main",
                    schema_name=req.schema_name or "default",
                    warehouse_id=req.warehouse_id or "default",
                    format=req.format,
                    status=StatementStatus(state="SUCCEEDED"),
                    manifest=manifest,
                    chunks=chunks,
                    execution_ms=10.0,
                )
            statement_store.put(stored)
            return _to_execution_response(stored)

    # 2. Enforce host filesystem & S3 workspace isolation guardrails
    catalogs = ai_service.get_workspace_catalogs_from_db(db, ws.id, user_id=user.id)
    allowed, reason = validate_statement_guardrails(
        sql=cleaned_sql,
        workspace_slug=ws.slug,
        catalogs=catalogs,
        is_superadmin=is_superadmin,
        bucket=DEFAULT_BUCKET,
    )
    if not allowed:
        code = (
            status.HTTP_400_BAD_REQUEST
            if reason and "Multi-statement" in reason
            else status.HTTP_403_FORBIDDEN
        )
        raise HTTPException(
            status_code=code,
            detail=reason or "Access Denied: SQL statement violates workspace guardrails.",
        )

    statement_id = str(uuid.uuid4())
    stored = StoredStatement(
        statement_id=statement_id,
        workspace_id=str(ws.id),
        workspace_slug=ws.slug,
        user_id=str(user.id),
        statement=cleaned_sql,
        catalog=req.catalog or "main",
        schema_name=req.schema_name or "default",
        warehouse_id=req.warehouse_id or "default",
        format=req.format,
        status=StatementStatus(state="RUNNING"),
    )
    statement_store.put(stored)

    # Support asynchronous execution when wait_timeout == "0s"
    if (req.wait_timeout or "").strip().lower() in ("0s", "0", "async"):

        def _bg_worker() -> None:
            bg_db = SessionLocal()
            try:
                _execute_statement_on_duckdb(
                    stored=stored,
                    req=req,
                    catalogs=catalogs,
                    workspace=ws,
                    user=user,
                    db=bg_db,
                )
            finally:
                bg_db.close()

        thread = threading.Thread(target=_bg_worker, daemon=True)
        thread.start()
        return _to_execution_response(stored)

    _execute_statement_on_duckdb(
        stored=stored,
        req=req,
        catalogs=catalogs,
        workspace=ws,
        user=user,
        db=db,
    )
    return _to_execution_response(stored)


@router.get("/statements", response_model=list[StatementExecutionResponse])
def list_statements(
    limit: int = Query(50, ge=1, le=200),
    state: str | None = Query(None),
    ctx: tuple[Workspace, str, User] = Depends(_resolve_sql_context),
) -> list[StatementExecutionResponse]:
    """List recent SQL statement executions in the active workspace."""
    ws, _, _ = ctx
    items = statement_store.list_for_workspace(
        workspace_id=str(ws.id),
        workspace_slug=ws.slug,
        limit=limit,
        state_filter=state,
    )
    return [_to_execution_response(item) for item in items]


@router.get(
    "/statements/{statement_id}",
    response_model=StatementExecutionResponse,
    response_model_exclude_none=False,
)
def get_statement(
    statement_id: str,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_sql_context),
) -> StatementExecutionResponse:
    """Get execution status, manifest, and chunk 0 for a statement."""
    ws, _, user = ctx
    stored = statement_store.get(statement_id)
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Statement '{statement_id}' not found.",
        )
    _assert_statement_workspace_access(stored, ws, user)
    return _to_execution_response(stored)


@router.get(
    "/statements/{statement_id}/result/chunks/{chunk_index}",
    response_model=StatementResultChunk,
    response_model_exclude_none=False,
)
def get_statement_result_chunk(
    statement_id: str,
    chunk_index: int,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_sql_context),
) -> StatementResultChunk:
    """Retrieve a specific paginated result chunk for a succeeded statement."""
    ws, _, user = ctx
    stored = statement_store.get(statement_id)
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Statement '{statement_id}' not found.",
        )
    _assert_statement_workspace_access(stored, ws, user)

    if stored.status.state != "SUCCEEDED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Statement '{statement_id}' is in state '{stored.status.state}', results unavailable.",
        )

    if chunk_index < 0 or chunk_index >= len(stored.chunks):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Chunk index {chunk_index} out of range (total chunks: {len(stored.chunks)}).",
        )

    return stored.chunks[chunk_index]


@router.post("/statements/{statement_id}/cancel")
def cancel_statement(
    statement_id: str,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_sql_context),
) -> dict[str, Any]:
    """Safely cancel a running DuckDB statement execution."""
    ws, _, user = ctx
    stored = statement_store.get(statement_id)
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Statement '{statement_id}' not found.",
        )
    _assert_statement_workspace_access(stored, ws, user)

    stored.cancel_event.set()
    con = stored.connection
    if con is not None:
        try:
            con.interrupt()
        except Exception as exc:
            logger.debug("DuckDB interrupt returned: %s", exc)

    if stored.status.state in ("PENDING", "RUNNING"):
        stored.status = StatementStatus(state="CANCELED")

    return {
        "statement_id": stored.statement_id,
        "status": stored.status.model_dump(),
    }
