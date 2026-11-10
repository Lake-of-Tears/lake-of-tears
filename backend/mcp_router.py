from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import ai_service
from auth import get_current_user, get_user_accessible_workspaces, is_effective_superadmin
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from models import CatalogTable, User
from openai_compat_router import execute_vector_retrieve_request
from schemas import VectorRetrieveRequest
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.mcp")

router = APIRouter()

SUPPORTED_MCP_VERSIONS = ("2025-03-26", "2024-11-05")
DEFAULT_MCP_VERSION = "2024-11-05"

MCP_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "list_workspaces_and_catalogs",
        "description": (
            "List all Lake of Tears workspaces, catalogs, schemas, and tables that the "
            "authenticated user is authorized to access, including column counts and vector index availability."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_slug": {
                    "type": "string",
                    "description": "Optional workspace slug to filter results to a single workspace.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "describe_table",
        "description": (
            "Describe a specific Lake of Tears catalog table, returning its column definitions, "
            "semantic business descriptions, vector index status, and optional sample rows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "catalog": {
                    "type": "string",
                    "description": "Catalog slug or name (e.g. 'training' or 'main').",
                },
                "schema": {
                    "type": "string",
                    "description": "Schema slug or name (e.g. 'bronze', 'silver', 'gold').",
                },
                "table": {
                    "type": "string",
                    "description": "Table slug or name.",
                },
                "workspace_slug": {
                    "type": "string",
                    "description": "Optional workspace slug context.",
                },
                "include_sample": {
                    "type": "boolean",
                    "description": "Whether to fetch up to 5 sample rows from the table (default: false).",
                    "default": False,
                },
            },
            "required": ["catalog", "schema", "table"],
            "additionalProperties": False,
        },
    },
    {
        "name": "semantic_search_datalake",
        "description": (
            "Execute permission-aware semantic vector similarity search across authorized Lake of Tears "
            "catalog tables and workspace embeddings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of chunks to return (1..50, default 5).",
                    "default": 5,
                },
                "workspace_slug": {
                    "type": "string",
                    "description": "Optional workspace slug to scope the vector search.",
                },
                "catalog": {
                    "type": "string",
                    "description": "Optional catalog slug or name filter.",
                },
                "schema": {
                    "type": "string",
                    "description": "Optional schema slug or name filter.",
                },
                "table": {
                    "type": "string",
                    "description": "Optional table slug or name filter.",
                },
                "score_threshold": {
                    "type": "number",
                    "description": "Optional minimum cosine similarity score threshold (0.0..1.0).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_datalake_sql",
        "description": (
            "Execute a read-only DuckDB SQL query (SELECT, WITH, DESCRIBE, SUMMARIZE, SHOW) "
            "against the user's authorized Lake of Tears catalog tables and views."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Read-only DuckDB SQL SELECT query.",
                },
                "workspace_slug": {
                    "type": "string",
                    "description": "Optional workspace slug context.",
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Maximum number of rows to return (default 100, max 1000).",
                    "default": 100,
                },
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ask_genie",
        "description": (
            "Ask the Lake of Tears Genie AI analyst a natural language question (Text-to-SQL "
            "synthesis + DuckDB execution + narrative insight, or semantic RAG)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Natural language analytical question.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["genie", "rag", "hybrid"],
                    "description": "Execution mode: 'genie' (Text-to-SQL), 'rag' (Vector RAG), or 'hybrid'.",
                    "default": "genie",
                },
                "workspace_slug": {
                    "type": "string",
                    "description": "Optional workspace slug to scope the query.",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
]


def _jsonrpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result,
    }


def _jsonrpc_error(
    req_id: Any,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": err,
    }


def _tool_success(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, default=str),
            }
        ],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_error(message: str, status_code: int = 400) -> dict[str, Any]:
    payload = {
        "ok": False,
        "error": message,
        "status_code": status_code,
    }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, default=str),
            }
        ],
        "structuredContent": payload,
        "isError": True,
    }


def _sanitize_catalog_for_mcp(cat: dict[str, Any]) -> dict[str, Any]:
    """Return a clean, RBAC-safe representation of a catalog without internal storage credentials."""
    schemas_out: list[dict[str, Any]] = []
    for sch in cat.get("schemas") or []:
        tables_out: list[dict[str, Any]] = []
        for tbl in sch.get("tables") or []:
            vec_cfg = tbl.get("vector_index") or {}
            has_vec = isinstance(vec_cfg, dict) and bool(
                (vec_cfg.get("s3_vector_path") or "").strip()
                or vec_cfg.get("enabled")
                or vec_cfg.get("status") in ("ready", "synced")
            )
            cols = tbl.get("column_defs") or []
            tables_out.append(
                {
                    "name": tbl.get("name"),
                    "slug": tbl.get("slug"),
                    "description": tbl.get("description"),
                    "column_count": len(cols),
                    "columns": [
                        {
                            "name": c.get("name"),
                            "type": c.get("type", "VARCHAR"),
                            "description": c.get("description"),
                        }
                        for c in cols
                        if isinstance(c, dict) and c.get("name")
                    ],
                    "has_vector_index": has_vec,
                }
            )
        schemas_out.append(
            {
                "name": sch.get("name"),
                "slug": sch.get("slug"),
                "tier": sch.get("tier"),
                "description": sch.get("description"),
                "tables": tables_out,
            }
        )
    return {
        "name": cat.get("name"),
        "slug": cat.get("slug"),
        "description": cat.get("description"),
        "workspace_slug": cat.get("owner_workspace_slug"),
        "schemas": schemas_out,
    }


def _handle_resources_list(db: Session, current_user: User) -> dict[str, Any]:
    is_super = is_effective_superadmin(current_user)
    workspaces = get_user_accessible_workspaces(db, current_user)
    resources: list[dict[str, Any]] = []
    seen_uris: set[str] = set()

    for ws in workspaces:
        cats = ai_service.get_workspace_catalogs_from_db(
            db,
            ws.id,
            user_id=None if is_super else current_user.id,
        )
        for cat in cats:
            cat_slug = str(cat.get("slug") or cat.get("name") or ws.slug)
            uri = f"lot://catalogs/{ws.slug}/{cat_slug}"
            if uri in seen_uris:
                continue
            seen_uris.add(uri)
            resources.append(
                {
                    "uri": uri,
                    "name": f"{ws.slug}/{cat_slug}",
                    "description": cat.get("description")
                    or f"Catalog '{cat_slug}' in workspace '{ws.slug}'",
                    "mimeType": "application/json",
                }
            )
    return {"resources": resources}


def _handle_resources_read(
    db: Session,
    current_user: User,
    params: dict[str, Any],
) -> dict[str, Any]:
    uri = str(params.get("uri") or "").strip()
    if not uri.startswith("lot://catalogs/"):
        raise HTTPException(status_code=400, detail=f"Unsupported resource URI: {uri}")
    rest = uri[len("lot://catalogs/") :]
    parts = [p.strip() for p in rest.split("/") if p.strip()]
    if len(parts) < 2:
        raise HTTPException(
            status_code=400,
            detail="Resource URI must match lot://catalogs/{workspace_slug}/{catalog_slug}",
        )
    ws_slug, cat_slug = parts[0], parts[1]
    scope = ai_service.resolve_user_ai_scope(
        db=db,
        user=current_user,
        workspace_slug=ws_slug,
        catalog_filter=cat_slug,
    )
    if not scope["catalogs"]:
        raise HTTPException(
            status_code=404,
            detail=f"Catalog '{cat_slug}' not found in workspace '{ws_slug}'",
        )
    sanitized = _sanitize_catalog_for_mcp(scope["catalogs"][0])
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(sanitized, indent=2, default=str),
            }
        ]
    }


def _execute_mcp_tool(
    db: Session,
    current_user: User,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        if tool_name == "list_workspaces_and_catalogs":
            ws_slug = (arguments.get("workspace_slug") or "").strip() or None
            scope = ai_service.resolve_user_ai_scope(
                db=db,
                user=current_user,
                workspace_slug=ws_slug,
            )
            workspaces_out = [
                {
                    "id": str(w.id),
                    "slug": w.slug,
                    "name": w.name,
                }
                for w in scope["target_workspaces"]
            ]
            catalogs_out = [_sanitize_catalog_for_mcp(c) for c in scope["catalogs"]]
            return _tool_success(
                {
                    "ok": True,
                    "workspaces": workspaces_out,
                    "catalogs": catalogs_out,
                }
            )

        if tool_name == "describe_table":
            cat_name = str(arguments.get("catalog") or "").strip()
            sch_name = str(arguments.get("schema") or "").strip()
            tbl_name = str(arguments.get("table") or "").strip()
            ws_slug = (arguments.get("workspace_slug") or "").strip() or None
            include_sample = bool(arguments.get("include_sample", False))

            if not (cat_name and sch_name and tbl_name):
                return _tool_error("catalog, schema, and table are required.", status_code=400)

            scope = ai_service.resolve_user_ai_scope(
                db=db,
                user=current_user,
                workspace_slug=ws_slug,
                catalog_filter=cat_name,
                schema_filter=sch_name,
                table_filter=tbl_name,
            )

            matched_tbl: dict[str, Any] | None = None
            matched_cat: dict[str, Any] | None = None
            matched_sch: dict[str, Any] | None = None
            for cat in scope["catalogs"]:
                for sch in cat.get("schemas") or []:
                    if (sch.get("slug") or "").lower() not in (sch_name.lower(),) and (
                        sch.get("name") or ""
                    ).lower() != sch_name.lower():
                        continue
                    for tbl in sch.get("tables") or []:
                        if (tbl.get("slug") or "").lower() == tbl_name.lower() or (
                            tbl.get("name") or ""
                        ).lower() == tbl_name.lower():
                            matched_tbl = tbl
                            matched_cat = cat
                            matched_sch = sch
                            break

            if matched_tbl is None:
                exists_in_db = (
                    db.query(CatalogTable)
                    .filter((CatalogTable.slug == tbl_name) | (CatalogTable.name == tbl_name))
                    .first()
                )
                if exists_in_db and not scope["is_superadmin"]:
                    return _tool_error(
                        f"Forbidden: You do not have permission to access table '{cat_name}.{sch_name}.{tbl_name}'.",
                        status_code=403,
                    )
                return _tool_error(
                    f"Table '{cat_name}.{sch_name}.{tbl_name}' not found.",
                    status_code=404,
                )

            sample_rows: list[list[Any]] = []
            sample_columns: list[str] = []
            if include_sample and matched_tbl.get("s3_path_pattern"):
                s3_pat = str(matched_tbl["s3_path_pattern"]).replace("'", "''")
                sample_res = ai_service.execute_sql_query(
                    sql=f"SELECT * FROM read_parquet('{s3_pat}') LIMIT 5",
                    catalogs=scope["catalogs"],
                    read_only=True,
                    workspace_slug=scope["primary_workspace"].slug,
                    is_superadmin=scope["is_superadmin"],
                )
                if sample_res.get("ok"):
                    sample_columns = sample_res.get("columns", [])
                    sample_rows = sample_res.get("rows", [])

            vec_cfg = matched_tbl.get("vector_index") or {}
            has_vec = isinstance(vec_cfg, dict) and bool(
                (vec_cfg.get("s3_vector_path") or "").strip()
                or vec_cfg.get("enabled")
                or vec_cfg.get("status") in ("ready", "synced")
            )
            return _tool_success(
                {
                    "ok": True,
                    "workspace_slug": (matched_cat or {}).get("owner_workspace_slug")
                    or scope["primary_workspace"].slug,
                    "catalog": (matched_cat or {}).get("slug") or cat_name,
                    "schema": (matched_sch or {}).get("slug") or sch_name,
                    "table": matched_tbl.get("slug") or tbl_name,
                    "description": matched_tbl.get("description"),
                    "columns": matched_tbl.get("column_defs") or [],
                    "has_vector_index": has_vec,
                    "sample_columns": sample_columns,
                    "sample_rows": sample_rows,
                }
            )

        if tool_name == "semantic_search_datalake":
            query_str = str(arguments.get("query") or "").strip()
            if not query_str:
                return _tool_error("query is required.", status_code=400)
            retrieve_req = VectorRetrieveRequest(
                query=query_str,
                top_k=int(arguments.get("top_k") or 5),
                score_threshold=(
                    float(arguments["score_threshold"])
                    if arguments.get("score_threshold") is not None
                    else None
                ),
                workspace_slug=(arguments.get("workspace_slug") or "").strip() or None,
                catalog=(arguments.get("catalog") or "").strip() or None,
                schema_name=(arguments.get("schema") or "").strip() or None,
                table=(arguments.get("table") or "").strip() or None,
            )
            resp = execute_vector_retrieve_request(
                req=retrieve_req,
                current_user=current_user,
                db=db,
            )
            return _tool_success(resp.model_dump())

        if tool_name == "query_datalake_sql":
            raw_sql = str(arguments.get("sql") or "").strip()
            if not raw_sql:
                return _tool_error("sql is required.", status_code=400)
            ws_slug = (arguments.get("workspace_slug") or "").strip() or None
            max_rows = max(1, min(int(arguments.get("max_rows") or 100), 1000))

            scope = ai_service.resolve_user_ai_scope(
                db=db,
                user=current_user,
                workspace_slug=ws_slug,
            )
            normalized_sql = ai_service.strip_catalog_qualifiers(raw_sql, scope["catalogs"])
            exec_res = ai_service.execute_sql_query(
                sql=normalized_sql,
                catalogs=scope["catalogs"],
                read_only=True,
                workspace_slug=scope["primary_workspace"].slug,
                is_superadmin=scope["is_superadmin"],
            )
            if not exec_res.get("ok"):
                err_code = int(exec_res.get("error_code") or 400)
                return _tool_error(
                    exec_res.get("error") or "SQL execution failed",
                    status_code=err_code,
                )
            rows = (exec_res.get("rows") or [])[:max_rows]
            return _tool_success(
                {
                    "ok": True,
                    "workspace_slug": scope["primary_workspace"].slug,
                    "columns": exec_res.get("columns", []),
                    "rows": rows,
                    "row_count": len(rows),
                    "total_row_count": exec_res.get("row_count", len(rows)),
                    "execution_ms": exec_res.get("execution_ms", 0.0),
                }
            )

        if tool_name == "ask_genie":
            question = str(arguments.get("question") or "").strip()
            if not question:
                return _tool_error("question is required.", status_code=400)
            mode = str(arguments.get("mode") or "genie").strip().lower()
            if mode not in ("genie", "rag", "hybrid"):
                mode = "genie"
            ws_slug = (arguments.get("workspace_slug") or "").strip() or None

            scope = ai_service.resolve_user_ai_scope(
                db=db,
                user=current_user,
                workspace_slug=ws_slug,
            )
            genie_res = ai_service.run_genie_chat(
                question=question,
                mode=mode,
                catalogs=scope["catalogs"],
                workspace_slug=scope["primary_workspace"].slug,
                allowed_patterns=scope["allowed_patterns"],
                is_superadmin=scope["is_superadmin"],
                include_workspace_pattern=bool(ws_slug),
            )
            if not genie_res.get("ok"):
                return _tool_error(
                    genie_res.get("error") or "Genie query failed",
                    status_code=502,
                )
            return _tool_success(
                {
                    "ok": True,
                    "mode": genie_res.get("mode", mode),
                    "workspace_slug": scope["primary_workspace"].slug,
                    "answer": genie_res.get("answer", ""),
                    "sql": genie_res.get("sql"),
                    "columns": genie_res.get("columns", []),
                    "rows": genie_res.get("rows", []),
                    "row_count": genie_res.get("row_count", 0),
                    "snippets": genie_res.get("snippets", []),
                    "tables_used": genie_res.get("tables_used", []),
                }
            )

        return _tool_error(f"Unknown MCP tool: '{tool_name}'", status_code=404)
    except HTTPException as exc:
        return _tool_error(str(exc.detail), status_code=exc.status_code)
    except Exception as exc:
        logger.exception("MCP tool '%s' execution error", tool_name)
        return _tool_error(f"Tool execution error: {exc}", status_code=500)


def _process_jsonrpc_message(
    msg: dict[str, Any],
    db: Session,
    current_user: User,
) -> dict[str, Any] | None:
    """Process a single MCP JSON-RPC 2.0 request or notification."""
    method = str(msg.get("method") or "").strip()
    req_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    # Notifications (no id or notifications/*)
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        if req_id is None:
            return None
        return _jsonrpc_result(req_id, {})

    if method == "initialize":
        requested_ver = str(params.get("protocolVersion") or DEFAULT_MCP_VERSION)
        negotiated_ver = (
            requested_ver if requested_ver in SUPPORTED_MCP_VERSIONS else DEFAULT_MCP_VERSION
        )
        return _jsonrpc_result(
            req_id,
            {
                "protocolVersion": negotiated_ver,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {
                    "name": "lake-of-tears-mcp",
                    "version": "1.0.0",
                },
            },
        )

    if method == "ping":
        return _jsonrpc_result(req_id, {})

    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": MCP_TOOLS_SPEC})

    if method == "tools/call":
        tool_name = str(params.get("name") or "").strip()
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not tool_name:
            return _jsonrpc_error(req_id, -32602, "Invalid params: 'name' is required")
        result = _execute_mcp_tool(db, current_user, tool_name, arguments)
        return _jsonrpc_result(req_id, result)

    if method == "resources/list":
        return _jsonrpc_result(req_id, _handle_resources_list(db, current_user))

    if method == "resources/read":
        try:
            res = _handle_resources_read(db, current_user, params)
            return _jsonrpc_result(req_id, res)
        except HTTPException as exc:
            err_code = -32003 if exc.status_code == 403 else -32002
            return _jsonrpc_error(
                req_id,
                err_code,
                str(exc.detail),
                data={"status_code": exc.status_code},
            )

    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


@router.post("")
@router.post("/")
@router.post("/messages")
async def mcp_jsonrpc_endpoint(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """MCP Streamable HTTP & SSE message endpoint accepting JSON-RPC 2.0 requests and batches."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            _jsonrpc_error(None, -32700, "Parse error: invalid JSON body"),
            status_code=400,
        )

    if isinstance(payload, list):
        responses = []
        for item in payload:
            if isinstance(item, dict):
                resp = _process_jsonrpc_message(item, db, current_user)
                if resp is not None:
                    responses.append(resp)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)

    if isinstance(payload, dict):
        resp = _process_jsonrpc_message(payload, db, current_user)
        if resp is None:
            return Response(status_code=202)
        return JSONResponse(resp)

    return JSONResponse(
        _jsonrpc_error(None, -32600, "Invalid Request"),
        status_code=400,
    )


@router.get("/sse")
@router.get("")
@router.get("/")
async def mcp_sse_or_info_endpoint(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response:
    """MCP SSE transport handshake endpoint (`GET /api/mcp/sse`) and server discovery endpoint."""
    accept = (request.headers.get("accept") or "").lower()
    is_sse = request.url.path.endswith("/sse") or "text/event-stream" in accept

    base_path = request.url.path.rstrip("/")
    if base_path.endswith("/sse"):
        messages_path = base_path[:-4] + "/messages"
    else:
        messages_path = base_path + "/messages"

    if is_sse:

        async def _sse_stream():
            yield f"event: endpoint\ndata: {messages_path}\n\n"
            try:
                while not await request.is_disconnected():
                    await asyncio.sleep(15.0)
                    yield ": keepalive\n\n"
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            _sse_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return JSONResponse(
        {
            "name": "lake-of-tears-mcp",
            "version": "1.0.0",
            "protocolVersions": list(SUPPORTED_MCP_VERSIONS),
            "endpoints": {
                "streamable_http": base_path or "/api/mcp",
                "sse": f"{base_path}/sse" if not base_path.endswith("/sse") else base_path,
                "messages": messages_path,
            },
            "tools": [t["name"] for t in MCP_TOOLS_SPEC],
        }
    )
