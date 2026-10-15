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
    UpdateDashboardRequest,
    WidgetConfig,
    WidgetDataResponse,
)
from sqlalchemy.orm import Session
from superset_service import (
    TEMPLATES,
    headless_superset,
    slugify,
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
    now = datetime.now(UTC)
    layout = list(dash.layout_config) if dash.layout_config else []
    return DashboardResponse(
        id=dash.id or uuid.uuid4(),
        workspace_id=dash.workspace_id,
        title=dash.title,
        slug=dash.slug,
        description=dash.description,
        is_published=dash.is_published,
        layout_config=layout,
        charts_count=len(layout) if layout else (dash.charts_count or 0),
        created_by=dash.created_by,
        created_at=dash.created_at or now,
        updated_at=dash.updated_at or now,
    )


def _find_dashboard(dashboard_id: str, workspace_id: uuid.UUID, db: Session) -> WorkspaceDashboard:
    dash = None
    try:
        dash_uuid = uuid.UUID(dashboard_id)
        dash = (
            db.query(WorkspaceDashboard)
            .filter(
                WorkspaceDashboard.id == dash_uuid, WorkspaceDashboard.workspace_id == workspace_id
            )
            .first()
        )
    except ValueError:
        dash = (
            db.query(WorkspaceDashboard)
            .filter(
                WorkspaceDashboard.slug == dashboard_id,
                WorkspaceDashboard.workspace_id == workspace_id,
            )
            .first()
        )

    if not dash:
        raise HTTPException(status_code=404, detail="Dashboard not found in this workspace.")
    return dash


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/stats", response_model=DashboardStatsResponse)
async def get_dashboard_stats(
    ctx: tuple[Workspace, str, User] = Depends(_resolve_dashboard_context),
    db: Session = Depends(get_db),
) -> DashboardStatsResponse:
    """Get aggregated metrics for workspace native dashboards."""
    ws, _, _ = ctx
    dashboards = db.query(WorkspaceDashboard).filter(WorkspaceDashboard.workspace_id == ws.id).all()
    total = len(dashboards)
    published = sum(1 for d in dashboards if d.is_published)
    total_charts = sum(len(d.layout_config or []) for d in dashboards)
    is_healthy = headless_superset.is_healthy()

    return DashboardStatsResponse(
        total_dashboards=total,
        published_dashboards=published,
        total_charts=total_charts,
        superset_online=is_healthy,
    )


@router.get("/datasets")
async def list_available_datasets(
    ctx: tuple[Workspace, str, User] = Depends(_resolve_dashboard_context),
) -> list[dict[str, Any]]:
    """List analytical datasets available for dashboard widget creation."""
    return headless_superset.get_datasets()


@router.get("", response_model=list[DashboardResponse])
async def list_dashboards(
    ctx: tuple[Workspace, str, User] = Depends(_resolve_dashboard_context),
    db: Session = Depends(get_db),
) -> list[DashboardResponse]:
    """List all dashboards in the current workspace."""
    ws, _, _ = ctx
    dashboards = (
        db.query(WorkspaceDashboard)
        .filter(WorkspaceDashboard.workspace_id == ws.id)
        .order_by(WorkspaceDashboard.created_at.desc())
        .all()
    )
    return [_to_response(d) for d in dashboards]


@router.post("", response_model=DashboardResponse, status_code=status.HTTP_201_CREATED)
async def create_dashboard(
    req: CreateDashboardRequest,
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Create a new native dashboard in the workspace, optionally populated with template widgets."""
    ws, _, user = ctx

    slug = _unique_slug(req.title, ws.id, db)
    now = datetime.now(UTC)

    # Initialize layout from template if specified
    template_key = (req.template or "blank").lower().strip()
    initial_layout = [dict(w) for w in TEMPLATES.get(template_key, [])]

    dash = WorkspaceDashboard(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        title=req.title,
        slug=slug,
        description=req.description,
        is_published=req.is_published,
        layout_config=initial_layout,
        charts_count=len(initial_layout),
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
    """Get dashboard details and full native widget layout."""
    ws, _, _ = ctx
    dash = _find_dashboard(dashboard_id, ws.id, db)
    return _to_response(dash)


@router.patch("/{dashboard_id}", response_model=DashboardResponse)
async def update_dashboard(
    dashboard_id: str,
    req: UpdateDashboardRequest,
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Update dashboard title, description, published status, or full layout_config."""
    ws, _, _ = ctx
    dash = _find_dashboard(dashboard_id, ws.id, db)

    if req.title is not None and req.title.strip():
        dash.title = req.title.strip()
        dash.slug = _unique_slug(dash.title, ws.id, db, exclude_id=dash.id)
    if req.description is not None:
        dash.description = req.description
    if req.is_published is not None:
        dash.is_published = req.is_published
    if req.layout_config is not None:
        dash.layout_config = req.layout_config
        dash.charts_count = len(req.layout_config)

    dash.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(dash)
    return _to_response(dash)


@router.post(
    "/{dashboard_id}/widgets", response_model=DashboardResponse, status_code=status.HTTP_201_CREATED
)
async def add_widget(
    dashboard_id: str,
    widget: WidgetConfig,
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Add a new visual widget to the dashboard canvas."""
    ws, _, _ = ctx
    dash = _find_dashboard(dashboard_id, ws.id, db)

    layout = list(dash.layout_config or [])
    # Generate unique widget ID if not provided
    w_dict = widget.model_dump()
    if not w_dict.get("id"):
        w_dict["id"] = f"w_{uuid.uuid4().hex[:8]}"

    layout.append(w_dict)
    dash.layout_config = layout
    dash.charts_count = len(layout)
    dash.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(dash)
    return _to_response(dash)


@router.patch("/{dashboard_id}/widgets/{widget_id}", response_model=DashboardResponse)
async def update_widget(
    dashboard_id: str,
    widget_id: str,
    widget_update: dict[str, Any],
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Update settings for a specific widget on the dashboard canvas."""
    ws, _, _ = ctx
    dash = _find_dashboard(dashboard_id, ws.id, db)

    layout = list(dash.layout_config or [])
    found = False
    for i, w in enumerate(layout):
        if w.get("id") == widget_id:
            layout[i] = {**w, **widget_update, "id": widget_id}
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail="Widget not found in dashboard.")

    dash.layout_config = layout
    dash.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(dash)
    return _to_response(dash)


@router.delete("/{dashboard_id}/widgets/{widget_id}", response_model=DashboardResponse)
async def delete_widget(
    dashboard_id: str,
    widget_id: str,
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    """Remove a visual widget from the dashboard canvas."""
    ws, _, _ = ctx
    dash = _find_dashboard(dashboard_id, ws.id, db)

    layout = [w for w in (dash.layout_config or []) if w.get("id") != widget_id]
    dash.layout_config = layout
    dash.charts_count = len(layout)
    dash.updated_at = datetime.now(UTC)

    db.commit()
    db.refresh(dash)
    return _to_response(dash)


@router.get("/{dashboard_id}/widgets/{widget_id}/data", response_model=WidgetDataResponse)
async def get_widget_data(
    dashboard_id: str,
    widget_id: str,
    time_range: str | None = Query(None),
    ctx: tuple[Workspace, str, User] = Depends(_resolve_dashboard_context),
    db: Session = Depends(get_db),
) -> WidgetDataResponse:
    """Execute headless analytical query and return chart-ready series data."""
    ws, _, _ = ctx
    dash = _find_dashboard(dashboard_id, ws.id, db)

    target_widget = None
    for w in dash.layout_config or []:
        if w.get("id") == widget_id:
            target_widget = dict(w)
            break

    if not target_widget:
        raise HTTPException(status_code=404, detail="Widget not found in dashboard.")

    if time_range:
        target_widget["time_range"] = time_range

    data = headless_superset.execute_widget_query(target_widget)
    return WidgetDataResponse(
        widget_id=widget_id,
        type=target_widget.get("type", "kpi"),
        title=target_widget.get("title", "Widget"),
        data=data,
    )


@router.delete("/{dashboard_id}")
async def delete_dashboard(
    dashboard_id: str,
    ctx: tuple[Workspace, str, User] = Depends(_dashboard_write_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Delete dashboard from workspace."""
    ws, _, _ = ctx
    dash = _find_dashboard(dashboard_id, ws.id, db)

    db.delete(dash)
    db.commit()

    return {
        "ok": True,
        "message": f"Dashboard '{dash.title}' deleted from workspace '{ws.slug}'.",
    }
