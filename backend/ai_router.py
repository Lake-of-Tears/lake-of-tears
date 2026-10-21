from __future__ import annotations

import logging
from typing import Any

import ai_service
from auth import get_ai_workspace_and_role, get_current_user
from database import get_db
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, status
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

logger = logging.getLogger("lake.backend.ai.router")

router = APIRouter()


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
async def get_ai_context(
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
async def execute_sql(
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
async def generate_sql(
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
async def fix_sql(
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
async def explain_sql(
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
async def genie_chat(
    req: GenieChatRequest,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_ai_context),
    db: Session = Depends(get_db),
) -> GenieChatResponse:
    """Run the multi-turn Genie conversational data analyst loop."""
    ws, _, _ = ctx
    catalogs = ai_service.get_workspace_catalogs_from_db(db, ws.id)
    history_dicts = [m.model_dump() for m in req.history]
    result = ai_service.run_genie_chat(
        question=req.question,
        mode=req.mode,
        history=history_dicts,
        catalogs=catalogs,
        model=req.model,
    )
    return GenieChatResponse(**result)
