from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

import ai_service
from auth import get_ai_workspace_and_role, get_current_user
from database import get_db
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from models import User, Workspace
from schemas import (
    GenieChatRequest,
    GenieChatResponse,
    SqlExecuteRequest,
    SqlExecuteResponse,
    SqlExplainRequest,
    SqlExplainResponse,
    SqlFixRequest,
    SqlFixResponse,
    SqlGenerateRequest,
    SqlGenerateResponse,
)
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("lake.backend.ai.router")

router = APIRouter()

_CANCEL_LOCK = threading.Lock()
_GENIE_CANCEL_EVENTS: dict[str, threading.Event] = {}


def _resolve_ai_context(
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[Workspace, str, User]:
    slug = workspace_slug or x_workspace_slug
    ws_id = x_workspace_id or lake_active_workspace or lake_workspace_id
    ws, role = get_ai_workspace_and_role(
        workspace_id=ws_id,
        workspace_slug=slug,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    return ws, role, current_user


@router.get("/context")
def get_ai_context(
    ctx: tuple[Workspace, str, User] = Depends(_resolve_ai_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return active workspace Catalog schemas, default S3 datasets, and SQL AI function metadata."""
    ws, role, _ = ctx
    catalogs = ai_service.get_workspace_catalogs_from_db(db, ws.id)
    return {
        "ok": True,
        "workspace_id": str(ws.id),
        "workspace_slug": ws.slug,
        "workspace_name": ws.name,
        "role": role,
        "catalogs": catalogs,
        "default_datasets": ai_service.DEFAULT_LAKEHOUSE_DATASETS,
        "sql_ai_functions": ai_service.SQL_AI_FUNCTIONS,
    }


@router.post("/sql/execute", response_model=SqlExecuteResponse)
def execute_sql(
    req: SqlExecuteRequest,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_ai_context),
    db: Session = Depends(get_db),
) -> SqlExecuteResponse:
    """Execute a DuckDB SQL query with workspace Catalog views and SQL AI UDFs registered."""
    ws, _, user = ctx
    is_superadmin = getattr(user, "role", None) == "superadmin"
    catalogs = ai_service.get_workspace_catalogs_from_db(db, ws.id)
    result = ai_service.execute_sql_query(
        sql=req.sql,
        catalogs=catalogs,
        read_only=req.read_only,
        max_ai_rows=req.max_ai_rows,
        workspace_slug=ws.slug,
        is_superadmin=is_superadmin,
    )
    if not result.get("ok") and result.get("error_code") == 403:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=result.get("error") or "Access Denied: S3 path violates workspace boundaries.",
        )
    return SqlExecuteResponse(**result)


@router.post("/sql/generate", response_model=SqlGenerateResponse)
def generate_sql(
    req: SqlGenerateRequest,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_ai_context),
    db: Session = Depends(get_db),
) -> SqlGenerateResponse:
    """Generate executable DuckDB SQL from natural language using workspace Catalog schemas."""
    ws, _, _ = ctx
    catalogs = ai_service.get_workspace_catalogs_from_db(db, ws.id)
    result = ai_service.generate_sql_from_prompt(
        prompt=req.prompt,
        catalogs=catalogs,
        current_sql=req.current_sql,
        model=req.model,
    )
    return SqlGenerateResponse(**result)


@router.post("/sql/fix", response_model=SqlFixResponse)
def fix_sql(
    req: SqlFixRequest,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_ai_context),
    db: Session = Depends(get_db),
) -> SqlFixResponse:
    """Fix a broken DuckDB SQL query using the error message and workspace Catalog schemas."""
    ws, _, _ = ctx
    catalogs = ai_service.get_workspace_catalogs_from_db(db, ws.id)
    result = ai_service.fix_sql_query(
        sql=req.sql,
        error_message=req.error,
        catalogs=catalogs,
        model=req.model,
    )
    return SqlFixResponse(**result)


@router.post("/sql/explain", response_model=SqlExplainResponse)
def explain_sql(
    req: SqlExplainRequest,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_ai_context),
    db: Session = Depends(get_db),
) -> SqlExplainResponse:
    """Explain a DuckDB SQL query step-by-step."""
    ws, _, _ = ctx
    catalogs = ai_service.get_workspace_catalogs_from_db(db, ws.id)
    result = ai_service.explain_sql_query(
        sql=req.sql,
        catalogs=catalogs,
        model=req.model,
    )
    return SqlExplainResponse(**result)


@router.post("/chat", response_model=GenieChatResponse)
def genie_chat(
    req: GenieChatRequest,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_ai_context),
    db: Session = Depends(get_db),
) -> GenieChatResponse:
    """Run the multi-turn Genie conversational data analyst loop."""
    ws, _, user = ctx
    target_ws = ws
    if req.workspace_slug and req.workspace_slug != ws.slug:
        target_ws, _ = get_ai_workspace_and_role(
            workspace_slug=req.workspace_slug,
            current_user=user,
            db=db,
            require_write=False,
        )
    elif req.workspace_id and str(ws.id) != req.workspace_id:
        target_ws, _ = get_ai_workspace_and_role(
            workspace_id=req.workspace_id,
            current_user=user,
            db=db,
            require_write=False,
        )

    is_superadmin = getattr(user, "role", None) == "superadmin"
    catalogs = ai_service.get_workspace_catalogs_from_db(db, target_ws.id)
    history_dicts = [m.model_dump() for m in req.history]

    cancel_event = threading.Event()
    if req.request_id:
        with _CANCEL_LOCK:
            _GENIE_CANCEL_EVENTS[req.request_id] = cancel_event

    try:
        result = ai_service.run_genie_chat(
            question=req.question,
            mode=req.mode,
            history=history_dicts,
            catalogs=catalogs,
            model=req.model,
            cancel=cancel_event,
            workspace_slug=target_ws.slug,
            is_superadmin=is_superadmin,
        )
    finally:
        if req.request_id:
            with _CANCEL_LOCK:
                _GENIE_CANCEL_EVENTS.pop(req.request_id, None)

    return GenieChatResponse(**result)


@router.post("/chat/stream")
async def genie_chat_stream(
    req: GenieChatRequest,
    request: Request,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_ai_context),
    db: Session = Depends(get_db),
):
    """Stream thinking steps and final results for the Genie conversational loop via SSE."""
    ws, _, user = ctx
    target_ws = ws
    if req.workspace_slug and req.workspace_slug != ws.slug:
        target_ws, _ = get_ai_workspace_and_role(
            workspace_slug=req.workspace_slug,
            current_user=user,
            db=db,
            require_write=False,
        )
    elif req.workspace_id and str(ws.id) != req.workspace_id:
        target_ws, _ = get_ai_workspace_and_role(
            workspace_id=req.workspace_id,
            current_user=user,
            db=db,
            require_write=False,
        )

    is_superadmin = getattr(user, "role", None) == "superadmin"
    catalogs = ai_service.get_workspace_catalogs_from_db(db, target_ws.id)
    history_dicts = [m.model_dump() for m in req.history]

    cancel_event = threading.Event()
    req_id = req.request_id or ""
    if req_id:
        with _CANCEL_LOCK:
            _GENIE_CANCEL_EVENTS[req_id] = cancel_event

    loop = asyncio.get_running_loop()
    event_queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    def on_step(evt: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, ("step", evt))

    async def _worker() -> None:
        try:
            res = await run_in_threadpool(
                ai_service.run_genie_chat,
                question=req.question,
                mode=req.mode,
                history=history_dicts,
                catalogs=catalogs,
                model=req.model,
                cancel=cancel_event,
                workspace_slug=target_ws.slug,
                is_superadmin=is_superadmin,
                progress_callback=on_step,
            )
            await event_queue.put(("result", res))
        except Exception as exc:
            await event_queue.put(("error", {"ok": False, "mode": req.mode, "error": str(exc)}))
        finally:
            await event_queue.put(None)

    worker_task = asyncio.create_task(_worker())

    async def _watch_disconnect() -> None:
        while not worker_task.done():
            if await request.is_disconnected():
                cancel_event.set()
                return
            await asyncio.sleep(0.5)

    watcher_task = asyncio.create_task(_watch_disconnect())

    async def sse_generator():
        try:
            while True:
                item = await event_queue.get()
                if item is None:
                    break
                event_name, data = item
                yield f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        finally:
            watcher_task.cancel()
            if not worker_task.done():
                cancel_event.set()
            if req_id:
                with _CANCEL_LOCK:
                    _GENIE_CANCEL_EVENTS.pop(req_id, None)

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/cancel")
def cancel_genie_chat(
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Handle cancellation requests for Genie conversational AI queries."""
    req_id = str((body or {}).get("request_id") or "")
    if req_id:
        with _CANCEL_LOCK:
            event = _GENIE_CANCEL_EVENTS.get(req_id)
            if event:
                event.set()
    return {"ok": True, "cancelled": True}
