from __future__ import annotations

import logging
import uuid
from typing import Any

from auth import get_current_user, get_workspace_context
from database import get_db
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, status
from models import User, Workspace
from schemas import (
    SecretAclCreate,
    SecretAclResponse,
    SecretAuditLogResponse,
    SecretKeyCreate,
    SecretKeySummaryResponse,
    SecretScopeCreate,
    SecretScopeResponse,
    SecretValueResponse,
    SecretValueUpdate,
)
from secrets_manager.service import SecretManagerService
from sqlalchemy.orm import Session

logger = logging.getLogger("lake.backend.secrets.router")

router = APIRouter()


def _resolve_context(
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[Workspace, str]:
    ws_id = workspace_id or x_workspace_id or lake_workspace_id or lake_active_workspace
    slug = workspace_slug or x_workspace_slug
    return get_workspace_context(
        workspace_id=ws_id,
        workspace_slug=slug,
        current_user=current_user,
        db=db,
    )


# ── Scope Management ──────────────────────────────────────────────────────────


@router.get("/scopes", response_model=list[SecretScopeResponse])
def list_secret_scopes(
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SecretScopeResponse]:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    items = svc.list_scopes(ws.id, current_user, role)
    return [
        SecretScopeResponse(
            id=s.id,
            workspace_id=s.workspace_id,
            name=s.name,
            backend_type=s.backend_type,
            provider_config=s.provider_config,
            created_by=s.created_by,
            created_at=s.created_at,
            updated_at=s.updated_at,
            user_permission=perm,
            key_count=count,
        )
        for s, perm, count in items
    ]


@router.post(
    "/scopes",
    response_model=SecretScopeResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_secret_scope(
    payload: SecretScopeCreate,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SecretScopeResponse:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    if role not in ("admin",) and current_user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace admin or superadmin access required to create secret scopes",
        )

    svc = SecretManagerService(db)
    scope = svc.create_scope(
        workspace_id=ws.id,
        name=payload.name,
        backend_type=payload.backend_type,
        provider_config=payload.provider_config,
        user=current_user,
    )
    return SecretScopeResponse(
        id=scope.id,
        workspace_id=scope.workspace_id,
        name=scope.name,
        backend_type=scope.backend_type,
        provider_config=scope.provider_config,
        created_by=scope.created_by,
        created_at=scope.created_at,
        updated_at=scope.updated_at,
        user_permission="MANAGE",
        key_count=0,
    )


@router.get("/scopes/{scope_identifier}", response_model=SecretScopeResponse)
def get_secret_scope(
    scope_identifier: str,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SecretScopeResponse:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    perm = svc.require_permission(scope, current_user, role, "READ")
    key_count = len(svc.list_keys_in_scope(scope))
    return SecretScopeResponse(
        id=scope.id,
        workspace_id=scope.workspace_id,
        name=scope.name,
        backend_type=scope.backend_type,
        provider_config=scope.provider_config,
        created_by=scope.created_by,
        created_at=scope.created_at,
        updated_at=scope.updated_at,
        user_permission=perm,
        key_count=key_count,
    )


@router.delete("/scopes/{scope_identifier}")
def delete_secret_scope(
    scope_identifier: str,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    svc.require_permission(scope, current_user, role, "MANAGE")
    svc.delete_scope(scope)
    return {"status": "deleted", "scope": scope.name}


# ── Secret Key Operations ─────────────────────────────────────────────────────


@router.get("/scopes/{scope_identifier}/keys", response_model=list[SecretKeySummaryResponse])
def list_secret_keys(
    scope_identifier: str,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SecretKeySummaryResponse]:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    svc.require_permission(scope, current_user, role, "READ")
    keys = svc.list_keys_in_scope(scope)
    return [
        SecretKeySummaryResponse(
            key=k.key,
            version=k.version,
            created_at=k.created_at,
            updated_at=k.updated_at,
            updated_by=k.updated_by,
        )
        for k in keys
    ]


@router.post(
    "/scopes/{scope_identifier}/keys",
    response_model=SecretKeySummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
def put_secret_key(
    scope_identifier: str,
    payload: SecretKeyCreate,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SecretKeySummaryResponse:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    svc.require_permission(scope, current_user, role, "WRITE")
    record = svc.put_secret_value(scope, payload.key, payload.value, current_user)
    return SecretKeySummaryResponse(
        key=record.key,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        updated_by=record.updated_by,
    )


@router.get("/scopes/{scope_identifier}/keys/{key}", response_model=SecretValueResponse)
def get_secret_key_value(
    scope_identifier: str,
    key: str,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SecretValueResponse:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    svc.require_permission(scope, current_user, role, "READ")
    val, ver, upd_at = svc.get_secret_value(scope, key)
    return SecretValueResponse(
        key=key,
        value=val,
        version=ver,
        updated_at=upd_at,
    )


@router.put(
    "/scopes/{scope_identifier}/keys/{key}",
    response_model=SecretKeySummaryResponse,
    status_code=status.HTTP_200_OK,
)
def put_secret_key_by_name(
    scope_identifier: str,
    key: str,
    payload: SecretValueUpdate,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SecretKeySummaryResponse:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    svc.require_permission(scope, current_user, role, "WRITE")
    record = svc.put_secret_value(scope, key, payload.value, current_user)
    return SecretKeySummaryResponse(
        key=record.key,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        updated_by=record.updated_by,
    )


@router.delete("/scopes/{scope_identifier}/keys/{key}")
def delete_secret_key(
    scope_identifier: str,
    key: str,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    svc.require_permission(scope, current_user, role, "WRITE")
    svc.delete_secret_value(scope, key, user=current_user)
    return {"status": "deleted", "scope": scope.name, "key": key}


@router.get("/scopes/{scope_identifier}/audit", response_model=list[SecretAuditLogResponse])
def list_scope_audit_logs(
    scope_identifier: str,
    limit: int = Query(100, ge=1, le=500),
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SecretAuditLogResponse]:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    svc.require_permission(scope, current_user, role, "READ")
    logs = svc.list_audit_logs(scope, limit=limit)
    return [
        SecretAuditLogResponse(
            id=row.id,
            workspace_id=row.workspace_id,
            scope_id=row.scope_id,
            scope_name=row.scope_name,
            key=row.key,
            action=row.action,
            version=row.version,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            user_id=row.user_id,
            bundle_name=row.bundle_name,
            pipeline_name=row.pipeline_name,
            task_name=row.task_name,
            created_at=row.created_at,
        )
        for row in logs
    ]


# ── Scope ACL Management ──────────────────────────────────────────────────────


@router.get("/scopes/{scope_identifier}/acls", response_model=list[SecretAclResponse])
def list_scope_acls(
    scope_identifier: str,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SecretAclResponse]:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    svc.require_permission(scope, current_user, role, "MANAGE")
    acls = svc.list_acls(scope)
    return [
        SecretAclResponse(
            id=a.id,
            scope_id=a.scope_id,
            principal_type=a.principal_type,
            principal_id=a.principal_id,
            permission=a.permission,
            created_at=a.created_at,
            created_by=a.created_by,
        )
        for a in acls
    ]


@router.post(
    "/scopes/{scope_identifier}/acls",
    response_model=SecretAclResponse,
    status_code=status.HTTP_200_OK,
)
def set_scope_acl(
    scope_identifier: str,
    payload: SecretAclCreate,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SecretAclResponse:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    svc.require_permission(scope, current_user, role, "MANAGE")
    acl = svc.set_acl(
        scope=scope,
        principal_type=payload.principal_type,
        principal_id=payload.principal_id,
        permission=payload.permission,
        user=current_user,
    )
    return SecretAclResponse(
        id=acl.id,
        scope_id=acl.scope_id,
        principal_type=acl.principal_type,
        principal_id=acl.principal_id,
        permission=acl.permission,
        created_at=acl.created_at,
        created_by=acl.created_by,
    )


@router.delete("/scopes/{scope_identifier}/acls/{acl_id}")
def delete_scope_acl(
    scope_identifier: str,
    acl_id: uuid.UUID,
    workspace_id: str | None = None,
    workspace_slug: str | None = Query(None),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws, role = _resolve_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        x_workspace_id=x_workspace_id,
        x_workspace_slug=x_workspace_slug,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
    )
    svc = SecretManagerService(db)
    scope = svc.get_scope(ws.id, scope_identifier)
    svc.require_permission(scope, current_user, role, "MANAGE")
    svc.delete_acl(scope, acl_id)
    return {"status": "deleted", "scope": scope.name, "acl_id": str(acl_id)}
