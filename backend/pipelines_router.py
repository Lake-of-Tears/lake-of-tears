from __future__ import annotations

import logging
from typing import Any

import pipeline_service
from auth import get_current_user, get_pipeline_workspace_and_role
from database import get_db
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    Query,
    status,
)
from models import User
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.pipelines.router")

router = APIRouter()


# ── Pydantic Request Schemas ─────────────────────────────────────────────────


class PauseUpdateRequest(BaseModel):
    is_paused: bool


class PipelineScheduleRequest(BaseModel):
    schedule_interval: str


class TriggerPipelineRequest(BaseModel):
    conf: dict[str, Any] | None = Field(default_factory=dict)


class ScheduleNotebookRequest(BaseModel):
    notebook_path: str
    schedule_interval: str
    pipeline_title: str | None = None
    parameters: dict[str, Any] | None = Field(default_factory=dict)
    compute_resources: dict[str, str] | None = None


class PipelineSettingsRequest(BaseModel):
    color_hex: str | None = None
    display_label: str | None = None
    description: str | None = None
    is_visible_on_timeline: bool | None = None


# ── Dependency Context Resolution ─────────────────────────────────────────────


def _resolve_workspace_context(
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Any, str]:
    slug = workspace_slug or x_workspace_slug
    ws_id = x_workspace_id or lake_active_workspace
    return get_pipeline_workspace_and_role(
        workspace_id=ws_id,
        workspace_slug=slug,
        current_user=current_user,
        db=db,
        require_write=require_write,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("")
@router.get("/")
async def list_pipelines(
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List pipelines visible to the current workspace with pause status and last/next runs."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    is_superadmin = current_user.role == "superadmin"
    pipelines = await pipeline_service.list_workspace_dags(
        workspace_slug=ws.slug,
        is_superadmin=is_superadmin,
        workspace_id=ws.id,
        db=db,
    )
    return {
        "workspace": ws.slug,
        "total": len(pipelines),
        "pipelines": pipelines,
    }


@router.post("/schedule-notebook", status_code=status.HTTP_201_CREATED)
async def schedule_notebook_pipeline(
    req: ScheduleNotebookRequest,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or update a recurring notebook schedule DAG."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    is_superadmin = current_user.role == "superadmin"
    return await pipeline_service.schedule_notebook(
        workspace_slug=ws.slug,
        notebook_path=req.notebook_path,
        schedule_interval=req.schedule_interval,
        pipeline_title=req.pipeline_title,
        parameters=req.parameters,
        compute_resources=req.compute_resources,
        is_superadmin=is_superadmin,
        workspace_id=ws.id,
        user_id=current_user.id,
        db=db,
    )


@router.delete("/schedule-notebook/{dag_id}")
@router.delete("/{dag_id}")
async def delete_pipeline(
    dag_id: str,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a workspace pipeline DAG."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    is_superadmin = current_user.role == "superadmin"
    return await pipeline_service.delete_scheduled_notebook(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        is_superadmin=is_superadmin,
        workspace_id=ws.id,
        db=db,
    )


delete_scheduled_notebook_pipeline = delete_pipeline


@router.get("/timeline")
async def get_pipeline_timeline(
    date: str | None = Query(None, description="Calendar date YYYY-MM-DD"),
    tz: str = Query("UTC", description="IANA timezone name"),
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve 24-hour visual schedule timeline for the selected calendar day."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    is_superadmin = current_user.role == "superadmin"
    return await pipeline_service.get_workspace_timeline(
        workspace_slug=ws.slug,
        workspace_id=ws.id,
        target_date_str=date,
        tz_str=tz,
        is_superadmin=is_superadmin,
        db=db,
    )


@router.get("/{dag_id}/settings")
async def get_pipeline_settings(
    dag_id: str,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get color and label display settings for a pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    meta_map = pipeline_service.get_pipeline_metadata_map(ws.id, db)
    meta = meta_map.get(dag_id)
    return {
        "dag_id": dag_id,
        "color_hex": meta.color_hex if meta else pipeline_service.get_default_color_for_dag(dag_id),
        "display_label": meta.display_label
        if meta and meta.display_label
        else dag_id.replace("_", " ").title(),
        "description": meta.description if meta and meta.description is not None else "",
        "is_visible_on_timeline": meta.is_visible_on_timeline if meta else True,
        "palette": pipeline_service.DEFAULT_PALETTE,
    }


@router.patch("/{dag_id}/settings")
async def update_pipeline_settings(
    dag_id: str,
    req: PipelineSettingsRequest,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update workspace-shared pipeline color, label, and description metadata."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    return pipeline_service.upsert_pipeline_metadata(
        workspace_id=ws.id,
        dag_id=dag_id,
        color_hex=req.color_hex,
        display_label=req.display_label,
        description=req.description,
        is_visible_on_timeline=req.is_visible_on_timeline,
        user_id=current_user.id,
        db=db,
    )


@router.patch("/{dag_id}/schedule")
async def update_pipeline_schedule(
    dag_id: str,
    req: PipelineScheduleRequest,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update execution schedule for a pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    is_superadmin = current_user.role == "superadmin"
    return await pipeline_service.update_pipeline_schedule(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        schedule_interval=req.schedule_interval,
        is_superadmin=is_superadmin,
        role=role,
    )


@router.get("/{dag_id}")
async def get_pipeline(
    dag_id: str,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get metadata for a single pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    is_superadmin = current_user.role == "superadmin"
    return await pipeline_service.get_dag_details(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        is_superadmin=is_superadmin,
        workspace_id=ws.id,
        db=db,
    )


@router.patch("/{dag_id}")
async def update_pipeline_pause(
    dag_id: str,
    req: PauseUpdateRequest,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pause or unpause a pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    is_superadmin = current_user.role == "superadmin"
    return await pipeline_service.set_dag_paused(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        is_paused=req.is_paused,
        is_superadmin=is_superadmin,
    )


@router.post("/{dag_id}/trigger", status_code=status.HTTP_201_CREATED)
async def trigger_pipeline(
    dag_id: str,
    req: TriggerPipelineRequest | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Trigger on-demand execution of a workspace pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    is_superadmin = current_user.role == "superadmin"
    conf = req.conf if req else {}
    return await pipeline_service.trigger_dag_run(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        conf=conf,
        triggered_by=current_user.email,
        is_superadmin=is_superadmin,
    )


@router.get("/{dag_id}/runs")
async def get_pipeline_runs(
    dag_id: str,
    limit: int = Query(25, ge=1, le=100),
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List execution run history for a pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    is_superadmin = current_user.role == "superadmin"
    runs = await pipeline_service.list_dag_runs(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        limit=limit,
        is_superadmin=is_superadmin,
    )
    return {
        "dag_id": dag_id,
        "workspace": ws.slug,
        "total": len(runs),
        "runs": runs,
    }


@router.get("/{dag_id}/runs/{run_id}")
async def get_pipeline_run(
    dag_id: str,
    run_id: str,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get single DAG run details."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    is_superadmin = current_user.role == "superadmin"
    return await pipeline_service.get_dag_run(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        run_id=run_id,
        is_superadmin=is_superadmin,
    )


@router.get("/{dag_id}/runs/{run_id}/tasks")
async def get_pipeline_tasks(
    dag_id: str,
    run_id: str,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get task instances for a specific execution run."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    is_superadmin = current_user.role == "superadmin"
    tasks = await pipeline_service.list_task_instances(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        run_id=run_id,
        is_superadmin=is_superadmin,
    )
    return {
        "dag_id": dag_id,
        "run_id": run_id,
        "tasks": tasks,
    }


@router.get("/{dag_id}/runs/{run_id}/tasks/{task_id}/logs")
async def get_pipeline_task_logs(
    dag_id: str,
    run_id: str,
    task_id: str,
    try_number: int | None = Query(None),
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fetch task execution logs."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=False,
    )
    is_superadmin = current_user.role == "superadmin"
    return await pipeline_service.get_task_log(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        run_id=run_id,
        task_id=task_id,
        try_number=try_number,
        is_superadmin=is_superadmin,
    )


@router.post("/{dag_id}/runs/{run_id}/cancel")
async def cancel_pipeline_run(
    dag_id: str,
    run_id: str,
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel / stop an active or queued execution run."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
    is_superadmin = current_user.role == "superadmin"
    return await pipeline_service.cancel_dag_run(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        run_id=run_id,
        is_superadmin=is_superadmin,
    )
