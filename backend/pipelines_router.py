from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from typing import Any

import pipeline_service
from auth import get_current_user, get_pipeline_workspace_and_role
from database import get_db
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from models import User, generate_webhook_secret
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from webhooks_router import SENSITIVE_HEADERS, _resolve_workspace_or_404, _verify_webhook_auth

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
    webhook_enabled: bool | None = None


# ── Dependency Context Resolution ─────────────────────────────────────────────


def _resolve_workspace_context(
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Any, str]:
    slug = workspace_slug or x_workspace_slug
    ws_id = x_workspace_id or lake_active_workspace or lake_workspace_id
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List pipelines visible to the current workspace with pause status and last/next runs."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or update a recurring notebook schedule DAG."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a workspace pipeline DAG."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve 24-hour visual schedule timeline for the selected calendar day."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get color and label display settings for a pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
        "webhook_enabled": bool(meta.webhook_enabled) if meta else False,
        "webhook_url": f"/api/v1/pipelines/{ws.slug}/{dag_id}/webhook",
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update workspace-shared pipeline color, label, and description metadata."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
        webhook_enabled=req.webhook_enabled,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update execution schedule for a pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get metadata for a single pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pause or unpause a pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Trigger on-demand execution of a workspace pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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


@router.api_route(
    "/{workspace_slug}/{dag_id}/webhook",
    methods=["GET", "POST"],
    status_code=status.HTTP_201_CREATED,
)
@router.api_route(
    "/{dag_id}/webhook",
    methods=["GET", "POST"],
    status_code=status.HTTP_201_CREATED,
)
async def handle_pipeline_webhook(
    request: Request,
    dag_id: str,
    workspace_slug: str | None = None,
    validation_token: str | None = Query(None, alias="validationToken"),
    token: str | None = Query(None),
    x_lake_webhook_secret: str | None = Header(None, alias="X-Lake-Webhook-Secret"),
    x_lake_signature: str | None = Header(None, alias="X-Lake-Signature"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_active_workspace: str | None = Cookie(None),
    db: Session = Depends(get_db),
):
    """Native webhook trigger for pipelines.

    Supports unauthenticated-by-cookie HTTP requests verified via:
    1. Microsoft Graph validation handshake (?validationToken=...)
    2. Header: X-Lake-Webhook-Secret
    3. Query parameter: ?token=...
    4. HMAC-SHA256 signature headers (X-Hub-Signature-256, X-Lake-Signature)
    """
    # 1. Handle Microsoft Graph change notification subscription validation handshake
    if validation_token:
        return Response(content=validation_token, media_type="text/plain", status_code=200)

    # 2. Resolve workspace
    target_ws = workspace_slug or x_workspace_slug or lake_active_workspace
    if not target_ws:
        match = re.match(r"^ws_([a-zA-Z0-9_-]+?)_", dag_id)
        if match:
            target_ws = match.group(1)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workspace identifier required (via URL path, query, or X-Workspace-Slug header)",
            )

    ws = _resolve_workspace_or_404(target_ws, db)

    # Ensure workspace has a webhook secret
    if not ws.webhook_secret:
        ws.webhook_secret = generate_webhook_secret()
        db.commit()

    # 3. Authenticate incoming webhook request
    raw_body = await request.body()
    signature_header = x_lake_signature or x_hub_signature_256

    is_authenticated = _verify_webhook_auth(
        raw_body=raw_body,
        secret=ws.webhook_secret,
        header_secret=x_lake_webhook_secret,
        query_token=token,
        signature_header=signature_header,
    )
    if not is_authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook secret/signature",
        )

    # 4. Check if webhook trigger is enabled for this pipeline
    meta_map = pipeline_service.get_pipeline_metadata_map(ws.id, db)
    meta = meta_map.get(dag_id)
    if meta and not meta.webhook_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Webhook trigger is disabled for pipeline '{dag_id}'",
        )

    # 5. Parse payload and unwrap Pub/Sub notifications if present
    try:
        payload_data = json.loads(raw_body) if raw_body else {}
    except Exception:
        payload_data = {"raw_text": raw_body.decode("utf-8", errors="replace")}

    if (
        isinstance(payload_data, dict)
        and "message" in payload_data
        and isinstance(payload_data["message"], dict)
    ):
        msg = payload_data["message"]
        if "data" in msg and isinstance(msg["data"], str):
            try:
                decoded_bytes = base64.b64decode(msg["data"])
                try:
                    payload_data["unwrapped_data"] = json.loads(decoded_bytes.decode("utf-8"))
                except Exception:
                    payload_data["unwrapped_data"] = decoded_bytes.decode("utf-8", errors="replace")
            except Exception as b64_err:
                logger.debug("Could not b64decode Pub/Sub message data: %s", b64_err)

    clean_headers = {
        k.lower(): v for k, v in request.headers.items() if k.lower() not in SENSITIVE_HEADERS
    }

    event_id = str(uuid.uuid4())
    conf = {
        "triggered_by": "webhook",
        "webhook_event_id": event_id,
        "webhook_payload": payload_data,
        "webhook_headers": clean_headers,
    }

    # 6. Trigger DAG run asynchronously
    run_info = await pipeline_service.trigger_dag_run(
        workspace_slug=ws.slug,
        dag_id=dag_id,
        conf=conf,
        triggered_by="webhook",
        is_superadmin=True,
    )

    return {
        "status": "queued",
        "dag_id": dag_id,
        "dag_run_id": run_info.get("dag_run_id"),
        "state": run_info.get("state", "queued"),
        "execution_date": run_info.get("execution_date"),
        "workspace_slug": ws.slug,
        "event_id": event_id,
    }


@router.get("/{dag_id}/runs")
async def get_pipeline_runs(
    dag_id: str,
    limit: int = Query(25, ge=1, le=100),
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List execution run history for a pipeline."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get single DAG run details."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get task instances for a specific execution run."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fetch task execution logs."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel / stop an active or queued execution run."""
    ws, role = _resolve_workspace_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
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
