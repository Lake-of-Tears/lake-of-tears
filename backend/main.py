from __future__ import annotations

import os
import re
import threading
from datetime import UTC, datetime, timedelta

import jwt as _jwt
from auth import (
    COOKIE_NAME,
    create_token,
    decode_token,
    get_current_user,
    get_session_expires_delta,
    hash_password,
    require_superadmin,
    verify_password,
)
from database import get_db
from email_service import send_access_removed, send_access_requested, send_access_reviewed
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from models import (
    Catalog,
    CatalogAccess,
    CatalogSchema,
    CatalogTable,
    SystemSetting,
    User,
    Workspace,
    WorkspaceMember,
)
from oauth import enabled_providers, get_oauth_redirect, handle_oauth_callback
from schemas import (
    AddMemberRequest,
    CatalogAccessResponse,
    CatalogDirectoryItem,
    CatalogResponse,
    CatalogSchemaResponse,
    CatalogTableResponse,
    CreateCatalogRequest,
    CreateSchemaRequest,
    CreateTableRequest,
    CreateWorkspaceRequest,
    DeleteWorkspaceRequest,
    InboundAccessRequest,
    LoginRequest,
    PurgeCatalogRequest,
    RegisterRequest,
    RequestAccessRequest,
    ReviewAccessRequest,
    SharedCatalogSettingsItem,
    SystemSettingResponse,
    UpdateCatalogRequest,
    UpdateMemberRequest,
    UpdateMeRequest,
    UpdateSchemaRequest,
    UpdateSystemSettingsRequest,
    UpdateTableRequest,
    UpdateUserRequest,
    UpdateWorkspaceRequest,
    UserResponse,
    WorkspaceAdminResponse,
    WorkspaceCatalogSettingsResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

_DEFAULT_SOFT_DELETE_DAYS = 30
_DEFAULT_WORKSPACE_INACTIVE_DAYS = 30
_DEFAULT_SESSION_INACTIVITY_TIMEOUT_MINUTES = 0
_DEFAULT_SESSION_MAX_HOURS = 24

app = FastAPI(title="Lake of Tears Auth API", docs_url=None, redoc_url=None)


@app.on_event("startup")
def on_startup():
    from database import SessionLocal

    db = SessionLocal()
    try:
        _backfill_default_catalogs(db)
        _ensure_system_settings(db)
    finally:
        db.close()


app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("AUTH_BASE_URL", "http://localhost")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _slugify(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug or "workspace"


def _unique_slug(db: Session, base: str) -> str:
    slug, i = base, 0
    while db.query(Workspace).filter(Workspace.slug == slug).first():
        i += 1
        slug = f"{base}-{i}"
    return slug


def _create_default_catalog(db: Session, ws: Workspace, user: User) -> Catalog:
    slug = _unique_catalog_slug(db, "default")
    catalog = Catalog(
        name="default",
        slug=slug,
        description=None,
        owner_workspace_id=ws.id,
        created_by=user.id,
        is_default=True,
    )
    db.add(catalog)
    db.flush()
    _seed_medallion_schemas(db, catalog)
    return catalog


def _create_default_workspace(db: Session, user: User) -> Workspace:
    slug = _unique_slug(db, "default")
    ws = Workspace(name="Default", slug=slug, description="Default workspace", created_by=user.id)
    db.add(ws)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="admin"))
    _create_default_catalog(db, ws, user)
    return ws


def _backfill_default_catalogs(db: Session) -> None:
    workspaces = db.query(Workspace).all()
    for ws in workspaces:
        existing = (
            db.query(Catalog)
            .filter(
                Catalog.owner_workspace_id == ws.id,
                func.lower(Catalog.name) == "default",
            )
            .order_by(Catalog.created_at.asc())
            .first()
        )
        if existing:
            if not existing.is_default:
                existing.is_default = True
            continue
        creator_id = ws.created_by
        user = db.query(User).filter(User.id == creator_id).first() if creator_id else None
        if not user:
            user = db.query(User).filter(User.role == "superadmin").first()
        if not user:
            continue
        _create_default_catalog(db, ws, user)
    db.commit()


def _set_auth_cookie(
    response: Response,
    user: User,
    db: Session,
    session_start_ts: float | None = None,
) -> None:
    expires_delta = get_session_expires_delta(db)
    token = create_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "display_name": user.display_name or "",
        },
        expires_delta=expires_delta,
        session_start_ts=session_start_ts,
    )
    max_age = int(expires_delta.total_seconds())
    response.set_cookie(COOKIE_NAME, token, max_age=max_age, httponly=True, samesite="lax")


def _workspace_response(ws: Workspace, user: User) -> WorkspaceResponse:
    member = next((m for m in ws.members if m.user_id == user.id), None)
    return WorkspaceResponse(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        description=ws.description,
        created_at=ws.created_at,
        is_system=ws.is_system,
        status=ws.status,
        my_role=member.role if member else ("superadmin" if user.role == "superadmin" else None),
    )


def _workspace_admin_response(ws: Workspace, db: Session) -> WorkspaceAdminResponse:
    member_count = (
        db.query(func.count(WorkspaceMember.id))
        .filter(WorkspaceMember.workspace_id == ws.id)
        .scalar()
        or 0
    )
    owned_catalog_count = (
        db.query(func.count(Catalog.id))
        .filter(
            Catalog.owner_workspace_id == ws.id,
            Catalog.deleted_at.is_(None),
        )
        .scalar()
        or 0
    )
    return WorkspaceAdminResponse(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        description=ws.description,
        is_system=ws.is_system,
        status=ws.status,
        deleted_at=ws.deleted_at,
        scheduled_purge_at=ws.scheduled_purge_at,
        member_count=member_count,
        owned_catalog_count=owned_catalog_count,
        created_at=ws.created_at,
    )


# ── Auth ──────────────────────────────────────────────────────────────────────


@app.post("/api/auth/register")
def register(req: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(400, "Email already registered")

    is_first = db.query(User).count() == 0
    user = User(
        email=req.email,
        display_name=req.display_name or req.email.split("@")[0],
        hashed_password=hash_password(req.password),
        role="superadmin" if is_first else "user",
    )
    db.add(user)
    db.flush()

    if is_first:
        _create_default_workspace(db, user)
    else:
        # Add to Default workspace automatically
        default_ws = db.query(Workspace).filter(Workspace.slug == "default").first()
        if default_ws:
            db.add(WorkspaceMember(workspace_id=default_ws.id, user_id=user.id, role="user"))

    db.commit()
    db.refresh(user)
    _set_auth_cookie(response, user, db)
    return {"ok": True, "role": user.role}


@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email).first()
    if (
        not user
        or not user.hashed_password
        or not verify_password(req.password, user.hashed_password)
    ):
        raise HTTPException(401, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "Account disabled — contact your superadmin")
    _set_auth_cookie(response, user, db)
    return {"ok": True, "role": user.role}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie("lake_workspace_id")
    return {"ok": True}


@app.post("/api/auth/refresh")
def refresh_token(
    response: Response,
    db: Session = Depends(get_db),
    lake_token: str | None = Cookie(default=None),
):
    """Slide the session window. Preserves the original session_start for the absolute cap."""
    if not lake_token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = decode_token(lake_token)
    except _jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except _jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

    user = db.query(User).filter(User.id == payload["sub"]).first()
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or disabled")

    max_hours = _get_session_max_hours(db)
    session_start_ts = payload.get("ss")
    if session_start_ts:
        session_start = datetime.fromtimestamp(session_start_ts, tz=UTC)
        if datetime.now(UTC) - session_start >= timedelta(hours=max_hours):
            raise HTTPException(401, "Maximum session length reached — please sign in again")

    _set_auth_cookie(response, user, db, session_start_ts=payload.get("ss"))
    return {"ok": True}


@app.get("/api/auth/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return UserResponse.model_validate(current_user)


@app.patch("/api/auth/me", response_model=UserResponse)
def update_me(
    req: UpdateMeRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if req.display_name is not None:
        name = req.display_name.strip()
        if name:
            current_user.display_name = name
    db.commit()
    db.refresh(current_user)
    _set_auth_cookie(response, current_user, db)
    return UserResponse.model_validate(current_user)


@app.get("/api/auth/providers")
def providers():
    return enabled_providers()


# ── OAuth ─────────────────────────────────────────────────────────────────────


@app.get("/api/auth/oauth/{provider}")
def oauth_start(provider: str, request: Request, response: Response):
    return get_oauth_redirect(provider, request, response)


@app.get("/api/auth/oauth/{provider}/callback")
def oauth_callback(
    provider: str, request: Request, response: Response, db: Session = Depends(get_db)
):
    return handle_oauth_callback(provider, request, response, db)


# ── Users (superadmin) ────────────────────────────────────────────────────────


@app.get("/api/users", response_model=list[UserResponse])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_superadmin)):
    return [UserResponse.model_validate(u) for u in db.query(User).order_by(User.created_at).all()]


@app.patch("/api/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: str,
    req: UpdateUserRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(require_superadmin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if str(user.id) == str(current_admin.id) and req.role and req.role != "superadmin":
        raise HTTPException(400, "Cannot demote yourself")
    if req.role is not None:
        if req.role not in ("superadmin", "user"):
            raise HTTPException(400, "Global role must be 'superadmin' or 'user'")
        user.role = req.role
    if req.is_active is not None:
        user.is_active = req.is_active
    if req.display_name is not None:
        user.display_name = req.display_name
    db.commit()
    return UserResponse.model_validate(user)


# ── Workspaces ────────────────────────────────────────────────────────────────


@app.get("/api/workspaces", response_model=list[WorkspaceResponse])
def list_workspaces(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role == "superadmin":
        workspaces = (
            db.query(Workspace)
            .filter(Workspace.status == "active")
            .order_by(Workspace.created_at)
            .all()
        )
    else:
        workspaces = (
            db.query(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .filter(WorkspaceMember.user_id == current_user.id, Workspace.status == "active")
            .order_by(Workspace.created_at)
            .all()
        )
    return [_workspace_response(ws, current_user) for ws in workspaces]


@app.post("/api/workspaces", response_model=WorkspaceResponse)
def create_workspace(
    req: CreateWorkspaceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
):
    # Block name if an inactive workspace with the same name exists (name reservation)
    conflict = (
        db.query(Workspace).filter(func.lower(Workspace.name) == req.name.strip().lower()).first()
    )
    if conflict:
        raise HTTPException(409, "A workspace with that name already exists")
    slug = _unique_slug(db, _slugify(req.name))
    ws = Workspace(
        name=req.name.strip(),
        slug=slug,
        description=req.description,
        created_by=current_user.id,
    )
    db.add(ws)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=current_user.id, role="admin"))
    _create_default_catalog(db, ws, current_user)
    db.commit()
    db.refresh(ws)
    return _workspace_response(ws, current_user)


@app.get("/api/workspaces/{workspace_id}", response_model=WorkspaceResponse)
def get_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    # check membership unless superadmin
    if current_user.role != "superadmin":
        member = next((m for m in ws.members if m.user_id == current_user.id), None)
        if not member:
            raise HTTPException(403, "Not a member of this workspace")
    return _workspace_response(ws, current_user)


@app.patch("/api/workspaces/{workspace_id}", response_model=WorkspaceResponse)
def update_workspace(
    workspace_id: str,
    req: UpdateWorkspaceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    # Must be workspace admin or superadmin
    if current_user.role != "superadmin":
        member = next(
            (m for m in ws.members if m.user_id == current_user.id and m.role == "admin"), None
        )
        if not member:
            raise HTTPException(403, "Workspace admin access required")
    if req.name is not None:
        ws.name = req.name.strip()
    if req.description is not None:
        ws.description = req.description
    db.commit()
    return _workspace_response(ws, current_user)


@app.delete("/api/workspaces/{workspace_id}")
def delete_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if ws.slug == "default":
        raise HTTPException(400, "Cannot delete the Default workspace")
    db.delete(ws)
    db.commit()
    return {"ok": True}


# ── Workspace members ─────────────────────────────────────────────────────────


@app.get("/api/workspaces/{workspace_id}/members", response_model=list[WorkspaceMemberResponse])
def list_members(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if current_user.role != "superadmin":
        if not any(m.user_id == current_user.id for m in ws.members):
            raise HTTPException(403, "Not a member of this workspace")
    return [
        WorkspaceMemberResponse(
            user_id=m.user_id,
            email=m.user.email,
            display_name=m.user.display_name,
            role=m.role,
            joined_at=m.joined_at,
        )
        for m in ws.members
    ]


@app.post("/api/workspaces/{workspace_id}/members")
def add_member(
    workspace_id: str,
    req: AddMemberRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if ws.status == "inactive":
        raise HTTPException(409, "Cannot add members to an inactive workspace")
    if current_user.role != "superadmin":
        member_check = next(
            (m for m in ws.members if m.user_id == current_user.id and m.role == "admin"), None
        )
        if not member_check:
            raise HTTPException(403, "Workspace admin access required")

    target = db.query(User).filter(User.email == req.email).first()
    if not target:
        raise HTTPException(404, "User not found — they must register first")

    existing = (
        db.query(WorkspaceMember).filter_by(workspace_id=workspace_id, user_id=target.id).first()
    )
    if existing:
        existing.role = req.role
    else:
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=target.id, role=req.role))
    db.commit()
    return {"ok": True}


@app.patch("/api/workspaces/{workspace_id}/members/{user_id}")
def update_member(
    workspace_id: str,
    user_id: str,
    req: UpdateMemberRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if current_user.role != "superadmin":
        admin_check = next(
            (m for m in ws.members if m.user_id == current_user.id and m.role == "admin"), None
        )
        if not admin_check:
            raise HTTPException(403, "Workspace admin access required")

    member = db.query(WorkspaceMember).filter_by(workspace_id=workspace_id, user_id=user_id).first()
    if not member:
        raise HTTPException(404, "Member not found")
    if str(member.user_id) == str(current_user.id):
        raise HTTPException(400, "Cannot change your own workspace role")
    member.role = req.role
    db.commit()
    return {"ok": True}


@app.delete("/api/workspaces/{workspace_id}/members/{user_id}")
def remove_member(
    workspace_id: str,
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if current_user.role != "superadmin":
        admin_check = next(
            (m for m in ws.members if m.user_id == current_user.id and m.role == "admin"), None
        )
        if not admin_check:
            raise HTTPException(403, "Workspace admin access required")

    member = db.query(WorkspaceMember).filter_by(workspace_id=workspace_id, user_id=user_id).first()
    if not member:
        raise HTTPException(404, "Member not found")
    if str(member.user_id) == str(current_user.id):
        raise HTTPException(400, "Cannot remove yourself from a workspace")
    db.delete(member)
    db.commit()
    return {"ok": True}


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ── Catalog helpers ───────────────────────────────────────────────────────────

_MEDALLION_TIERS = ("bronze", "silver", "gold")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    return re.sub(r"[\s_-]+", "-", slug).strip("-") or "item"


def _unique_catalog_slug(db: Session, base: str) -> str:
    slug, i = base, 0
    while db.query(Catalog).filter(Catalog.slug == slug).first():
        i += 1
        slug = f"{base}-{i}"
    return slug


def _seed_medallion_schemas(db: Session, catalog: Catalog) -> None:
    for tier in _MEDALLION_TIERS:
        db.add(
            CatalogSchema(
                catalog_id=catalog.id,
                name=tier.capitalize(),
                slug=tier,
                tier=tier,
            )
        )


def _caller_access(db: Session, catalog: Catalog, user: User) -> str | None:
    if user.role == "superadmin":
        return "owner"
    ws_ids = [str(m.workspace_id) for m in user.workspace_memberships]
    if str(catalog.owner_workspace_id) in ws_ids:
        return "owner"
    grant = (
        db.query(CatalogAccess)
        .filter(
            CatalogAccess.catalog_id == catalog.id,
            CatalogAccess.workspace_id.in_(ws_ids),
            CatalogAccess.status == "approved",
        )
        .first()
    )
    return grant.mode if grant else None


def _assert_catalog_access(db: Session, catalog: Catalog, user: User, require: str = "read") -> str:
    access = _caller_access(db, catalog, user)
    if access is None:
        raise HTTPException(403, "No access to this catalog")
    if require == "write" and access not in ("owner", "write"):
        raise HTTPException(403, "Write access required")
    if require == "owner" and access != "owner":
        raise HTTPException(403, "Catalog owner access required")
    return access


def _owner_emails(db: Session, catalog: Catalog) -> list[str]:
    members = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == catalog.owner_workspace_id)
        .all()
    )
    return [m.user.email for m in members if m.user and m.user.is_active]


def _get_soft_delete_days(db: Session) -> int:
    setting = (
        db.query(SystemSetting).filter(SystemSetting.key == "catalog_soft_delete_days").first()
    )
    if setting is not None and setting.value is not None:
        return setting.value
    return _DEFAULT_SOFT_DELETE_DAYS


def _get_workspace_inactive_days(db: Session) -> int:
    setting = (
        db.query(SystemSetting)
        .filter(SystemSetting.key == "workspace_inactive_grace_period_days")
        .first()
    )
    if setting is not None and setting.value is not None:
        return setting.value
    return _DEFAULT_WORKSPACE_INACTIVE_DAYS


def _get_session_inactivity_minutes(db: Session) -> int:
    setting = (
        db.query(SystemSetting)
        .filter(SystemSetting.key == "session_inactivity_timeout_minutes")
        .first()
    )
    if setting is not None and setting.value is not None:
        return setting.value
    return _DEFAULT_SESSION_INACTIVITY_TIMEOUT_MINUTES


def _get_session_max_hours(db: Session) -> int:
    setting = (
        db.query(SystemSetting)
        .filter(SystemSetting.key == "session_max_hours")
        .first()
    )
    if setting is not None and setting.value is not None:
        return setting.value
    return _DEFAULT_SESSION_MAX_HOURS


def _ensure_system_settings(db: Session) -> None:
    defaults = {
        "catalog_soft_delete_days": _DEFAULT_SOFT_DELETE_DAYS,
        "workspace_inactive_grace_period_days": _DEFAULT_WORKSPACE_INACTIVE_DAYS,
        "session_inactivity_timeout_minutes": _DEFAULT_SESSION_INACTIVITY_TIMEOUT_MINUTES,
        "session_max_hours": _DEFAULT_SESSION_MAX_HOURS,
    }
    for key, value in defaults.items():
        if not db.query(SystemSetting).filter(SystemSetting.key == key).first():
            db.add(SystemSetting(key=key, value=value))
    db.commit()


def _catalog_response(catalog: Catalog, user: User, db: Session) -> CatalogResponse:
    access = _caller_access(db, catalog, user)
    owner_ws_name = catalog.owner_workspace.name if catalog.owner_workspace else None
    return CatalogResponse(
        id=catalog.id,
        name=catalog.name,
        slug=catalog.slug,
        description=catalog.description,
        owner_workspace_id=catalog.owner_workspace_id,
        owner_workspace_name=owner_ws_name,
        created_at=catalog.created_at,
        deleted_at=catalog.deleted_at,
        scheduled_purge_at=catalog.scheduled_purge_at,
        is_default=catalog.is_default,
        schemas=[
            CatalogSchemaResponse(
                id=s.id,
                catalog_id=s.catalog_id,
                name=s.name,
                slug=s.slug,
                description=s.description,
                tier=s.tier,
                created_at=s.created_at,
                tables=[CatalogTableResponse.model_validate(t) for t in s.tables],
            )
            for s in catalog.schemas
        ],
        my_access=access,
    )


# ── Catalog CRUD ──────────────────────────────────────────────────────────────


@app.get("/api/catalogs", response_model=list[CatalogResponse])
def list_catalogs(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role == "superadmin":
        catalogs = (
            db.query(Catalog)
            .filter(Catalog.deleted_at.is_(None))
            .order_by(Catalog.created_at)
            .all()
        )
    else:
        ws_ids = [m.workspace_id for m in current_user.workspace_memberships]
        shared_ids = db.query(CatalogAccess.catalog_id).filter(
            CatalogAccess.workspace_id.in_(ws_ids),
            CatalogAccess.status == "approved",
            CatalogAccess.suspended.is_(False),
        )
        from sqlalchemy import or_

        catalogs = (
            db.query(Catalog)
            .filter(
                or_(Catalog.owner_workspace_id.in_(ws_ids), Catalog.id.in_(shared_ids)),
                Catalog.deleted_at.is_(None),
            )
            .order_by(Catalog.created_at)
            .all()
        )
    return [_catalog_response(c, current_user, db) for c in catalogs]


@app.post("/api/catalogs", response_model=CatalogResponse)
def create_catalog(
    req: CreateCatalogRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = _resolve_workspace(req.workspace_id, current_user, db)
    if (
        db.query(Catalog)
        .filter(
            Catalog.owner_workspace_id == ws.id,
            func.lower(Catalog.name) == req.name.strip().lower(),
        )
        .first()
    ):
        raise HTTPException(409, "A catalog with that name already exists in this workspace")
    slug = _unique_catalog_slug(db, _slugify(req.name))
    catalog = Catalog(
        name=req.name.strip(),
        slug=slug,
        description=req.description,
        owner_workspace_id=ws.id,
        created_by=current_user.id,
    )
    db.add(catalog)
    db.flush()
    _seed_medallion_schemas(db, catalog)
    db.commit()
    db.refresh(catalog)
    return _catalog_response(catalog, current_user, db)


def _resolve_workspace(workspace_id: str | None, user: User, db: Session) -> Workspace:
    if not workspace_id:
        raise HTTPException(422, "workspace_id is required")
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if user.role != "superadmin":
        member = next(
            (
                m
                for m in user.workspace_memberships
                if str(m.workspace_id) == workspace_id and m.role == "admin"
            ),
            None,
        )
        if not member:
            raise HTTPException(403, "Workspace admin access required")
    return ws


@app.get("/api/catalogs/directory", response_model=list[CatalogDirectoryItem])
def catalog_directory(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    membership = next(
        (m for m in current_user.workspace_memberships if str(m.workspace_id) == workspace_id),
        None,
    )
    if not membership and current_user.role != "superadmin":
        raise HTTPException(403, "Not a member of this workspace")
    if membership and membership.role != "admin" and current_user.role != "superadmin":
        raise HTTPException(403, "Workspace admin access required")

    catalogs = (
        db.query(Catalog)
        .filter(
            Catalog.deleted_at.is_(None),
            Catalog.owner_workspace_id != workspace_id,
        )
        .order_by(Catalog.name)
        .all()
    )

    grants = db.query(CatalogAccess).filter(CatalogAccess.workspace_id == workspace_id).all()
    grant_by_catalog = {str(g.catalog_id): g for g in grants}

    result = []
    for cat in catalogs:
        owner_ws = cat.owner_workspace
        grant = grant_by_catalog.get(str(cat.id))
        if grant and grant.status == "approved":
            access_status = "approved"
        elif grant and grant.status == "pending":
            access_status = "pending"
        else:
            access_status = "none"
        result.append(
            CatalogDirectoryItem(
                id=cat.id,
                name=cat.name,
                slug=cat.slug,
                description=cat.description,
                owner_workspace_id=cat.owner_workspace_id,
                owner_workspace_name=owner_ws.name if owner_ws else None,
                schema_count=len(cat.schemas),
                access_status=access_status,
                access_id=grant.id if grant else None,
            )
        )
    return result


@app.get("/api/catalogs/{catalog_id}", response_model=CatalogResponse)
def get_catalog(
    catalog_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "read")
    return _catalog_response(catalog, current_user, db)


@app.patch("/api/catalogs/{catalog_id}", response_model=CatalogResponse)
def update_catalog(
    catalog_id: str,
    req: UpdateCatalogRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner")
    if req.name is not None:
        catalog.name = req.name.strip()
    if req.description is not None:
        catalog.description = req.description
    db.commit()
    db.refresh(catalog)
    return _catalog_response(catalog, current_user, db)


@app.delete("/api/catalogs/{catalog_id}", response_model=CatalogResponse)
def delete_catalog(
    catalog_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner")
    if catalog.is_default:
        raise HTTPException(409, "The default catalog cannot be deleted")
    if catalog.deleted_at is not None:
        raise HTTPException(409, "Catalog is already pending deletion")
    grace_days = _get_soft_delete_days(db)
    if grace_days == 0:
        raise HTTPException(
            422,
            "Soft delete is disabled — use POST /api/catalogs/{id}/purge to permanently delete",
        )

    now = datetime.now(UTC)
    catalog.deleted_at = now
    catalog.scheduled_purge_at = now + timedelta(days=grace_days)

    # Suspend all approved access grants for other workspaces
    db.query(CatalogAccess).filter(
        CatalogAccess.catalog_id == catalog.id,
        CatalogAccess.status == "approved",
    ).update({"suspended": True})

    db.commit()
    db.refresh(catalog)
    return _catalog_response(catalog, current_user, db)


@app.post("/api/catalogs/{catalog_id}/reactivate", response_model=CatalogResponse)
def reactivate_catalog(
    catalog_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner")
    if catalog.deleted_at is None:
        raise HTTPException(409, "Catalog is not pending deletion")

    catalog.deleted_at = None
    catalog.scheduled_purge_at = None

    # Restore suspended access grants
    db.query(CatalogAccess).filter(
        CatalogAccess.catalog_id == catalog.id,
        CatalogAccess.suspended.is_(True),
    ).update({"suspended": False})

    db.commit()
    db.refresh(catalog)
    return _catalog_response(catalog, current_user, db)


@app.post("/api/catalogs/{catalog_id}/purge")
def purge_catalog(
    catalog_id: str,
    req: PurgeCatalogRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner")
    if catalog.is_default:
        raise HTTPException(409, "The default catalog cannot be deleted")

    # Workspace admins can only purge soft-deleted catalogs; superadmins can purge any
    if current_user.role != "superadmin" and catalog.deleted_at is None:
        raise HTTPException(
            422, "Catalog must be in pending-deletion state before purging — delete it first"
        )

    if req.confirm_name != catalog.name:
        raise HTTPException(422, "Catalog name confirmation does not match")

    db.delete(catalog)
    db.commit()
    return {"ok": True}


# ── Schema CRUD ───────────────────────────────────────────────────────────────


@app.get("/api/catalogs/{catalog_id}/schemas", response_model=list[CatalogSchemaResponse])
def list_schemas(
    catalog_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "read")
    return [
        CatalogSchemaResponse(
            id=s.id,
            catalog_id=s.catalog_id,
            name=s.name,
            slug=s.slug,
            description=s.description,
            tier=s.tier,
            created_at=s.created_at,
            tables=[CatalogTableResponse.model_validate(t) for t in s.tables],
        )
        for s in catalog.schemas
    ]


@app.post("/api/catalogs/{catalog_id}/schemas", response_model=CatalogSchemaResponse)
def create_schema(
    catalog_id: str,
    req: CreateSchemaRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write")
    slug = _slugify(req.name)
    if db.query(CatalogSchema).filter_by(catalog_id=catalog_id, slug=slug).first():
        raise HTTPException(400, f"Schema slug '{slug}' already exists in this catalog")
    schema = CatalogSchema(
        catalog_id=catalog.id, name=req.name.strip(), slug=slug, description=req.description
    )
    db.add(schema)
    db.commit()
    db.refresh(schema)
    return CatalogSchemaResponse(
        id=schema.id,
        catalog_id=schema.catalog_id,
        name=schema.name,
        slug=schema.slug,
        description=schema.description,
        tier=schema.tier,
        created_at=schema.created_at,
        tables=[],
    )


@app.patch("/api/catalogs/{catalog_id}/schemas/{schema_id}", response_model=CatalogSchemaResponse)
def update_schema(
    catalog_id: str,
    schema_id: str,
    req: UpdateSchemaRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write")
    schema = db.query(CatalogSchema).filter_by(id=schema_id, catalog_id=catalog_id).first()
    if not schema:
        raise HTTPException(404, "Schema not found")
    if schema.tier in _MEDALLION_TIERS:
        raise HTTPException(400, f"Cannot modify the locked '{schema.tier}' medallion schema")
    if req.description is not None:
        schema.description = req.description
    db.commit()
    db.refresh(schema)
    return CatalogSchemaResponse(
        id=schema.id,
        catalog_id=schema.catalog_id,
        name=schema.name,
        slug=schema.slug,
        description=schema.description,
        tier=schema.tier,
        created_at=schema.created_at,
        tables=[CatalogTableResponse.model_validate(t) for t in schema.tables],
    )


@app.delete("/api/catalogs/{catalog_id}/schemas/{schema_id}")
def delete_schema(
    catalog_id: str,
    schema_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner")
    schema = db.query(CatalogSchema).filter_by(id=schema_id, catalog_id=catalog_id).first()
    if not schema:
        raise HTTPException(404, "Schema not found")
    if schema.tier in _MEDALLION_TIERS:
        raise HTTPException(400, f"Cannot delete the locked '{schema.tier}' medallion schema")
    db.delete(schema)
    db.commit()
    return {"ok": True}


# ── Table CRUD ────────────────────────────────────────────────────────────────


@app.get(
    "/api/catalogs/{catalog_id}/schemas/{schema_id}/tables",
    response_model=list[CatalogTableResponse],
)
def list_tables(
    catalog_id: str,
    schema_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "read")
    schema = db.query(CatalogSchema).filter_by(id=schema_id, catalog_id=catalog_id).first()
    if not schema:
        raise HTTPException(404, "Schema not found")
    return [CatalogTableResponse.model_validate(t) for t in schema.tables]


@app.post(
    "/api/catalogs/{catalog_id}/schemas/{schema_id}/tables", response_model=CatalogTableResponse
)
def create_table(
    catalog_id: str,
    schema_id: str,
    req: CreateTableRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write")
    schema = db.query(CatalogSchema).filter_by(id=schema_id, catalog_id=catalog_id).first()
    if not schema:
        raise HTTPException(404, "Schema not found")
    slug = _slugify(req.name)
    if db.query(CatalogTable).filter_by(schema_id=schema_id, slug=slug).first():
        raise HTTPException(400, f"Table slug '{slug}' already exists in this schema")
    table = CatalogTable(
        schema_id=schema.id,
        name=req.name.strip(),
        slug=slug,
        description=req.description,
        s3_path_pattern=req.s3_path_pattern,
        column_defs=[],
    )
    db.add(table)
    db.commit()
    db.refresh(table)
    return CatalogTableResponse.model_validate(table)


@app.patch(
    "/api/catalogs/{catalog_id}/schemas/{schema_id}/tables/{table_id}",
    response_model=CatalogTableResponse,
)
def update_table(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    req: UpdateTableRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write")
    table = db.query(CatalogTable).filter_by(id=table_id, schema_id=schema_id).first()
    if not table:
        raise HTTPException(404, "Table not found")
    if req.description is not None:
        table.description = req.description
    if req.s3_path_pattern is not None:
        table.s3_path_pattern = req.s3_path_pattern
    if req.column_defs is not None:
        table.column_defs = [c.model_dump() for c in req.column_defs]
    db.commit()
    db.refresh(table)
    return CatalogTableResponse.model_validate(table)


@app.delete("/api/catalogs/{catalog_id}/schemas/{schema_id}/tables/{table_id}")
def delete_table(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write")
    table = db.query(CatalogTable).filter_by(id=table_id, schema_id=schema_id).first()
    if not table:
        raise HTTPException(404, "Table not found")
    db.delete(table)
    db.commit()
    return {"ok": True}


# ── Access management ─────────────────────────────────────────────────────────


@app.get("/api/catalogs/{catalog_id}/access", response_model=list[CatalogAccessResponse])
def list_access(
    catalog_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner")
    grants = db.query(CatalogAccess).filter(CatalogAccess.catalog_id == catalog_id).all()
    result = []
    for g in grants:
        ws = db.query(Workspace).filter(Workspace.id == g.workspace_id).first()
        requester = (
            db.query(User).filter(User.id == g.requested_by).first() if g.requested_by else None
        )
        result.append(
            CatalogAccessResponse(
                id=g.id,
                catalog_id=g.catalog_id,
                workspace_id=g.workspace_id,
                workspace_name=ws.name if ws else None,
                mode=g.mode,
                status=g.status,
                requested_by_email=requester.email if requester else None,
                requested_at=g.requested_at,
                reviewed_at=g.reviewed_at,
            )
        )
    return result


@app.post("/api/catalogs/{catalog_id}/access/request")
def request_access(
    catalog_id: str,
    req: RequestAccessRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")

    # caller must be a workspace admin in the given workspace (or superadmin)
    if req.workspace_id:
        membership = next(
            (
                m
                for m in current_user.workspace_memberships
                if str(m.workspace_id) == req.workspace_id and m.role == "admin"
            ),
            None,
        )
        if not membership and current_user.role != "superadmin":
            raise HTTPException(403, "Workspace admin access required to request catalog access")
        ws_id = membership.workspace_id if membership else req.workspace_id
    else:
        admin_membership = next(
            (m for m in current_user.workspace_memberships if m.role == "admin"), None
        )
        if not admin_membership and current_user.role != "superadmin":
            raise HTTPException(403, "Workspace admin access required to request catalog access")
        ws_id = admin_membership.workspace_id if admin_membership else catalog.owner_workspace_id
    if str(ws_id) == str(catalog.owner_workspace_id):
        raise HTTPException(400, "Owner workspace already has full access")

    existing = db.query(CatalogAccess).filter_by(catalog_id=catalog_id, workspace_id=ws_id).first()
    if existing:
        if existing.status == "approved":
            raise HTTPException(400, "Access already approved")
        existing.mode = req.mode
        existing.status = "pending"
        existing.requested_by = current_user.id
        existing.requested_at = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
    else:
        db.add(
            CatalogAccess(
                catalog_id=catalog.id,
                workspace_id=ws_id,
                mode=req.mode,
                status="pending",
                requested_by=current_user.id,
            )
        )
    db.commit()

    owner_emails = _owner_emails(db, catalog)
    ws = db.query(Workspace).filter(Workspace.id == ws_id).first()
    threading.Thread(
        target=send_access_requested,
        args=(owner_emails, ws.name if ws else "Unknown", catalog.name, req.mode),
        daemon=True,
    ).start()

    return {"ok": True}


@app.patch("/api/catalogs/{catalog_id}/access/{access_id}")
def review_access(
    catalog_id: str,
    access_id: str,
    req: ReviewAccessRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner")

    grant = db.query(CatalogAccess).filter_by(id=access_id, catalog_id=catalog_id).first()
    if not grant:
        raise HTTPException(404, "Access grant not found")

    grant.status = req.status
    grant.reviewed_by = current_user.id
    from datetime import datetime

    grant.reviewed_at = datetime.now(UTC)
    db.commit()

    requester = (
        db.query(User).filter(User.id == grant.requested_by).first() if grant.requested_by else None
    )
    if requester:
        threading.Thread(
            target=send_access_reviewed,
            args=(requester.email, catalog.name, req.status),
            daemon=True,
        ).start()

    return {"ok": True}


@app.delete("/api/catalogs/{catalog_id}/access/{access_id}")
def revoke_access(
    catalog_id: str,
    access_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    catalog = db.query(Catalog).filter(Catalog.id == catalog_id).first()
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner")
    grant = db.query(CatalogAccess).filter_by(id=access_id, catalog_id=catalog_id).first()
    if not grant:
        raise HTTPException(404, "Access grant not found")
    db.delete(grant)
    db.commit()
    return {"ok": True}


@app.get(
    "/api/workspaces/{workspace_id}/access/requests/inbound",
    response_model=list[InboundAccessRequest],
)
def inbound_access_requests(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    membership = next(
        (m for m in current_user.workspace_memberships if str(m.workspace_id) == workspace_id),
        None,
    )
    if not membership and current_user.role != "superadmin":
        raise HTTPException(403, "Not a member of this workspace")
    if membership and membership.role != "admin" and current_user.role != "superadmin":
        raise HTTPException(403, "Workspace admin access required")

    owned_catalog_ids = db.query(Catalog.id).filter(
        Catalog.owner_workspace_id == workspace_id,
        Catalog.deleted_at.is_(None),
    )
    grants = (
        db.query(CatalogAccess)
        .filter(
            CatalogAccess.catalog_id.in_(owned_catalog_ids),
            CatalogAccess.status == "pending",
        )
        .all()
    )

    result = []
    for g in grants:
        cat = db.query(Catalog).filter(Catalog.id == g.catalog_id).first()
        req_ws = db.query(Workspace).filter(Workspace.id == g.workspace_id).first()
        result.append(
            InboundAccessRequest(
                access_id=g.id,
                catalog_id=g.catalog_id,
                catalog_name=cat.name if cat else "—",
                requesting_workspace_id=g.workspace_id,
                requesting_workspace_name=req_ws.name if req_ws else None,
                mode=g.mode,
                requested_at=g.requested_at,
            )
        )
    return result


@app.get("/api/workspaces/{workspace_id}/catalogs", response_model=list[CatalogResponse])
def workspace_catalogs(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import or_

    shared_ids = db.query(CatalogAccess.catalog_id).filter(
        CatalogAccess.workspace_id == workspace_id,
        CatalogAccess.status == "approved",
        CatalogAccess.suspended.is_(False),
    )
    catalogs = (
        db.query(Catalog)
        .filter(
            or_(Catalog.owner_workspace_id == workspace_id, Catalog.id.in_(shared_ids)),
            Catalog.deleted_at.is_(None),
        )
        .order_by(Catalog.created_at)
        .all()
    )
    return [_catalog_response(c, current_user, db) for c in catalogs]


@app.get(
    "/api/workspaces/{workspace_id}/settings/catalogs",
    response_model=WorkspaceCatalogSettingsResponse,
)
def workspace_catalog_settings(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if current_user.role != "superadmin":
        member = next(
            (m for m in current_user.workspace_memberships if str(m.workspace_id) == workspace_id),
            None,
        )
        if not member:
            raise HTTPException(403, "Not a member of this workspace")

    owned = (
        db.query(Catalog)
        .filter(Catalog.owner_workspace_id == workspace_id)
        .order_by(Catalog.deleted_at.is_(None).desc(), Catalog.name)
        .all()
    )

    access_grants = (
        db.query(CatalogAccess)
        .filter(
            CatalogAccess.workspace_id == workspace_id,
            CatalogAccess.status.in_(["pending", "approved"]),
        )
        .all()
    )
    shared = []
    for grant in access_grants:
        cat = grant.catalog
        owner_ws = cat.owner_workspace if cat else None
        shared.append(
            SharedCatalogSettingsItem(
                access_id=grant.id,
                catalog_id=grant.catalog_id,
                catalog_name=cat.name if cat else "—",
                owner_workspace_id=grant.catalog.owner_workspace_id if cat else workspace_id,
                owner_workspace_name=owner_ws.name if owner_ws else None,
                status=grant.status,
                suspended=grant.suspended,
            )
        )

    return WorkspaceCatalogSettingsResponse(
        owned=[_catalog_response(c, current_user, db) for c in owned],
        shared=shared,
    )


@app.delete("/api/workspaces/{workspace_id}/catalogs/{catalog_id}/shared")
def remove_shared_catalog(
    workspace_id: str,
    catalog_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if current_user.role != "superadmin":
        member = next(
            (
                m
                for m in current_user.workspace_memberships
                if str(m.workspace_id) == workspace_id and m.role == "admin"
            ),
            None,
        )
        if not member:
            raise HTTPException(403, "Workspace admin access required")

    grant = (
        db.query(CatalogAccess)
        .filter(
            CatalogAccess.workspace_id == workspace_id,
            CatalogAccess.catalog_id == catalog_id,
        )
        .first()
    )
    if not grant:
        raise HTTPException(404, "Shared catalog not found")

    catalog = grant.catalog
    catalog_name = catalog.name if catalog else "Unknown"
    owner_emails = _owner_emails(db, catalog) if catalog else []
    removing_ws_name = ws.name

    db.delete(grant)
    db.commit()

    threading.Thread(
        target=send_access_removed,
        args=(owner_emails, removing_ws_name, catalog_name),
        daemon=True,
    ).start()

    return {"ok": True}


# ── Admin: Workspaces ─────────────────────────────────────────────────────────


@app.get("/api/admin/workspaces", response_model=list[WorkspaceAdminResponse])
def list_admin_workspaces(
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
):
    workspaces = db.query(Workspace).order_by(Workspace.created_at).all()
    return [_workspace_admin_response(ws, db) for ws in workspaces]


def _soft_delete_workspace_catalogs(db: Session, ws: Workspace, purge_at: datetime) -> None:
    catalogs = (
        db.query(Catalog)
        .filter(Catalog.owner_workspace_id == ws.id, Catalog.deleted_at.is_(None))
        .all()
    )
    now = datetime.now(UTC)
    for cat in catalogs:
        cat.deleted_at = now
        cat.scheduled_purge_at = purge_at
        for grant in cat.access_grants:
            grant.suspended = True


def _reactivate_workspace_catalogs(db: Session, ws: Workspace) -> None:
    catalogs = (
        db.query(Catalog)
        .filter(Catalog.owner_workspace_id == ws.id, Catalog.deleted_at.isnot(None))
        .all()
    )
    for cat in catalogs:
        cat.deleted_at = None
        cat.scheduled_purge_at = None
        for grant in cat.access_grants:
            grant.suspended = False


@app.delete("/api/admin/workspaces/{workspace_id}", status_code=204)
def admin_delete_workspace(
    workspace_id: str,
    req: DeleteWorkspaceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if ws.is_system:
        raise HTTPException(409, "The system workspace cannot be deleted")

    if req.mode == "hard":
        if not req.confirm_name or req.confirm_name != ws.name:
            raise HTTPException(400, "Workspace name confirmation does not match")
        db.delete(ws)
        db.commit()
        return

    # Soft delete
    grace_days = _get_workspace_inactive_days(db)
    now = datetime.now(UTC)
    purge_at = now + timedelta(days=grace_days)
    ws.status = "inactive"
    ws.deleted_at = now
    ws.scheduled_purge_at = purge_at
    _soft_delete_workspace_catalogs(db, ws, purge_at)
    db.commit()


@app.post("/api/admin/workspaces/{workspace_id}/restore", response_model=WorkspaceAdminResponse)
def restore_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
):
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if ws.status != "inactive":
        raise HTTPException(409, "Workspace is not inactive")
    ws.status = "active"
    ws.deleted_at = None
    ws.scheduled_purge_at = None
    _reactivate_workspace_catalogs(db, ws)
    db.commit()
    db.refresh(ws)
    return _workspace_admin_response(ws, db)


# ── Admin: System Settings ────────────────────────────────────────────────────


@app.get("/api/admin/settings/public")
def get_public_settings(db: Session = Depends(get_db)):
    """Unauthenticated endpoint — returns only session config for the UI middleware."""
    return {
        "session_inactivity_timeout_minutes": _get_session_inactivity_minutes(db),
        "session_max_hours": _get_session_max_hours(db),
    }


@app.get("/api/admin/settings", response_model=SystemSettingResponse)
def get_admin_settings(
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
):
    return SystemSettingResponse(
        catalog_soft_delete_days=_get_soft_delete_days(db),
        workspace_inactive_grace_period_days=_get_workspace_inactive_days(db),
        session_inactivity_timeout_minutes=_get_session_inactivity_minutes(db),
        session_max_hours=_get_session_max_hours(db),
    )


def _upsert_setting(db: Session, key: str, value: int, user_id) -> None:
    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if setting:
        setting.value = value
        setting.updated_at = datetime.now(UTC)
        setting.updated_by = user_id
    else:
        db.add(SystemSetting(key=key, value=value, updated_by=user_id))


@app.patch("/api/admin/settings", response_model=SystemSettingResponse)
def update_admin_settings(
    req: UpdateSystemSettingsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
):
    if req.catalog_soft_delete_days is not None:
        _upsert_setting(
            db, "catalog_soft_delete_days", req.catalog_soft_delete_days, current_user.id
        )
    if req.workspace_inactive_grace_period_days is not None:
        _upsert_setting(
            db,
            "workspace_inactive_grace_period_days",
            req.workspace_inactive_grace_period_days,
            current_user.id,
        )
    if req.session_inactivity_timeout_minutes is not None:
        _upsert_setting(
            db,
            "session_inactivity_timeout_minutes",
            req.session_inactivity_timeout_minutes,
            current_user.id,
        )
    if req.session_max_hours is not None:
        _upsert_setting(db, "session_max_hours", req.session_max_hours, current_user.id)
    db.commit()
    return SystemSettingResponse(
        catalog_soft_delete_days=_get_soft_delete_days(db),
        workspace_inactive_grace_period_days=_get_workspace_inactive_days(db),
        session_inactivity_timeout_minutes=_get_session_inactivity_minutes(db),
        session_max_hours=_get_session_max_hours(db),
    )
