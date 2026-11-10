"""Lakeset BI Studio Router (`/api/v1/lakeset` and `/api/lakeset`).

Provides:
- `GET /status`: Lakeset DuckDB Lakehouse connection & workspace sync status.
- `GET /datasets`: RBAC-scoped workspace & shared catalog datasets with column descriptions.
- `POST /sync`: Automated DuckDB + MinIO S3 database registration & semantic dataset sync.
- `POST /guest-token`: Short-lived Superset Guest Token minting with per-workspace RLS.
- `POST /explore-data`: Interactive Chart Builder series aggregation & SQL preview.
"""

from __future__ import annotations

import logging
from typing import Any

from auth import get_current_user, get_dashboard_workspace_and_role
from database import get_db
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
)
from models import User, Workspace, WorkspaceDashboard
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from superset_service import (
    _LAST_SYNC_METADATA,
    LAKESET_DB_NAME,
    get_authorized_workspace_tables,
    headless_superset,
    mint_lakeset_guest_token,
    sync_workspace_to_lakeset,
)

logger = logging.getLogger("lake.backend.lakeset.router")

router = APIRouter()


class LakesetSyncRequest(BaseModel):
    catalog_slug: str | None = None
    schema_slug: str | None = None
    force: bool = True


class LakesetGuestTokenRequest(BaseModel):
    dataset: str | None = None
    dashboard_id: str | None = None
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class LakesetExploreRequest(BaseModel):
    dataset: str | None = None
    sql: str | None = None
    chart_type: str = "bar"
    dimension: str | None = None
    metric: str | None = "count"
    metric_agg: str = "count"
    limit: int = Field(default=50, ge=1, le=500)


def _resolve_lakeset_context(
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
    ws, role = get_dashboard_workspace_and_role(
        workspace_id=ws_id,
        workspace_slug=slug,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    return ws, role, current_user


@router.get("/status")
def get_lakeset_status(
    ctx: tuple[Workspace, str, User] = Depends(_resolve_lakeset_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return Lakeset BI Studio health, DuckDB Lakehouse status, and workspace dataset counts."""
    ws, role, user = ctx
    datasets = get_authorized_workspace_tables(db=db, workspace=ws, user=user)
    synced_count = sum(1 for d in datasets if d.get("synced"))
    dashboards_count = (
        db.query(WorkspaceDashboard).filter(WorkspaceDashboard.workspace_id == ws.id).count()
    )
    last_sync = _LAST_SYNC_METADATA.get(str(ws.id), {})

    return {
        "ok": True,
        "superset_online": headless_superset.is_healthy(),
        "database_name": LAKESET_DB_NAME,
        "engine": "duckdb",
        "workspace_id": str(ws.id),
        "workspace_slug": ws.slug,
        "workspace_name": ws.name,
        "role": role,
        "user_email": user.email,
        "datasets_count": len(datasets),
        "synced_datasets_count": synced_count,
        "dashboards_count": dashboards_count,
        "last_synced_at": last_sync.get("synced_at"),
    }


@router.get("/datasets")
def list_lakeset_datasets(
    search: str | None = Query(None),
    schema: str | None = Query(None),
    tier: str | None = Query(None),
    catalog: str | None = Query(None),
    ctx: tuple[Workspace, str, User] = Depends(_resolve_lakeset_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List governed catalog datasets authorized for the caller in the active workspace."""
    ws, role, user = ctx
    datasets = get_authorized_workspace_tables(db=db, workspace=ws, user=user)

    if catalog:
        c_filter = catalog.strip().lower()
        datasets = [
            d
            for d in datasets
            if d["catalog_slug"].lower() == c_filter or d["catalog_name"].lower() == c_filter
        ]
    if schema:
        s_filter = schema.strip().lower()
        datasets = [
            d
            for d in datasets
            if d["schema_slug"].lower() == s_filter or d["schema_name"].lower() == s_filter
        ]
    if tier:
        t_filter = tier.strip().lower()
        datasets = [d for d in datasets if (d.get("tier") or "").lower() == t_filter]
    if search:
        q = search.strip().lower()
        datasets = [
            d
            for d in datasets
            if q in d["full_name"].lower()
            or q in d["display_name"].lower()
            or q in (d.get("description") or "").lower()
            or any(
                q in c["column_name"].lower() or q in (c.get("description") or "").lower()
                for c in d.get("columns", [])
            )
        ]

    return {
        "ok": True,
        "workspace_id": str(ws.id),
        "workspace_slug": ws.slug,
        "role": role,
        "database_name": LAKESET_DB_NAME,
        "total": len(datasets),
        "datasets": datasets,
    }


@router.post("/sync")
def trigger_lakeset_sync(
    req: LakesetSyncRequest | None = None,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_lakeset_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Register the DuckDB + MinIO S3 database in Superset and sync workspace catalog tables."""
    ws, _, user = ctx
    result = sync_workspace_to_lakeset(db=db, workspace=ws, user=user)

    if req and (req.catalog_slug or req.schema_slug):
        filtered = result["datasets"]
        if req.catalog_slug:
            filtered = [
                d for d in filtered if d["catalog_slug"].lower() == req.catalog_slug.strip().lower()
            ]
        if req.schema_slug:
            filtered = [
                d for d in filtered if d["schema_slug"].lower() == req.schema_slug.strip().lower()
            ]
        result["datasets"] = filtered
        result["synced_datasets_count"] = len(filtered)

    return result


@router.post("/guest-token")
def create_lakeset_guest_token(
    req: LakesetGuestTokenRequest | None = None,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_lakeset_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Mint a short-lived Superset Guest Token scoped to the caller's authorized workspace datasets."""
    ws, _, user = ctx
    authorized_datasets = get_authorized_workspace_tables(db=db, workspace=ws, user=user)
    payload = req or LakesetGuestTokenRequest()

    try:
        token_bundle = mint_lakeset_guest_token(
            user=user,
            workspace=ws,
            authorized_datasets=authorized_datasets,
            requested_dataset=payload.dataset,
            requested_dashboard_id=payload.dashboard_id,
            ttl_seconds=payload.ttl_seconds,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return {
        "ok": True,
        **token_bundle,
    }


@router.post("/explore-data")
def explore_lakeset_dataset(
    req: LakesetExploreRequest,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_lakeset_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Generate analytical chart series and SQL preview for a governed dataset in Chart Builder."""
    ws, _, user = ctx
    authorized_datasets = get_authorized_workspace_tables(db=db, workspace=ws, user=user)

    matched_ds: dict[str, Any] | None = None
    if req.dataset:
        target = req.dataset.strip().lower()
        for d in authorized_datasets:
            if (
                d["full_name"].lower() == target
                or d["table_name"].lower() == target
                or d["sql_view"].lower() == target
                or str(d["id"]).lower() == target
            ):
                matched_ds = d
                break
        if not matched_ds:
            raise HTTPException(
                status_code=403,
                detail=f"Dataset '{req.dataset}' is not authorized in workspace '{ws.slug}'.",
            )
    elif authorized_datasets:
        matched_ds = authorized_datasets[0]

    cols = matched_ds.get("columns", []) if matched_ds else []
    dim = req.dimension
    if not dim and cols:
        dim = cols[0]["column_name"]
    dim = dim or "category"

    metric_col = req.metric or "count"
    agg = (req.metric_agg or "count").upper()
    view_name = matched_ds["sql_view"] if matched_ds else "lakehouse.events"

    if req.sql and req.sql.strip():
        generated_sql = req.sql.strip()
    elif metric_col == "count" or agg == "COUNT":
        generated_sql = (
            f'SELECT "{dim}" AS "{dim}", COUNT(*) AS "count" '
            f"FROM {view_name} "
            f'GROUP BY "{dim}" ORDER BY "count" DESC LIMIT {req.limit}'
        )
    else:
        generated_sql = (
            f'SELECT "{dim}" AS "{dim}", {agg}("{metric_col}") AS "{metric_col}" '
            f"FROM {view_name} "
            f'GROUP BY "{dim}" ORDER BY "{metric_col}" DESC LIMIT {req.limit}'
        )

    widget_data = headless_superset.execute_widget_query(
        {
            "type": req.chart_type or "bar",
            "dimension": dim,
            "metric": metric_col,
        }
    )

    return {
        "ok": True,
        "dataset": matched_ds["full_name"] if matched_ds else (req.dataset or "custom_sql"),
        "chart_type": req.chart_type,
        "dimension": dim,
        "metric": metric_col,
        "sql": generated_sql,
        "data": widget_data,
    }
