from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from database import get_db
from fastapi import Cookie, Depends, Header, HTTPException
from models import SystemSetting, User, Workspace, WorkspaceMember
from passlib.context import CryptContext
from sqlalchemy.orm import Session

SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "dev-secret-please-change-in-production")
ALGORITHM = "HS256"
COOKIE_NAME = "lake_token"

# Fallback TTL used when the DB is unavailable (e.g. during first startup).
_FALLBACK_TTL = timedelta(hours=24)

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


def get_session_expires_delta(db: Session) -> timedelta:
    """Return the token TTL based on current system settings."""
    timeout_row = (
        db.query(SystemSetting)
        .filter(SystemSetting.key == "session_inactivity_timeout_minutes")
        .first()
    )
    max_row = db.query(SystemSetting).filter(SystemSetting.key == "session_max_hours").first()
    timeout_minutes = timeout_row.value if timeout_row and timeout_row.value else 0
    max_hours = max_row.value if max_row and max_row.value else 24
    if timeout_minutes > 0:
        return timedelta(minutes=timeout_minutes)
    return timedelta(hours=max_hours)


def create_token(
    data: dict,
    expires_delta: timedelta | None = None,
    session_start_ts: float | None = None,
) -> str:
    now = datetime.now(UTC)
    if expires_delta is None:
        expires_delta = _FALLBACK_TTL
    payload = {
        **data,
        "ss": session_start_ts if session_start_ts is not None else now.timestamp(),
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def get_current_user(
    lake_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    token = lake_token
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

    sub_val = payload.get("sub")
    try:
        user_uuid = uuid.UUID(str(sub_val))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(401, "Invalid token")

    user = db.query(User).filter(User.id == user_uuid).first()
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or disabled")
    return user


def require_superadmin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "superadmin":
        raise HTTPException(403, "Superadmin access required")
    return current_user


def get_workspace_member(
    workspace_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceMember:
    member = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == current_user.id,
        )
        .first()
    )
    if not member and current_user.role != "superadmin":
        raise HTTPException(403, "Not a member of this workspace")
    return member


def require_workspace_admin(
    workspace_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceMember:
    if current_user.role == "superadmin":
        member = (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == current_user.id,
            )
            .first()
        )
        return member  # superadmin always passes
    member = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == current_user.id,
            WorkspaceMember.role == "admin",
        )
        .first()
    )
    if not member:
        raise HTTPException(403, "Workspace admin access required")
    return member


def get_notebook_workspace_and_role(
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str]:
    """Resolve workspace and verify user permissions for notebook operations."""
    ws = None
    if workspace_slug:
        ws = (
            db.query(Workspace)
            .filter(Workspace.slug == workspace_slug.strip(), Workspace.status == "active")
            .first()
        )
    elif workspace_id:
        try:
            ws_uuid = uuid.UUID(str(workspace_id))
            ws = (
                db.query(Workspace)
                .filter(Workspace.id == ws_uuid, Workspace.status == "active")
                .first()
            )
        except (ValueError, TypeError):
            ws = None

    if not ws:
        if current_user.role == "superadmin":
            ws = (
                db.query(Workspace)
                .filter(Workspace.status == "active")
                .order_by(Workspace.created_at)
                .first()
            )
        else:
            ws = (
                db.query(Workspace)
                .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
                .filter(WorkspaceMember.user_id == current_user.id, Workspace.status == "active")
                .order_by(Workspace.created_at)
                .first()
            )

    if not ws:
        raise HTTPException(404, "Workspace not found")

    member = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == current_user.id,
        )
        .first()
    )

    if not member and current_user.role != "superadmin":
        raise HTTPException(403, "Not a member of this workspace")

    role = (
        member.role if member else ("superadmin" if current_user.role == "superadmin" else "viewer")
    )

    if require_write and current_user.role != "superadmin" and role == "viewer":
        raise HTTPException(
            403, "Workspace viewer role is read-only. Cannot modify or execute notebooks."
        )

    return ws, role


def get_pipeline_workspace_and_role(
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str]:
    """Resolve workspace and verify user permissions for pipeline operations."""
    ws = None
    if workspace_slug:
        ws = (
            db.query(Workspace)
            .filter(Workspace.slug == workspace_slug.strip(), Workspace.status == "active")
            .first()
        )
    elif workspace_id:
        try:
            ws_uuid = uuid.UUID(str(workspace_id))
            ws = (
                db.query(Workspace)
                .filter(Workspace.id == ws_uuid, Workspace.status == "active")
                .first()
            )
        except (ValueError, TypeError):
            ws = None

    if not ws:
        if current_user.role == "superadmin":
            ws = (
                db.query(Workspace)
                .filter(Workspace.status == "active")
                .order_by(Workspace.created_at)
                .first()
            )
        else:
            ws = (
                db.query(Workspace)
                .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
                .filter(WorkspaceMember.user_id == current_user.id, Workspace.status == "active")
                .order_by(Workspace.created_at)
                .first()
            )

    if not ws:
        raise HTTPException(404, "Workspace not found")

    member = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == current_user.id,
        )
        .first()
    )

    if not member and current_user.role != "superadmin":
        raise HTTPException(403, "Not a member of this workspace")

    role = (
        member.role if member else ("superadmin" if current_user.role == "superadmin" else "viewer")
    )

    if require_write and current_user.role != "superadmin" and role == "viewer":
        raise HTTPException(
            403, "Workspace viewer role is read-only. Cannot modify or trigger pipelines."
        )

    return ws, role


def get_storage_workspace_and_role(
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str]:
    """Resolve workspace and verify user permissions for storage operations."""
    ws = None
    if workspace_slug:
        ws = (
            db.query(Workspace)
            .filter(Workspace.slug == workspace_slug.strip(), Workspace.status == "active")
            .first()
        )
    elif workspace_id:
        try:
            ws_uuid = uuid.UUID(str(workspace_id))
            ws = (
                db.query(Workspace)
                .filter(Workspace.id == ws_uuid, Workspace.status == "active")
                .first()
            )
        except (ValueError, TypeError):
            ws = None

    if not ws:
        if current_user.role == "superadmin":
            ws = (
                db.query(Workspace)
                .filter(Workspace.status == "active")
                .order_by(Workspace.created_at)
                .first()
            )
        else:
            ws = (
                db.query(Workspace)
                .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
                .filter(WorkspaceMember.user_id == current_user.id, Workspace.status == "active")
                .order_by(Workspace.created_at)
                .first()
            )

    if not ws:
        raise HTTPException(404, "Workspace not found")

    member = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == current_user.id,
        )
        .first()
    )

    if not member and current_user.role != "superadmin":
        raise HTTPException(403, "Not a member of this workspace")

    role = (
        member.role if member else ("superadmin" if current_user.role == "superadmin" else "viewer")
    )

    if require_write and current_user.role != "superadmin" and role == "viewer":
        raise HTTPException(
            403, "Workspace viewer role is read-only. Cannot delete or modify storage objects."
        )

    return ws, role
