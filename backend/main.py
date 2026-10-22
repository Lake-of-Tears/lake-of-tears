from __future__ import annotations

import hmac
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import ai_service
import jwt as _jwt
from ai_admin_router import router as ai_admin_router
from ai_gateway_router import router as ai_gateway_router
from ai_router import router as ai_router
from auth import (
    COOKIE_NAME,
    SECRET_KEY,
    create_token,
    decode_token,
    get_catalog_workspace_and_role,
    get_current_user,
    get_session_expires_delta,
    get_stored_gemini_api_key,
    get_stored_gemini_model,
    hash_password,
    mask_secret,
    require_superadmin,
    set_stored_gemini_api_key,
    set_stored_gemini_model,
    verify_password,
)
from catalog_storage import delete_prefix, prefixes_to_delete
from dashboards_router import router as dashboards_router
from database import get_db
from email_service import send_access_removed, send_access_requested, send_access_reviewed
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
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
    WorkspaceShare,
)
from notebooks_router import router as notebooks_router
from oauth import enabled_providers, get_oauth_redirect, handle_oauth_callback
from pipelines_router import router as pipelines_router
from schemas import (
    AddMemberRequest,
    CandidateMemberResponse,
    CatalogAccessResponse,
    CatalogDirectoryItem,
    CatalogResponse,
    CatalogSchemaResponse,
    CatalogTableResponse,
    ChangePasswordRequest,
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
    TestGeminiKeyRequest,
    TestGeminiKeyResponse,
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
    WorkspaceShareRequest,
    WorkspaceShareResponse,
    WorkspaceShareReviewRequest,
)
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from storage_router import router as storage_router

_DEFAULT_SOFT_DELETE_DAYS = 30
_DEFAULT_WORKSPACE_INACTIVE_DAYS = 30
_DEFAULT_SESSION_INACTIVITY_TIMEOUT_MINUTES = 0
_DEFAULT_SESSION_MAX_HOURS = 24


@asynccontextmanager
async def lifespan(app: FastAPI):
    from database import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _backfill_default_catalogs(db)
        _ensure_system_settings(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="Lake of Tears Auth API",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("AUTH_BASE_URL", "http://localhost")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(notebooks_router, prefix="/api/v1/notebooks", tags=["notebooks"])
app.include_router(notebooks_router, prefix="/api/notebooks", tags=["notebooks"])
app.include_router(pipelines_router, prefix="/api/v1/pipelines", tags=["pipelines"])
app.include_router(pipelines_router, prefix="/api/pipelines", tags=["pipelines"])
app.include_router(storage_router, prefix="/api/v1/storage", tags=["storage"])
app.include_router(storage_router, prefix="/api/storage", tags=["storage"])
app.include_router(dashboards_router, prefix="/api/v1/dashboards", tags=["dashboards"])
app.include_router(dashboards_router, prefix="/api/dashboards", tags=["dashboards"])
app.include_router(ai_router, prefix="/api/v1/ai", tags=["ai"])
app.include_router(ai_router, prefix="/api/ai", tags=["ai"])
app.include_router(ai_gateway_router, prefix="/api/internal/ai", tags=["ai-internal"])
app.include_router(ai_admin_router, prefix="/api/admin/ai-provider", tags=["ai-admin"])


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


def _as_uuid(val: Any) -> uuid.UUID | None:
    if isinstance(val, uuid.UUID):
        return val
    if not val:
        return None
    try:
        return uuid.UUID(str(val))
    except (ValueError, TypeError, AttributeError):
        return None


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
    ws = Workspace(
        name="Default",
        slug=slug,
        description="Default workspace",
        created_by=user.id,
        is_system=True,
    )
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
) -> str:
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
    ws = (
        db.query(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .filter(WorkspaceMember.user_id == user.id, Workspace.status == "active")
        .order_by(Workspace.created_at)
        .first()
    )
    if not ws and user.role == "superadmin":
        ws = (
            db.query(Workspace)
            .filter(Workspace.status == "active")
            .order_by(Workspace.created_at)
            .first()
        )
    if ws:
        response.set_cookie("lake_workspace_id", str(ws.id), max_age=max_age, samesite="lax")
    return token


def _workspace_response(ws: Workspace, user: User, is_shared: bool = False) -> WorkspaceResponse:
    member = next((m for m in ws.members if m.user_id == user.id), None)
    role = (
        member.role
        if member
        else ("superadmin" if user.role == "superadmin" else ("read" if is_shared else None))
    )
    return WorkspaceResponse(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        description=ws.description,
        created_at=ws.created_at,
        is_system=ws.is_system,
        status=ws.status,
        my_role=role,
        is_shared=is_shared,
    )


def _workspace_share_response(
    share: WorkspaceShare, ws: Workspace | None = None, user: User | None = None
) -> WorkspaceShareResponse:
    target_ws = ws or share.workspace
    target_user = user or share.user
    return WorkspaceShareResponse(
        id=share.id,
        workspace_id=share.workspace_id,
        workspace_name=target_ws.name if target_ws else "",
        workspace_slug=target_ws.slug if target_ws else "",
        user_id=share.user_id,
        user_email=target_user.email if target_user else "",
        user_display_name=target_user.display_name if target_user else None,
        status=share.status,
        requested_at=share.requested_at,
        approved_at=share.approved_at,
        approved_by=share.approved_by,
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
            db.add(WorkspaceMember(workspace_id=default_ws.id, user_id=user.id, role="read"))

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

    sub_val = payload.get("sub")
    try:
        user_uuid = uuid.UUID(str(sub_val))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(401, "Invalid token")

    user = db.query(User).filter(User.id == user_uuid).first()
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


@app.post("/api/auth/change-password")
def change_password(
    req: ChangePasswordRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.hashed_password:
        raise HTTPException(
            400,
            "Password changes are not available for accounts managed via SSO/OAuth.",
        )
    if not verify_password(req.current_password, current_user.hashed_password):
        raise HTTPException(400, "Incorrect current password")

    current_user.hashed_password = hash_password(req.new_password)
    db.commit()
    db.refresh(current_user)
    token = _set_auth_cookie(response, current_user, db)
    return {"ok": True, "token": token}


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
        return [_workspace_response(ws, current_user, is_shared=False) for ws in workspaces]
    else:
        member_workspaces = (
            db.query(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .filter(WorkspaceMember.user_id == current_user.id, Workspace.status == "active")
            .order_by(Workspace.created_at)
            .all()
        )
        member_ws_ids = {ws.id for ws in member_workspaces}

        shared_workspaces = (
            db.query(Workspace)
            .join(WorkspaceShare, WorkspaceShare.workspace_id == Workspace.id)
            .filter(
                WorkspaceShare.user_id == current_user.id,
                WorkspaceShare.status == "approved",
                Workspace.status == "active",
            )
            .order_by(Workspace.created_at)
            .all()
        )
        shared_workspaces = [ws for ws in shared_workspaces if ws.id not in member_ws_ids]

        res = [_workspace_response(ws, current_user, is_shared=False) for ws in member_workspaces]
        res.extend(
            [_workspace_response(ws, current_user, is_shared=True) for ws in shared_workspaces]
        )
        return res


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
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    # check membership or approved share unless superadmin
    is_shared = False
    if current_user.role != "superadmin":
        member = next((m for m in ws.members if m.user_id == current_user.id), None)
        if not member:
            share = (
                db.query(WorkspaceShare)
                .filter(
                    WorkspaceShare.workspace_id == ws.id,
                    WorkspaceShare.user_id == current_user.id,
                    WorkspaceShare.status == "approved",
                )
                .first()
            )
            if not share:
                raise HTTPException(403, "Not a member of this workspace")
            is_shared = True
    return _workspace_response(ws, current_user, is_shared=is_shared)


@app.patch("/api/workspaces/{workspace_id}", response_model=WorkspaceResponse)
def update_workspace(
    workspace_id: str,
    req: UpdateWorkspaceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
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


# ── Workspace members ─────────────────────────────────────────────────────────


@app.get("/api/workspaces/{workspace_id}/members", response_model=list[WorkspaceMemberResponse])
def list_members(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
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
            role="admin" if m.user.role == "superadmin" else m.role,
            global_role=m.user.role,
            is_superadmin=(m.user.role == "superadmin"),
            joined_at=m.joined_at,
        )
        for m in ws.members
    ]


@app.get(
    "/api/workspaces/{workspace_id}/candidate-members",
    response_model=list[CandidateMemberResponse],
)
def list_candidate_members(
    workspace_id: str,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import or_

    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if current_user.role != "superadmin":
        member_check = next(
            (m for m in ws.members if m.user_id == current_user.id and m.role == "admin"), None
        )
        if not member_check:
            raise HTTPException(403, "Workspace admin access required")

    existing_member_ids = [m.user_id for m in ws.members]

    query = db.query(User).filter(User.is_active.is_(True))
    if existing_member_ids:
        query = query.filter(~User.id.in_(existing_member_ids))

    if q and q.strip():
        search = f"%{q.strip()}%"
        query = query.filter(or_(User.email.ilike(search), User.display_name.ilike(search)))

    users = query.order_by(User.email.asc()).limit(limit).all()
    return [CandidateMemberResponse.model_validate(u) for u in users]


@app.post("/api/workspaces/{workspace_id}/members")
def add_member(
    workspace_id: str,
    req: AddMemberRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
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

    role_to_set = "admin" if target.role == "superadmin" else req.role
    existing = db.query(WorkspaceMember).filter_by(workspace_id=ws.id, user_id=target.id).first()
    if existing:
        existing.role = role_to_set
    else:
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=target.id, role=role_to_set))
    # Automatically delete/supersede any existing WorkspaceShare for this user on this workspace
    db.query(WorkspaceShare).filter_by(workspace_id=ws.id, user_id=target.id).delete()
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
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
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
    if member.user.role == "superadmin":
        raise HTTPException(400, "Cannot change the workspace role of a superadmin")
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
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
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
    if member.user.role == "superadmin":
        raise HTTPException(400, "Cannot remove a superadmin from a workspace")
    db.delete(member)
    db.commit()
    return {"ok": True}


# ── Workspace Shares ──────────────────────────────────────────────────────────


@app.get("/api/workspaces/shares/mine", response_model=list[WorkspaceShareResponse])
def list_my_workspace_shares(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    shares = (
        db.query(WorkspaceShare)
        .filter(WorkspaceShare.user_id == current_user.id)
        .order_by(WorkspaceShare.requested_at.desc())
        .all()
    )
    return [_workspace_share_response(s) for s in shares]


@app.post("/api/workspaces/{workspace_id}/shares", response_model=WorkspaceShareResponse)
def request_workspace_share(
    workspace_id: str,
    req: WorkspaceShareRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid, Workspace.status == "active").first()
    if not ws:
        raise HTTPException(404, "Workspace not found")

    # Member check
    member = (
        db.query(WorkspaceMember).filter_by(workspace_id=ws.id, user_id=current_user.id).first()
    )
    if member:
        raise HTTPException(400, "You are already a direct member of this workspace")

    existing_share = (
        db.query(WorkspaceShare).filter_by(workspace_id=ws.id, user_id=current_user.id).first()
    )
    if existing_share:
        if existing_share.status == "pending":
            raise HTTPException(409, "A share request is already pending for this workspace")
        if existing_share.status == "approved":
            raise HTTPException(409, "Workspace share is already approved")
        if existing_share.status == "rejected":
            existing_share.status = "pending"
            existing_share.requested_at = datetime.now(UTC)
            existing_share.approved_at = None
            existing_share.approved_by = None
            db.commit()
            db.refresh(existing_share)
            return _workspace_share_response(existing_share, ws, current_user)

    share = WorkspaceShare(
        workspace_id=ws.id,
        user_id=current_user.id,
        status="pending",
        requested_at=datetime.now(UTC),
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return _workspace_share_response(share, ws, current_user)


@app.get(
    "/api/workspaces/{workspace_id}/shares",
    response_model=list[WorkspaceShareResponse],
)
def list_workspace_shares(
    workspace_id: str,
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")

    if current_user.role != "superadmin":
        admin_check = next(
            (m for m in ws.members if m.user_id == current_user.id and m.role == "admin"),
            None,
        )
        if not admin_check:
            raise HTTPException(403, "Workspace admin access required")

    query = db.query(WorkspaceShare).filter(WorkspaceShare.workspace_id == ws.id)
    if status:
        query = query.filter(WorkspaceShare.status == status)

    shares = query.order_by(WorkspaceShare.requested_at.desc()).all()
    return [_workspace_share_response(s, ws) for s in shares]


@app.patch(
    "/api/workspaces/{workspace_id}/shares/{share_id}",
    response_model=WorkspaceShareResponse,
)
def review_workspace_share(
    workspace_id: str,
    share_id: str,
    req: WorkspaceShareReviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")

    if current_user.role != "superadmin":
        admin_check = next(
            (m for m in ws.members if m.user_id == current_user.id and m.role == "admin"),
            None,
        )
        if not admin_check:
            raise HTTPException(403, "Workspace admin access required")

    share_uuid = _as_uuid(share_id)
    if not share_uuid:
        raise HTTPException(404, "Workspace share request not found")

    share = (
        db.query(WorkspaceShare)
        .filter(WorkspaceShare.id == share_uuid, WorkspaceShare.workspace_id == ws.id)
        .first()
    )
    if not share:
        raise HTTPException(404, "Workspace share request not found")

    share.status = req.status
    share.approved_at = datetime.now(UTC)
    share.approved_by = current_user.id
    db.commit()
    db.refresh(share)
    return _workspace_share_response(share, ws)


@app.delete(
    "/api/workspaces/{workspace_id}/shares",
    status_code=status.HTTP_204_NO_CONTENT,
)
def disconnect_workspace_share(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")

    share = (
        db.query(WorkspaceShare)
        .filter(
            WorkspaceShare.workspace_id == ws.id,
            WorkspaceShare.user_id == current_user.id,
        )
        .first()
    )
    if not share:
        raise HTTPException(404, "No workspace share found for this workspace")

    db.delete(share)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.delete(
    "/api/workspaces/{workspace_id}/shares/{share_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_workspace_share_by_id(
    workspace_id: str,
    share_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_uuid = _as_uuid(workspace_id)
    if not ws_uuid:
        raise HTTPException(404, "Workspace not found")
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first()
    if not ws:
        raise HTTPException(404, "Workspace not found")

    share_uuid = _as_uuid(share_id)
    if not share_uuid:
        raise HTTPException(404, "Workspace share request not found")

    share = (
        db.query(WorkspaceShare)
        .filter(WorkspaceShare.id == share_uuid, WorkspaceShare.workspace_id == ws.id)
        .first()
    )
    if not share:
        raise HTTPException(404, "Workspace share not found")

    if current_user.role != "superadmin" and share.user_id != current_user.id:
        admin_check = next(
            (m for m in ws.members if m.user_id == current_user.id and m.role == "admin"),
            None,
        )
        if not admin_check:
            raise HTTPException(403, "Workspace admin access required")

    db.delete(share)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ── Catalog helpers ───────────────────────────────────────────────────────────

_MEDALLION_TIERS = ("bronze", "silver", "gold")
_SCHEMA_TIER_ORDER = {t: i for i, t in enumerate(_MEDALLION_TIERS, start=1)}


def _schema_sort_key(schema: Any) -> tuple[int, str]:
    tier = (
        getattr(schema, "tier", None)
        or (schema.get("tier") if isinstance(schema, dict) else None)
        or getattr(schema, "slug", None)
        or (schema.get("slug") if isinstance(schema, dict) else None)
        or ""
    ).lower()
    name = (
        getattr(schema, "name", None)
        or (schema.get("name") if isinstance(schema, dict) else None)
        or getattr(schema, "slug", None)
        or (schema.get("slug") if isinstance(schema, dict) else None)
        or ""
    ).lower()
    return (_SCHEMA_TIER_ORDER.get(tier, 99), name)


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


def _caller_access(
    db: Session,
    catalog: Catalog,
    user: User,
    ws: Workspace | None = None,
) -> str | None:
    """Determine caller's access level ('owner', 'write', 'read', or None) to a catalog.

    When `ws` is provided (active workspace context), access is strictly jailed to that workspace:
    - If active workspace owns the catalog:
      - superadmin -> 'owner'
      - workspace admin/owner -> 'owner'
      - workspace member/write/user -> 'write'
      - workspace viewer/read -> 'read'
    - If active workspace does NOT own the catalog:
      - Checks approved, non-suspended CatalogAccess grant specifically for `ws.id`
      - If no approved grant exists -> returns None (catalog is invisible in this workspace)
      - If grant exists -> grant.mode (e.g. 'read')
    """
    if ws is not None:
        if str(catalog.owner_workspace_id) == str(ws.id):
            if user.role == "superadmin":
                return "owner"
            member = next(
                (m for m in user.workspace_memberships if str(m.workspace_id) == str(ws.id)),
                None,
            )
            if not member:
                share = (
                    db.query(WorkspaceShare)
                    .filter(
                        WorkspaceShare.workspace_id == ws.id,
                        WorkspaceShare.user_id == user.id,
                        WorkspaceShare.status == "approved",
                    )
                    .first()
                )
                if share:
                    return "read"
                return None
            role = member.role.lower() if member.role else "read"
            if role in ("admin", "owner"):
                return "owner"
            if role in ("write", "user", "member"):
                return "write"
            return "read"

        # Cross-workspace: strictly verify approved sharing grant for active workspace
        grant = (
            db.query(CatalogAccess)
            .filter(
                CatalogAccess.catalog_id == catalog.id,
                CatalogAccess.workspace_id == ws.id,
                CatalogAccess.status == "approved",
                CatalogAccess.suspended.is_(False),
            )
            .first()
        )
        return grant.mode if grant else None

    # Fallback when ws is not provided
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
            CatalogAccess.suspended.is_(False),
        )
        .first()
    )
    return grant.mode if grant else None


def _assert_catalog_access(
    db: Session,
    catalog: Catalog,
    user: User,
    require: str = "read",
    ws: Workspace | None = None,
) -> str:
    access = _caller_access(db, catalog, user, ws=ws)
    if access is None:
        # Strict invisibility outside active workspace / approved shares
        raise HTTPException(404, "Catalog not found")
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
    setting = db.query(SystemSetting).filter(SystemSetting.key == "session_max_hours").first()
    if setting is not None and setting.value is not None:
        return setting.value
    return _DEFAULT_SESSION_MAX_HOURS


def _ensure_system_settings(db: Session) -> None:
    try:
        db.execute(text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS value_str TEXT"))
        db.commit()
    except Exception:
        db.rollback()
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


def _catalog_response(
    catalog: Catalog,
    user: User,
    db: Session,
    ws: Workspace | None = None,
) -> CatalogResponse:
    access = _caller_access(db, catalog, user, ws=ws)
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
            for s in sorted(catalog.schemas, key=_schema_sort_key)
        ],
        my_access=access,
    )


def _as_uuid(val: Any) -> uuid.UUID | None:
    if isinstance(val, uuid.UUID):
        return val
    if not val:
        return None
    try:
        return uuid.UUID(str(val))
    except (ValueError, TypeError, AttributeError):
        return None


def _extract_ws(ws_ctx: Any) -> Workspace | None:
    if isinstance(ws_ctx, tuple) and len(ws_ctx) == 2 and ws_ctx[0] is not None:
        return ws_ctx[0]
    return None


# ── Catalog CRUD ──────────────────────────────────────────────────────────────


@app.get("/api/catalogs", response_model=list[CatalogResponse])
def list_catalogs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    if ws is None:
        return []
    from sqlalchemy import or_

    shared_ids = db.query(CatalogAccess.catalog_id).filter(
        CatalogAccess.workspace_id == ws.id,
        CatalogAccess.status == "approved",
        CatalogAccess.suspended.is_(False),
    )
    catalogs = (
        db.query(Catalog)
        .filter(
            or_(Catalog.owner_workspace_id == ws.id, Catalog.id.in_(shared_ids)),
            Catalog.deleted_at.is_(None),
        )
        .order_by(Catalog.created_at)
        .all()
    )
    return [_catalog_response(c, current_user, db, ws=ws) for c in catalogs]


def _catalog_manifest_projection(catalogs: list[Catalog]) -> list[dict[str, Any]]:
    result = []
    for cat in catalogs:
        schemas = []
        for s in sorted(cat.schemas, key=_schema_sort_key):
            tables = []
            for t in s.tables:
                cols = [
                    c.get("name")
                    for c in (t.column_defs or [])
                    if isinstance(c, dict) and c.get("name")
                ]
                tables.append(
                    {
                        "id": str(t.id),
                        "name": t.name,
                        "slug": t.slug,
                        "description": t.description or "",
                        "s3_path_pattern": t.s3_path_pattern or "",
                        "columns": cols,
                    }
                )
            schemas.append(
                {
                    "id": str(s.id),
                    "catalog_id": str(s.catalog_id),
                    "name": s.name,
                    "slug": s.slug,
                    "description": s.description or "",
                    "tier": s.tier or "custom",
                    "tables": tables,
                }
            )
        result.append(
            {
                "id": str(cat.id),
                "name": cat.name,
                "slug": cat.slug,
                "description": cat.description or "",
                "schemas": schemas,
            }
        )
    return result


@app.get("/api/catalogs/catalog-manifest")
@app.get("/api/catalogs/manifest")
def catalogs_manifest(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    if ws is None:
        return []
    from sqlalchemy import or_

    shared_ids = db.query(CatalogAccess.catalog_id).filter(
        CatalogAccess.workspace_id == ws.id,
        CatalogAccess.status == "approved",
        CatalogAccess.suspended.is_(False),
    )
    catalogs = (
        db.query(Catalog)
        .filter(
            or_(Catalog.owner_workspace_id == ws.id, Catalog.id.in_(shared_ids)),
            Catalog.deleted_at.is_(None),
        )
        .order_by(Catalog.created_at)
        .all()
    )
    return _catalog_manifest_projection(catalogs)


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
    return _catalog_response(catalog, current_user, db, ws=ws)


def _resolve_workspace(workspace_id: str | None, user: User, db: Session) -> Workspace:
    if not workspace_id:
        raise HTTPException(422, "workspace_id is required")
    ws_uuid = _as_uuid(workspace_id)
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first() if ws_uuid else None
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if user.role != "superadmin":
        member = next(
            (
                m
                for m in user.workspace_memberships
                if str(m.workspace_id) == str(workspace_id) and m.role == "admin"
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
    ws_uuid = _as_uuid(workspace_id)
    membership = next(
        (m for m in current_user.workspace_memberships if str(m.workspace_id) == str(workspace_id)),
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
            Catalog.owner_workspace_id != ws_uuid,
        )
        .order_by(Catalog.name)
        .all()
        if ws_uuid
        else []
    )

    grants = (
        db.query(CatalogAccess).filter(CatalogAccess.workspace_id == ws_uuid).all()
        if ws_uuid
        else []
    )
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
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "read", ws=ws)
    return _catalog_response(catalog, current_user, db, ws=ws)


@app.patch("/api/catalogs/{catalog_id}", response_model=CatalogResponse)
def update_catalog(
    catalog_id: str,
    req: UpdateCatalogRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner", ws=ws)
    if req.name is not None:
        catalog.name = req.name.strip()
    if req.description is not None:
        catalog.description = req.description
    db.commit()
    db.refresh(catalog)
    return _catalog_response(catalog, current_user, db, ws=ws)


@app.delete("/api/catalogs/{catalog_id}", response_model=CatalogResponse)
def delete_catalog(
    catalog_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner", ws=ws)
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
    return _catalog_response(catalog, current_user, db, ws=ws)


@app.post("/api/catalogs/{catalog_id}/reactivate", response_model=CatalogResponse)
def reactivate_catalog(
    catalog_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner", ws=ws)
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
    return _catalog_response(catalog, current_user, db, ws=ws)


def _purge_objects_for_catalogs(db: Session, catalog_ids: set) -> None:
    """Delete object data for the given catalogs, sparing prefixes other catalogs use."""
    rows = (
        db.query(CatalogTable.s3_path_pattern, CatalogSchema.catalog_id)
        .join(CatalogSchema, CatalogTable.schema_id == CatalogSchema.id)
        .filter(CatalogTable.s3_path_pattern.isnot(None))
        .all()
    )
    own = [p for p, cid in rows if cid in catalog_ids]
    others = [p for p, cid in rows if cid not in catalog_ids]
    for prefix in prefixes_to_delete(own, others):
        delete_prefix(prefix)


def _purge_catalog_objects(db: Session, catalog: Catalog) -> None:
    """Remove the catalog's table data from object storage (see catalog_storage)."""
    _purge_objects_for_catalogs(db, {catalog.id})


def _purge_workspace_objects(db: Session, ws: Workspace) -> None:
    """Remove the table data of every catalog the workspace owns."""
    ids = {c.id for c in db.query(Catalog.id).filter(Catalog.owner_workspace_id == ws.id)}
    _purge_objects_for_catalogs(db, ids)


@app.post("/api/catalogs/{catalog_id}/purge")
def purge_catalog(
    catalog_id: str,
    req: PurgeCatalogRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner", ws=ws)
    if catalog.is_default:
        raise HTTPException(409, "The default catalog cannot be deleted")

    # Workspace admins can only purge soft-deleted catalogs; superadmins can purge any
    if current_user.role != "superadmin" and catalog.deleted_at is None:
        raise HTTPException(
            422, "Catalog must be in pending-deletion state before purging — delete it first"
        )

    if req.confirm_name != catalog.name:
        raise HTTPException(422, "Catalog name confirmation does not match")

    # Delete object data first: if storage fails the DB rows survive and the purge can retry.
    try:
        _purge_catalog_objects(db, catalog)
    except Exception as exc:
        raise HTTPException(502, "Failed to delete catalog data from object storage") from exc

    db.delete(catalog)
    db.commit()
    return {"ok": True}


# ── Schema CRUD ───────────────────────────────────────────────────────────────


@app.get("/api/catalogs/{catalog_id}/schemas", response_model=list[CatalogSchemaResponse])
def list_schemas(
    catalog_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "read", ws=ws)
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
        for s in sorted(catalog.schemas, key=_schema_sort_key)
    ]


@app.post("/api/catalogs/{catalog_id}/schemas", response_model=CatalogSchemaResponse)
def create_schema(
    catalog_id: str,
    req: CreateSchemaRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write", ws=ws)
    slug = _slugify(req.name)
    if db.query(CatalogSchema).filter_by(catalog_id=cat_uuid, slug=slug).first():
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
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    sch_uuid = _as_uuid(schema_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write", ws=ws)
    schema = (
        db.query(CatalogSchema).filter_by(id=sch_uuid, catalog_id=cat_uuid).first()
        if sch_uuid and cat_uuid
        else None
    )
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
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    sch_uuid = _as_uuid(schema_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner", ws=ws)
    schema = (
        db.query(CatalogSchema).filter_by(id=sch_uuid, catalog_id=cat_uuid).first()
        if sch_uuid and cat_uuid
        else None
    )
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
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    sch_uuid = _as_uuid(schema_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "read", ws=ws)
    schema = (
        db.query(CatalogSchema).filter_by(id=sch_uuid, catalog_id=cat_uuid).first()
        if (sch_uuid and cat_uuid)
        else None
    )
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
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    sch_uuid = _as_uuid(schema_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write", ws=ws)
    schema = (
        db.query(CatalogSchema).filter_by(id=sch_uuid, catalog_id=cat_uuid).first()
        if (sch_uuid and cat_uuid)
        else None
    )
    if not schema:
        raise HTTPException(404, "Schema not found")
    slug = _slugify(req.name)
    if db.query(CatalogTable).filter_by(schema_id=sch_uuid, slug=slug).first():
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
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    sch_uuid = _as_uuid(schema_id)
    tbl_uuid = _as_uuid(table_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write", ws=ws)
    table = (
        db.query(CatalogTable).filter_by(id=tbl_uuid, schema_id=sch_uuid).first()
        if (tbl_uuid and sch_uuid)
        else None
    )
    if not table:
        raise HTTPException(404, "Table not found")
    if req.description is not None:
        table.description = req.description
    if req.s3_path_pattern is not None:
        table.s3_path_pattern = req.s3_path_pattern
    if req.column_defs is not None:
        table.column_defs = [c.model_dump() for c in req.column_defs]
    if req.vector_index is not None:
        table.vector_index = req.vector_index
    db.commit()
    db.refresh(table)
    return CatalogTableResponse.model_validate(table)


@app.post(
    "/api/catalogs/{catalog_id}/schemas/{schema_id}/tables/{table_id}/vector-index/sync",
    response_model=CatalogTableResponse,
)
def sync_table_vector_index_endpoint(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    sch_uuid = _as_uuid(schema_id)
    tbl_uuid = _as_uuid(table_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write", ws=ws)
    table = (
        db.query(CatalogTable).filter_by(id=tbl_uuid, schema_id=sch_uuid).first()
        if (tbl_uuid and sch_uuid)
        else None
    )
    if not table:
        raise HTTPException(404, "Table not found")

    import vector_index_service

    res = vector_index_service.sync_table_vector_index(table, db)
    if not res.get("ok"):
        raise HTTPException(500, res.get("error", "Failed to sync vector index"))
    return CatalogTableResponse.model_validate(table)


@app.delete("/api/catalogs/{catalog_id}/schemas/{schema_id}/tables/{table_id}")
def delete_table(
    catalog_id: str,
    schema_id: str,
    table_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    sch_uuid = _as_uuid(schema_id)
    tbl_uuid = _as_uuid(table_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "write", ws=ws)
    table = (
        db.query(CatalogTable).filter_by(id=tbl_uuid, schema_id=sch_uuid).first()
        if (tbl_uuid and sch_uuid)
        else None
    )
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
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner", ws=ws)
    grants = (
        db.query(CatalogAccess).filter(CatalogAccess.catalog_id == cat_uuid).all()
        if cat_uuid
        else []
    )
    result = []
    for g in grants:
        g_ws = db.query(Workspace).filter(Workspace.id == g.workspace_id).first()
        requester = (
            db.query(User).filter(User.id == g.requested_by).first() if g.requested_by else None
        )
        result.append(
            CatalogAccessResponse(
                id=g.id,
                catalog_id=g.catalog_id,
                workspace_id=g.workspace_id,
                workspace_name=g_ws.name if g_ws else None,
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
    cat_uuid = _as_uuid(catalog_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
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

    ws_uuid = _as_uuid(ws_id)
    existing = (
        db.query(CatalogAccess).filter_by(catalog_id=cat_uuid, workspace_id=ws_uuid).first()
        if (cat_uuid and ws_uuid)
        else None
    )
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
                workspace_id=ws_uuid,
                mode=req.mode,
                status="pending",
                requested_by=current_user.id,
            )
        )
    db.commit()

    owner_emails = _owner_emails(db, catalog)
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first() if ws_uuid else None
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
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    acc_uuid = _as_uuid(access_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner", ws=ws)

    grant = (
        db.query(CatalogAccess).filter_by(id=acc_uuid, catalog_id=cat_uuid).first()
        if (acc_uuid and cat_uuid)
        else None
    )
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
    ws_ctx: tuple[Workspace, str] | None = Depends(get_catalog_workspace_and_role),
):
    ws = _extract_ws(ws_ctx)
    cat_uuid = _as_uuid(catalog_id)
    acc_uuid = _as_uuid(access_id)
    catalog = db.query(Catalog).filter(Catalog.id == cat_uuid).first() if cat_uuid else None
    if not catalog:
        raise HTTPException(404, "Catalog not found")
    _assert_catalog_access(db, catalog, current_user, "owner", ws=ws)
    grant = (
        db.query(CatalogAccess).filter_by(id=acc_uuid, catalog_id=cat_uuid).first()
        if (acc_uuid and cat_uuid)
        else None
    )
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
    ws_uuid = _as_uuid(workspace_id)
    membership = next(
        (m for m in current_user.workspace_memberships if str(m.workspace_id) == str(workspace_id)),
        None,
    )
    if not membership and current_user.role != "superadmin":
        raise HTTPException(403, "Not a member of this workspace")
    if membership and membership.role != "admin" and current_user.role != "superadmin":
        raise HTTPException(403, "Workspace admin access required")

    owned_catalog_ids = (
        db.query(Catalog.id).filter(
            Catalog.owner_workspace_id == ws_uuid,
            Catalog.deleted_at.is_(None),
        )
        if ws_uuid
        else []
    )
    grants = (
        db.query(CatalogAccess)
        .filter(
            CatalogAccess.catalog_id.in_(owned_catalog_ids),
            CatalogAccess.status == "pending",
        )
        .all()
        if ws_uuid
        else []
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
    ws_uuid = _as_uuid(workspace_id)
    ws = (
        db.query(Workspace).filter(Workspace.id == ws_uuid, Workspace.status == "active").first()
        if ws_uuid
        else None
    )
    if not ws:
        raise HTTPException(404, "Workspace not found")

    if current_user.role != "superadmin":
        is_member = any(
            str(m.workspace_id) == str(workspace_id) for m in current_user.workspace_memberships
        )
        if not is_member:
            has_share = (
                db.query(WorkspaceShare)
                .filter(
                    WorkspaceShare.workspace_id == ws.id,
                    WorkspaceShare.user_id == current_user.id,
                    WorkspaceShare.status == "approved",
                )
                .first()
            )
            if not has_share:
                raise HTTPException(403, "Not a member of this workspace")

    from sqlalchemy import or_

    shared_ids = db.query(CatalogAccess.catalog_id).filter(
        CatalogAccess.workspace_id == ws_uuid,
        CatalogAccess.status == "approved",
        CatalogAccess.suspended.is_(False),
    )
    catalogs = (
        db.query(Catalog)
        .filter(
            or_(Catalog.owner_workspace_id == ws_uuid, Catalog.id.in_(shared_ids)),
            Catalog.deleted_at.is_(None),
        )
        .order_by(Catalog.created_at)
        .all()
    )
    return [_catalog_response(c, current_user, db, ws=ws) for c in catalogs]


@app.get("/api/workspaces/{workspace_id}/catalog-manifest")
@app.get("/api/workspaces/{workspace_id}/manifest")
def workspace_catalog_manifest(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_uuid = _as_uuid(workspace_id)
    ws = (
        db.query(Workspace).filter(Workspace.id == ws_uuid, Workspace.status == "active").first()
        if ws_uuid
        else None
    )
    if not ws:
        raise HTTPException(404, "Workspace not found")

    if current_user.role != "superadmin":
        is_member = any(
            str(m.workspace_id) == str(workspace_id) for m in current_user.workspace_memberships
        )
        if not is_member:
            has_share = (
                db.query(WorkspaceShare)
                .filter(
                    WorkspaceShare.workspace_id == ws.id,
                    WorkspaceShare.user_id == current_user.id,
                    WorkspaceShare.status == "approved",
                )
                .first()
            )
            if not has_share:
                raise HTTPException(403, "Not a member of this workspace")

    from sqlalchemy import or_

    shared_ids = db.query(CatalogAccess.catalog_id).filter(
        CatalogAccess.workspace_id == ws_uuid,
        CatalogAccess.status == "approved",
        CatalogAccess.suspended.is_(False),
    )
    catalogs = (
        db.query(Catalog)
        .filter(
            or_(Catalog.owner_workspace_id == ws_uuid, Catalog.id.in_(shared_ids)),
            Catalog.deleted_at.is_(None),
        )
        .order_by(Catalog.created_at)
        .all()
    )
    return _catalog_manifest_projection(catalogs)


@app.get(
    "/api/workspaces/{workspace_id}/settings/catalogs",
    response_model=WorkspaceCatalogSettingsResponse,
)
def workspace_catalog_settings(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws_uuid = _as_uuid(workspace_id)
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first() if ws_uuid else None
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if current_user.role != "superadmin":
        member = next(
            (
                m
                for m in current_user.workspace_memberships
                if str(m.workspace_id) == str(workspace_id)
            ),
            None,
        )
        if not member:
            raise HTTPException(403, "Not a member of this workspace")

    owned = (
        db.query(Catalog)
        .filter(Catalog.owner_workspace_id == ws_uuid)
        .order_by(Catalog.deleted_at.is_(None).desc(), Catalog.name)
        .all()
    )

    access_grants = (
        db.query(CatalogAccess)
        .filter(
            CatalogAccess.workspace_id == ws_uuid,
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
    ws_uuid = _as_uuid(workspace_id)
    cat_uuid = _as_uuid(catalog_id)
    ws = db.query(Workspace).filter(Workspace.id == ws_uuid).first() if ws_uuid else None
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if current_user.role != "superadmin":
        member = next(
            (
                m
                for m in current_user.workspace_memberships
                if str(m.workspace_id) == str(workspace_id) and m.role == "admin"
            ),
            None,
        )
        if not member:
            raise HTTPException(403, "Workspace admin access required")

    grant = (
        db.query(CatalogAccess)
        .filter(
            CatalogAccess.workspace_id == ws_uuid,
            CatalogAccess.catalog_id == cat_uuid,
        )
        .first()
        if (ws_uuid and cat_uuid)
        else None
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
        # Delete object data first: if storage fails the DB rows survive and this can retry.
        try:
            _purge_workspace_objects(db, ws)
        except Exception as exc:
            raise HTTPException(502, "Failed to delete workspace data from object storage") from exc
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


def _system_setting_response(db: Session) -> SystemSettingResponse:
    raw_key = get_stored_gemini_api_key(db)
    return SystemSettingResponse(
        catalog_soft_delete_days=_get_soft_delete_days(db),
        workspace_inactive_grace_period_days=_get_workspace_inactive_days(db),
        session_inactivity_timeout_minutes=_get_session_inactivity_minutes(db),
        session_max_hours=_get_session_max_hours(db),
        gemini_api_key_configured=bool(raw_key),
        gemini_api_key_masked=mask_secret(raw_key),
        gemini_default_model=get_stored_gemini_model(db),
    )


@app.get("/api/admin/settings", response_model=SystemSettingResponse)
def get_admin_settings(
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
):
    return _system_setting_response(db)


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
    if req.clear_gemini_api_key:
        set_stored_gemini_api_key(db, None, current_user.id)
        ai_service.clear_ai_cache()
    elif req.gemini_api_key is not None:
        clean_key = req.gemini_api_key.strip()
        if not clean_key:
            raise HTTPException(400, "GEMINI_API_KEY cannot be empty")
        set_stored_gemini_api_key(db, clean_key, current_user.id)
        ai_service.clear_ai_cache()
    if req.gemini_default_model is not None:
        set_stored_gemini_model(db, req.gemini_default_model, current_user.id)
        ai_service.clear_ai_cache()
    db.commit()
    return _system_setting_response(db)


@app.post("/api/admin/settings/test-gemini-key", response_model=TestGeminiKeyResponse)
def test_admin_gemini_key(
    req: TestGeminiKeyRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
):
    candidate = (req.api_key or "").strip() or get_stored_gemini_api_key(db)
    model = (req.model or "").strip() or get_stored_gemini_model(db)
    if not candidate:
        return TestGeminiKeyResponse(
            ok=False,
            model=model,
            message="No GEMINI_API_KEY is configured or provided to test.",
        )
    result = ai_service.test_gemini_api_key(api_key=candidate, model=model)
    return TestGeminiKeyResponse(
        ok=bool(result.get("ok")),
        model=str(result.get("model") or model),
        message=str(result.get("message") or ""),
    )


@app.get("/api/internal/settings/gemini-key")
def get_internal_gemini_key(
    x_internal_secret: str | None = Header(default=None),
    lake_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Return the active decrypted GEMINI_API_KEY to internal services (ui, pipeline)."""
    authorized = False
    if x_internal_secret and hmac.compare_digest(
        x_internal_secret.encode("utf-8"), SECRET_KEY.encode("utf-8")
    ):
        authorized = True
    else:
        token = lake_token
        if not token and authorization and authorization.startswith("Bearer "):
            token = authorization[7:]
        if token:
            try:
                payload = decode_token(token)
                sub_val = payload.get("sub")
                user_uuid = uuid.UUID(str(sub_val))
                user = db.query(User).filter(User.id == user_uuid).first()
                if user and user.is_active:
                    authorized = True
            except Exception:
                authorized = False

    if not authorized:
        raise HTTPException(401, "Not authorized")

    raw_key = get_stored_gemini_api_key(db)
    return {
        "gemini_api_key": raw_key,
        "configured": bool(raw_key),
        "gemini_default_model": get_stored_gemini_model(db),
    }
