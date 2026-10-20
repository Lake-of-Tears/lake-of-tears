from __future__ import annotations

import logging
from typing import Any

import storage_service
from auth import get_current_user, get_storage_workspace_and_role
from database import get_db
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from models import User
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.storage.router")

router = APIRouter()


# ── Dependency Context Resolution ─────────────────────────────────────────────


def _resolve_storage_context(
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
    return get_storage_workspace_and_role(
        workspace_id=ws_id,
        workspace_slug=slug,
        current_user=current_user,
        db=db,
        require_write=require_write,
    )


def _storage_write_context(
    workspace_slug: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_active_workspace: str | None = Cookie(None),
    lake_workspace_id: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[Any, str]:
    return _resolve_storage_context(
        workspace_slug=workspace_slug,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_active_workspace=lake_active_workspace,
        lake_workspace_id=lake_workspace_id,
        current_user=current_user,
        db=db,
        require_write=True,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/stats")
async def get_storage_stats(
    ctx: tuple[Any, str] = Depends(_resolve_storage_context),
) -> dict[str, Any]:
    """Get aggregated bucket storage metrics and top-level prefix breakdown for active workspace."""
    ws, _ = ctx
    return storage_service.get_bucket_summary(workspace_slug=getattr(ws, "slug", None))


@router.get("/browse")
async def browse_storage(
    prefix: str = Query("", description="S3 prefix to browse"),
    limit: int = Query(100, ge=1, le=1000, description="Max items per page"),
    continuation_token: str | None = Query(None, description="S3 pagination continuation token"),
    ctx: tuple[Any, str] = Depends(_resolve_storage_context),
) -> dict[str, Any]:
    """List folders and objects in a bucket prefix jailed to active workspace."""
    ws, _ = ctx
    slug = getattr(ws, "slug", "default")
    try:
        return storage_service.browse_prefix(
            prefix=prefix,
            limit=limit,
            continuation_token=continuation_token,
            workspace_slug=slug,
        )
    except (storage_service.StorageAccessDeniedError, PermissionError) as exc:
        logger.warning(
            "Access denied browsing storage prefix '%s' in workspace '%s': %s", prefix, slug, exc
        )
        raise HTTPException(status_code=403, detail=str(exc))
    except (storage_service.StoragePathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to browse storage prefix '%s': %s", prefix, exc)
        raise HTTPException(status_code=500, detail=f"Failed to browse prefix: {exc}")


@router.get("/objects/{key:path}/metadata")
async def get_object_metadata(
    key: str,
    ctx: tuple[Any, str] = Depends(_resolve_storage_context),
) -> dict[str, Any]:
    """Retrieve object metadata and Parquet schema inspection."""
    ws, _ = ctx
    slug = getattr(ws, "slug", "default")
    try:
        return storage_service.get_object_metadata(key=key, workspace_slug=slug)
    except (storage_service.StorageAccessDeniedError, PermissionError) as exc:
        logger.warning(
            "Access denied getting metadata for '%s' in workspace '%s': %s", key, slug, exc
        )
        raise HTTPException(status_code=403, detail=str(exc))
    except (storage_service.StoragePathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to get object metadata for '%s': %s", key, exc)
        if "NoSuchKey" in str(exc) or "404" in str(exc):
            raise HTTPException(status_code=404, detail="Object not found")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve metadata: {exc}")


@router.get("/objects/{key:path}/download")
async def get_download_url(
    key: str,
    expires_in: int = Query(3600, ge=60, le=86400),
    ctx: tuple[Any, str] = Depends(_resolve_storage_context),
) -> dict[str, Any]:
    """Generate a presigned S3 GET URL for direct download."""
    ws, _ = ctx
    slug = getattr(ws, "slug", "default")
    try:
        url = storage_service.get_download_url(key=key, expires_in=expires_in, workspace_slug=slug)
        jailed_key = storage_service.validate_and_jail_path(slug, key, is_prefix=False)
        return {
            "key": jailed_key,
            "download_url": url,
            "stream_url": f"/api/v1/storage/objects/{jailed_key}/stream",
            "expires_in": expires_in,
        }
    except (storage_service.StorageAccessDeniedError, PermissionError) as exc:
        logger.warning(
            "Access denied generating download URL for '%s' in workspace '%s': %s", key, slug, exc
        )
        raise HTTPException(status_code=403, detail=str(exc))
    except (storage_service.StoragePathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to generate download URL for '%s': %s", key, exc)
        raise HTTPException(status_code=500, detail=f"Failed to generate download URL: {exc}")


@router.get("/objects/{key:path}/stream")
async def stream_object(
    key: str,
    ctx: tuple[Any, str] = Depends(_resolve_storage_context),
) -> StreamingResponse:
    """Stream object content as fallback."""
    ws, _ = ctx
    slug = getattr(ws, "slug", "default")
    try:
        stream_gen, headers = storage_service.stream_object(key=key, workspace_slug=slug)
        return StreamingResponse(stream_gen, headers=headers)
    except (storage_service.StorageAccessDeniedError, PermissionError) as exc:
        logger.warning("Access denied streaming object '%s' in workspace '%s': %s", key, slug, exc)
        raise HTTPException(status_code=403, detail=str(exc))
    except (storage_service.StoragePathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to stream object '%s': %s", key, exc)
        if "NoSuchKey" in str(exc) or "404" in str(exc):
            raise HTTPException(status_code=404, detail="Object not found")
        raise HTTPException(status_code=500, detail=f"Failed to stream object: {exc}")


@router.delete("/objects/{key:path}")
async def delete_object(
    key: str,
    ctx: tuple[Any, str] = Depends(_storage_write_context),
) -> dict[str, Any]:
    """Delete an S3 object. Restricted to workspace admin and superadmin roles."""
    ws, _ = ctx
    slug = getattr(ws, "slug", "default")
    try:
        return storage_service.delete_object(key=key, workspace_slug=slug)
    except (storage_service.StorageAccessDeniedError, PermissionError) as exc:
        logger.warning("Access denied deleting object '%s' in workspace '%s': %s", key, slug, exc)
        raise HTTPException(status_code=403, detail=str(exc))
    except (storage_service.StoragePathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to delete object '%s': %s", key, exc)
        raise HTTPException(status_code=500, detail=f"Failed to delete object: {exc}")


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    prefix: str = Form(""),
    ctx: tuple[Any, str] = Depends(_storage_write_context),
) -> dict[str, Any]:
    """Upload a file directly into active workspace storage prefix."""
    ws, _ = ctx
    slug = getattr(ws, "slug", "default")
    filename = file.filename or "upload"
    target_key = f"{prefix.strip('/')}/{filename}" if prefix.strip("/") else filename
    try:
        content = await file.read()
        return storage_service.upload_object(
            key=target_key,
            data=content,
            content_type=file.content_type or "application/octet-stream",
            workspace_slug=slug,
        )
    except (storage_service.StorageAccessDeniedError, PermissionError) as exc:
        logger.warning(
            "Access denied uploading to '%s' in workspace '%s': %s", target_key, slug, exc
        )
        raise HTTPException(status_code=403, detail=str(exc))
    except (storage_service.StoragePathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to upload object '%s': %s", target_key, exc)
        raise HTTPException(status_code=500, detail=f"Failed to upload object: {exc}")


@router.post("/objects/{key:path}/upload-url")
async def get_upload_url(
    key: str,
    content_type: str = Query("application/octet-stream"),
    expires_in: int = Query(3600, ge=60, le=86400),
    ctx: tuple[Any, str] = Depends(_storage_write_context),
) -> dict[str, Any]:
    """Generate a presigned S3 PUT URL for uploading directly to active workspace prefix."""
    ws, _ = ctx
    slug = getattr(ws, "slug", "default")
    try:
        url = storage_service.get_upload_url(
            key=key,
            expires_in=expires_in,
            content_type=content_type,
            workspace_slug=slug,
        )
        jailed_key = storage_service.validate_and_jail_path(slug, key, is_prefix=False)
        return {
            "key": jailed_key,
            "upload_url": url,
            "expires_in": expires_in,
        }
    except (storage_service.StorageAccessDeniedError, PermissionError) as exc:
        logger.warning(
            "Access denied generating upload URL for '%s' in workspace '%s': %s", key, slug, exc
        )
        raise HTTPException(status_code=403, detail=str(exc))
    except (storage_service.StoragePathError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to generate upload URL for '%s': %s", key, exc)
        raise HTTPException(status_code=500, detail=f"Failed to generate upload URL: {exc}")
