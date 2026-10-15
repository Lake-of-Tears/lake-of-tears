from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
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
    status,
)
from models import User, Workspace, WorkspaceDashboard
from schemas import (
    CreateDashboardRequest,
    DashboardResponse,
    DashboardStatsResponse,
    GuestTokenResponse,
    LinkDashboardRequest,
    UpdateDashboardRequest,
)
from sqlalchemy.orm import Session
from superset_service import (
    SUPERSET_PUBLIC_URL,
    slugify,
    superset_gateway,
)

logger = logging.getLogger("lake.backend.dashboards.router")

router = APIRouter()


# ── Dependency Context Resolution ─────────────────────────────────────────────


def _resolve_dashboard_context(
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str, User]:
    slug = workspace_slug or x_workspace_slug
    ws_id = x_workspace_id or lake_active_workspace
    ws, role = get_dashboard_workspace_and_role(
        workspace_id=ws_id,
        workspace_slug=slug,
        current_user=current_user,
        db=db,
        require_write=require_write,
    )
    return ws, role, current_user


def _dashboard_write_context(
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[Workspace, str, User]:
    return _resolve_dashboard_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )


def _unique_slug(
    title: str, workspace_id: uuid.UUID, db: Session, exclude_id: uuid.UUID | None = None
) -> str:
    base = slugify(title) or "dashboard"
    slug = base
    counter = 1
    while True:
        query = db.query(WorkspaceDashboard).filter(
            WorkspaceDashboard.workspace_id == workspace_id,
            WorkspaceDashboard.slug == slug,
        )
        if exclude_id:
            query = query.filter(WorkspaceDashboard.id != exclude_id)
        if not query.first():
            return slug
        slug = f"{base}-{counter}"
        counter += 1


def _to_response(dash: WorkspaceDashboard) -> DashboardResponse:
    superset_url = f"{SUPERSET_PUBLIC_URL}/superset/dashboard/{dash.superset_dashboard_id}/"
    now = datetime.now(UTC)
    return DashboardResponse(
        id=dash.id or uuid.uuid4(),
        workspace_id=dash.workspace_id,
        superset_dashboard_id=dash.superset_dashboard_id,
        embedded_uuid=dash.embedded_uuid,
        title=dash.title,
        slug=dash.slug,
        description=dash.description,
        charts_count=dash.charts_count or 0,
        is_published=dash.is_published,
        created_by=dash.created_by,
        created_at=dash.created_at or now,
        updated_at=dash.updated_at or now,
        superset_url=superset_url,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/stats", response_model=DashboardStatsResponse)
async def get_dashboard_stats(
    ctx: tuple[Workspace, str, User] = Depends(_resolve_dashboard_context),
    db: Session = Depends(get_db),
) -> DashboardStatsResponse:
    """Get aggregated dashboard metrics for the current workspace."""
    ws, _, _ = ctx
    dashboards = db.query(WorkspaceDashboard).filter(WorkspaceDashboard.workspace_id == ws.id).all()
    total = len(dashboards)
    published = sum(1 for d in dashboards if d.is_published)
    total_charts = sum(d.charts_count for d in dashboards)
    is_healthy = superset_gateway.is_healthy()

    return DashboardStatsResponse(
        total_dashboards=total,
        published_dashboards=published,
        total_charts=total_charts,
        superset_online=is_healthy,
    )


@router.get("", response_model=list[DashboardResponse])
async def list_dashboards(
    ctx: tuple[Workspace, str, User] = Depends(_resolve_dashboard_context),
    db: Session = Depends(get_db),
) -> list[DashboardResponse]:
    """List all dashboards registered in the current workspace."""
    ws, _, _ = ctx
    dashboards = (
        db.query(WorkspaceDashboard)
        .filter(WorkspaceDashboard.workspace_id == ws.id)
        .order_by(WorkspaceDashboard.created_at.desc())
        .all()
    )
    return [_to_response(d) for d in dashboards]


@router.get("/superset-available")
async def list_available_superset_dashboards(
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List all raw Superset dashboards and indicate which are already linked in this workspace."""
    ws, _, _ = ctx
    linked_superset_ids = {
        row[0]
        for row in db.query(WorkspaceDashboard.superset_dashboard_id)
        .filter(WorkspaceDashboard.workspace_id == ws.id)
        .all()
    }
    raw_dashboards = superset_gateway.list_superset_dashboards()
    result = []
    for d in raw_dashboards:
        sid = d.get("id")
        result.append(
            {
                "id": sid,
                "title": d.get("dashboard_title") or f"Dashboard #{sid}",
                "slug": d.get("slug"),
                "published": d.get("published", True),
                "is_linked": sid in linked_superset_ids,
            }
        )
    return result


@router.post("", response_model=DashboardResponse, status_code=status.HTTP_201_CREATED)
async def create_dashboard(
    req: CreateDashboardRequest,
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Create a new blank dashboard in Superset and link it to the current workspace."""
    ws, _, user = ctx

    superset_id, embed_uuid = superset_gateway.create_dashboard(
        title=req.title,
        description=req.description,
        is_published=req.is_published,
    )

    slug = _unique_slug(req.title, ws.id, db)
    now = datetime.now(UTC)
    dash = WorkspaceDashboard(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        superset_dashboard_id=superset_id,
        embedded_uuid=embed_uuid,
        title=req.title,
        slug=slug,
        description=req.description,
        charts_count=0,
        is_published=req.is_published,
        created_by=user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(dash)
    db.commit()
    db.refresh(dash)
    return _to_response(dash)


@router.post("/link", response_model=DashboardResponse, status_code=status.HTTP_201_CREATED)
async def link_dashboard(
    req: LinkDashboardRequest,
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Link an existing Superset dashboard to the current workspace."""
    ws, _, user = ctx

    # Check if already linked
    existing = (
        db.query(WorkspaceDashboard)
        .filter(
            WorkspaceDashboard.workspace_id == ws.id,
            WorkspaceDashboard.superset_dashboard_id == req.superset_dashboard_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Dashboard #{req.superset_dashboard_id} is already linked to this workspace.",
        )

    # Get dashboard info from Superset if title not provided
    title = req.title
    charts_count = 0
    superset_info = superset_gateway.get_dashboard(req.superset_dashboard_id)
    if superset_info:
        if not title:
            title = superset_info.get("dashboard_title")
        positions = superset_info.get("position_json")
        if positions and isinstance(positions, str):
            charts_count = positions.count('"type": "CHART"') or positions.count('"CHART-')

    if not title:
        title = f"Superset Dashboard #{req.superset_dashboard_id}"

    embed_uuid = superset_gateway.get_or_create_embedded_config(req.superset_dashboard_id)
    slug = _unique_slug(title, ws.id, db)
    now = datetime.now(UTC)

    dash = WorkspaceDashboard(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        superset_dashboard_id=req.superset_dashboard_id,
        embedded_uuid=embed_uuid,
        title=title,
        slug=slug,
        description=req.description,
        charts_count=charts_count,
        is_published=req.is_published,
        created_by=user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(dash)
    db.commit()
    db.refresh(dash)
    return _to_response(dash)


@router.get("/{dashboard_id}", response_model=DashboardResponse)
async def get_dashboard(
    dashboard_id: str,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_dashboard_context),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Get metadata for a specific workspace dashboard by ID or slug."""
    ws, _, _ = ctx
    dash = None
    try:
        dash_uuid = uuid.UUID(dashboard_id)
        dash = (
            db.query(WorkspaceDashboard)
            .filter(WorkspaceDashboard.id == dash_uuid, WorkspaceDashboard.workspace_id == ws.id)
            .first()
        )
    except ValueError:
        dash = (
            db.query(WorkspaceDashboard)
            .filter(
                WorkspaceDashboard.slug == dashboard_id, WorkspaceDashboard.workspace_id == ws.id
            )
            .first()
        )

    if not dash:
        raise HTTPException(status_code=404, detail="Dashboard not found in this workspace.")

    return _to_response(dash)


@router.get("/{dashboard_id}/guest-token", response_model=GuestTokenResponse)
async def get_dashboard_guest_token(
    dashboard_id: str,
    ctx: tuple[Workspace, str, User] = Depends(_resolve_dashboard_context),
    db: Session = Depends(get_db),
) -> GuestTokenResponse:
    """Mint a guest token for the embedded Superset dashboard."""
    ws, _, user = ctx
    dash = None
    try:
        dash_uuid = uuid.UUID(dashboard_id)
        dash = (
            db.query(WorkspaceDashboard)
            .filter(WorkspaceDashboard.id == dash_uuid, WorkspaceDashboard.workspace_id == ws.id)
            .first()
        )
    except ValueError:
        dash = (
            db.query(WorkspaceDashboard)
            .filter(
                WorkspaceDashboard.slug == dashboard_id, WorkspaceDashboard.workspace_id == ws.id
            )
            .first()
        )

    if not dash:
        raise HTTPException(status_code=404, detail="Dashboard not found in this workspace.")

    # Mint official Superset Guest Token scoped to the embedded UUID
    guest_token = superset_gateway.mint_guest_token(
        user=user,
        embedded_uuid=dash.embedded_uuid,
    )

    return GuestTokenResponse(
        token=guest_token,
        embedded_uuid=dash.embedded_uuid,
        dashboard_id=dash.id,
        superset_dashboard_id=dash.superset_dashboard_id,
        superset_domain=SUPERSET_PUBLIC_URL,
    )


@router.patch("/{dashboard_id}", response_model=DashboardResponse)
async def update_dashboard(
    dashboard_id: str,
    req: UpdateDashboardRequest,
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Update dashboard title, description, or published status."""
    ws, _, _ = ctx
    dash = None
    try:
        dash_uuid = uuid.UUID(dashboard_id)
        dash = (
            db.query(WorkspaceDashboard)
            .filter(WorkspaceDashboard.id == dash_uuid, WorkspaceDashboard.workspace_id == ws.id)
            .first()
        )
    except ValueError:
        dash = (
            db.query(WorkspaceDashboard)
            .filter(
                WorkspaceDashboard.slug == dashboard_id, WorkspaceDashboard.workspace_id == ws.id
            )
            .first()
        )

    if not dash:
        raise HTTPException(status_code=404, detail="Dashboard not found in this workspace.")

    if req.title is not None and req.title.strip():
        dash.title = req.title.strip()
        dash.slug = _unique_slug(dash.title, ws.id, db, exclude_id=dash.id)
    if req.description is not None:
        dash.description = req.description
    if req.is_published is not None:
        dash.is_published = req.is_published

    db.commit()
    db.refresh(dash)
    return _to_response(dash)


@router.delete("/{dashboard_id}")
async def delete_dashboard(
    dashboard_id: str,
    purge_superset: bool = Query(False, description="Permanently delete from Superset"),
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Unlink dashboard from workspace, optionally purging from Superset."""
    ws, _, _ = ctx
    dash = None
    try:
        dash_uuid = uuid.UUID(dashboard_id)
        dash = (
            db.query(WorkspaceDashboard)
            .filter(WorkspaceDashboard.id == dash_uuid, WorkspaceDashboard.workspace_id == ws.id)
            .first()
        )
    except ValueError:
        dash = (
            db.query(WorkspaceDashboard)
            .filter(
                WorkspaceDashboard.slug == dashboard_id, WorkspaceDashboard.workspace_id == ws.id
            )
            .first()
        )

    if not dash:
        raise HTTPException(status_code=404, detail="Dashboard not found in this workspace.")

    superset_id = dash.superset_dashboard_id
    db.delete(dash)
    db.commit()

    purged = False
    if purge_superset:
        purged = superset_gateway.delete_dashboard(superset_id)

    return {
        "ok": True,
        "message": f"Dashboard unlinked from workspace '{ws.slug}'.",
        "purged_from_superset": purged,
    }
