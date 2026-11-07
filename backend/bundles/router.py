"""REST API router for Lake Asset Bundles (LABs)."""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
    status,
)
from sqlalchemy.orm import Session

try:
    from auth import get_current_user, get_workspace_context
    from database import get_db
    from models import User, Workspace
except ImportError:
    from backend.auth import get_current_user, get_workspace_context
    from backend.database import get_db
    from backend.models import User, Workspace

from .deployer import BundleDeployer
from .schemas import (
    BundleDeployRequest,
    BundleDeployResponse,
    BundleDestroyResponse,
    BundleDetailResponse,
    BundleItemResponse,
    BundleListResponse,
    BundleRunRequest,
    BundleRunResponse,
    BundleValidateRequest,
    BundleValidationResponse,
)
from .state_service import BundleStateService

logger = logging.getLogger("lake.backend.bundles.router")

router = APIRouter()


# ── Dependency Helpers ────────────────────────────────────────────────────────


def _resolve_bundle_workspace(
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str]:
    """Resolve active workspace and role with permissions verification."""
    slug = workspace_slug or x_workspace_slug
    ws_id = workspace_id or x_workspace_id or lake_active_workspace or lake_workspace_id

    return get_workspace_context(
        workspace_slug=slug,
        workspace_id=ws_id,
        current_user=current_user,
        db=db,
        require_write=require_write,
        write_error_msg="Workspace viewer role is read-only. Cannot modify, deploy, or run bundles.",
    )


# ── REST API Endpoints ────────────────────────────────────────────────────────


@router.post("/validate", response_model=BundleValidationResponse)
def validate_bundle(
    req: BundleValidateRequest,
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BundleValidationResponse:
    """Syntactic & semantic manifest validation, DAG cycle check, and dry-run reconciliation diff."""
    # Resolve workspace (fallback to body params if query/header empty)
    eff_slug = workspace_slug or req.workspace_slug
    eff_id = workspace_id or req.workspace_id

    ws, _ = get_workspace_context(
        workspace_slug=eff_slug or x_workspace_slug,
        workspace_id=eff_id or x_workspace_id or lake_active_workspace or lake_workspace_id,
        current_user=current_user,
        db=db,
        require_write=False,
    )

    return BundleDeployer.validate(
        db=db,
        workspace=ws,
        manifest_content=req.manifest_content,
        manifest_format=req.manifest_format,
        target_name=req.target,
        variable_overrides=req.variable_overrides,
    )


@router.post("/deploy", response_model=BundleDeployResponse, status_code=status.HTTP_201_CREATED)
def deploy_bundle(
    req: BundleDeployRequest,
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BundleDeployResponse:
    """Atomic deployment orchestrating resource provisioning and state recording with rollback."""
    eff_slug = workspace_slug or req.workspace_slug
    eff_id = workspace_id or req.workspace_id

    ws, _ = get_workspace_context(
        workspace_slug=eff_slug or x_workspace_slug,
        workspace_id=eff_id or x_workspace_id or lake_active_workspace or lake_workspace_id,
        current_user=current_user,
        db=db,
        require_write=True,
    )

    return BundleDeployer.deploy(
        db=db,
        workspace=ws,
        user=current_user,
        manifest_content=req.manifest_content,
        manifest_format=req.manifest_format,
        target_name=req.target,
        variable_overrides=req.variable_overrides,
        commit_hash=req.commit_hash,
        files=req.files,
        dry_run=req.dry_run,
    )


@router.get("", response_model=BundleListResponse)
@router.get("/", response_model=BundleListResponse)
def list_bundles(
    status_filter: str | None = Query(None, alias="status"),
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BundleListResponse:
    """List all deployed bundles in the active workspace."""
    ws, _ = get_workspace_context(
        workspace_slug=workspace_slug or x_workspace_slug,
        workspace_id=workspace_id or x_workspace_id or lake_active_workspace or lake_workspace_id,
        current_user=current_user,
        db=db,
        require_write=False,
    )

    bundles = BundleStateService.list_bundles(db, ws.id, status=status_filter)
    items = [
        BundleItemResponse(
            id=b.id,
            workspace_id=b.workspace_id,
            bundle_name=b.bundle_name,
            target=b.target,
            status=b.status,
            version=b.version,
            manifest_format="toon",
            commit_hash=b.git_commit_sha,
            resources_count=sum(len(v) for v in (b.resources_map or {}).values()),
            deployed_by=b.deployed_by,
            created_at=b.created_at,
            updated_at=b.updated_at,
            last_deployed_at=b.last_deployed_at,
        )
        for b in bundles
    ]

    return BundleListResponse(bundles=items, total=len(items))


@router.get("/{bundle_id}", response_model=BundleDetailResponse)
def get_bundle(
    bundle_id: uuid.UUID,
    include_drift: bool = Query(True, description="Run live drift detection"),
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BundleDetailResponse:
    """Retrieve detailed bundle deployment status, resource map, manifest snapshot, and drift."""
    ws, _ = get_workspace_context(
        workspace_slug=workspace_slug or x_workspace_slug,
        workspace_id=workspace_id or x_workspace_id or lake_active_workspace or lake_workspace_id,
        current_user=current_user,
        db=db,
        require_write=False,
    )

    bundle = BundleStateService.get_bundle_by_id(db, bundle_id)
    if not bundle or bundle.workspace_id != ws.id:
        raise HTTPException(404, "Bundle not found in this workspace")

    drift_report = None
    if include_drift:
        try:
            drift_report = BundleStateService.detect_drift(db, bundle)
        except Exception as exc:
            logger.warning("Drift detection error for bundle %s: %s", bundle.id, exc)

    return BundleDetailResponse(
        id=bundle.id,
        workspace_id=bundle.workspace_id,
        bundle_name=bundle.bundle_name,
        target=bundle.target,
        status=bundle.status,
        version=bundle.version,
        manifest_format="toon",
        manifest_raw=json.dumps(bundle.manifest_snapshot, indent=2)
        if bundle.manifest_snapshot
        else "",
        commit_hash=bundle.git_commit_sha,
        resources_map=bundle.resources_map or {},
        deployed_by=bundle.deployed_by,
        created_at=bundle.created_at,
        updated_at=bundle.updated_at,
        last_deployed_at=bundle.last_deployed_at,
        error_message=getattr(bundle, "error_message", None),
        drift=drift_report,
    )


@router.post("/{bundle_id}/run", response_model=BundleRunResponse)
async def run_bundle_pipeline(
    bundle_id: uuid.UUID,
    req: BundleRunRequest | None = None,
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BundleRunResponse:
    """Ad-hoc trigger of a specific bundle pipeline/DAG execution."""
    ws, _ = get_workspace_context(
        workspace_slug=workspace_slug or x_workspace_slug,
        workspace_id=workspace_id or x_workspace_id or lake_active_workspace or lake_workspace_id,
        current_user=current_user,
        db=db,
        require_write=True,
    )

    pipe_name = req.pipeline_name if req else None
    conf = req.conf if req else None

    return await BundleDeployer.run_pipeline(
        db=db,
        workspace=ws,
        bundle_id=bundle_id,
        pipeline_name=pipe_name,
        conf=conf,
        triggered_by=current_user.email,
    )


@router.delete("/{bundle_id}", response_model=BundleDestroyResponse)
def destroy_bundle(
    bundle_id: uuid.UUID,
    purge_storage: bool = Query(False, description="Purge uploaded artifact files from storage"),
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BundleDestroyResponse:
    """Clean teardown of all managed resources tracked in the bundle's resources_map."""
    ws, _ = get_workspace_context(
        workspace_slug=workspace_slug or x_workspace_slug,
        workspace_id=workspace_id or x_workspace_id or lake_active_workspace or lake_workspace_id,
        current_user=current_user,
        db=db,
        require_write=True,
    )

    return BundleDeployer.destroy(
        db=db,
        workspace=ws,
        bundle_id=bundle_id,
        purge_storage=purge_storage,
    )
