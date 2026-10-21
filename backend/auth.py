from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from database import SessionLocal, get_db
from fastapi import Cookie, Depends, Header, HTTPException, Query
from models import SystemSetting, User, Workspace, WorkspaceMember
from sqlalchemy.orm import Session

SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "dev-secret-please-change-in-production")
ALGORITHM = "HS256"
COOKIE_NAME = "lake_token"
GEMINI_API_KEY_SETTING = "gemini_api_key"
GEMINI_MODEL_SETTING = "gemini_default_model"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

# Fallback TTL used when the DB is unavailable (e.g. during first startup).
_FALLBACK_TTL = timedelta(hours=24)

# bcrypt only reads the first 72 bytes of a password and bcrypt >= 5 refuses longer ones. Longer
# passwords are rejected when they are set (see schemas.check_password_length), never truncated.
_BCRYPT_ROUNDS = 12


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string at rest using key material derived from AUTH_SECRET_KEY."""
    raw = plaintext.encode("utf-8")
    nonce = secrets.token_bytes(16)
    secret_bytes = SECRET_KEY.encode("utf-8")
    enc_key = hmac.new(secret_bytes, b"lot-secret-enc-v1", hashlib.sha256).digest()
    mac_key = hmac.new(secret_bytes, b"lot-secret-mac-v1", hashlib.sha256).digest()
    stream = _keystream(enc_key, nonce, len(raw))
    ciphertext = bytes(a ^ b for a, b in zip(raw, stream, strict=False))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return (
        "v1:"
        + base64.urlsafe_b64encode(nonce).decode("ascii")
        + ":"
        + base64.urlsafe_b64encode(ciphertext).decode("ascii")
        + ":"
        + base64.urlsafe_b64encode(tag).decode("ascii")
    )


def decrypt_secret(token: str | None) -> str:
    """Decrypt a secret encrypted by encrypt_secret(). Returns '' if empty or invalid."""
    if not token:
        return ""
    if not token.startswith("v1:"):
        return ""
    parts = token.split(":")
    if len(parts) != 4:
        return ""
    try:
        nonce = base64.urlsafe_b64decode(parts[1].encode("ascii"))
        ciphertext = base64.urlsafe_b64decode(parts[2].encode("ascii"))
        tag = base64.urlsafe_b64decode(parts[3].encode("ascii"))
    except Exception:
        return ""
    secret_bytes = SECRET_KEY.encode("utf-8")
    enc_key = hmac.new(secret_bytes, b"lot-secret-enc-v1", hashlib.sha256).digest()
    mac_key = hmac.new(secret_bytes, b"lot-secret-mac-v1", hashlib.sha256).digest()
    expected_tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        return ""
    stream = _keystream(enc_key, nonce, len(ciphertext))
    plaintext = bytes(a ^ b for a, b in zip(ciphertext, stream, strict=False))
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def mask_secret(plaintext: str | None) -> str | None:
    """Return a masked preview (e.g. '••••••••1234') or None if unset."""
    if not plaintext or not plaintext.strip():
        return None
    clean = plaintext.strip()
    if len(clean) <= 4:
        return "••••••••"
    return f"••••••••{clean[-4:]}"


def get_stored_gemini_api_key(db: Session | None = None) -> str:
    """Retrieve and decrypt the GEMINI_API_KEY configured in the Admin Panel."""
    owns_session = False
    if db is None:
        try:
            db = SessionLocal()
            owns_session = True
        except Exception:
            return ""
    try:
        setting = (
            db.query(SystemSetting).filter(SystemSetting.key == GEMINI_API_KEY_SETTING).first()
        )
        if not setting or not setting.value_str:
            return ""
        return decrypt_secret(setting.value_str).strip()
    except Exception:
        return ""
    finally:
        if owns_session and db is not None:
            db.close()


def set_stored_gemini_api_key(
    db: Session,
    plaintext_key: str | None,
    user_id: uuid.UUID | None = None,
) -> None:
    """Encrypt and store (or clear) the GEMINI_API_KEY in system_settings."""
    clean = (plaintext_key or "").strip()
    encrypted = encrypt_secret(clean) if clean else None
    setting = db.query(SystemSetting).filter(SystemSetting.key == GEMINI_API_KEY_SETTING).first()
    if setting:
        setting.value_str = encrypted
        setting.updated_at = datetime.now(UTC)
        setting.updated_by = user_id
    else:
        db.add(
            SystemSetting(
                key=GEMINI_API_KEY_SETTING,
                value=None,
                value_str=encrypted,
                updated_by=user_id,
            )
        )


def get_stored_gemini_model(db: Session | None = None) -> str:
    """Return the default Gemini model configured in the Admin Panel (or the built-in default)."""
    owns_session = False
    if db is None:
        try:
            db = SessionLocal()
            owns_session = True
        except Exception:
            return DEFAULT_GEMINI_MODEL
    try:
        setting = db.query(SystemSetting).filter(SystemSetting.key == GEMINI_MODEL_SETTING).first()
        value = (setting.value_str or "").strip() if setting else ""
        return value or DEFAULT_GEMINI_MODEL
    except Exception:
        return DEFAULT_GEMINI_MODEL
    finally:
        if owns_session and db is not None:
            db.close()


def set_stored_gemini_model(
    db: Session,
    model: str | None,
    user_id: uuid.UUID | None = None,
) -> None:
    """Store the default Gemini model in system_settings (empty/None resets to built-in default)."""
    clean = (model or "").strip() or None
    setting = db.query(SystemSetting).filter(SystemSetting.key == GEMINI_MODEL_SETTING).first()
    if setting:
        setting.value_str = clean
        setting.updated_at = datetime.now(UTC)
        setting.updated_by = user_id
    else:
        db.add(
            SystemSetting(key=GEMINI_MODEL_SETTING, value=None, value_str=clean, updated_by=user_id)
        )


def hash_password(password: str) -> str:
    # raises ValueError for a password over 72 bytes; the API validates that first
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode(
        "ascii"
    )


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:  # too long to be a real password, or not a bcrypt hash: a failed login
        return False


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


def get_workspace_context(
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
    write_error_msg: str = "Workspace read role is read-only. Cannot execute write operations.",
) -> tuple[Workspace, str]:
    """Resolve active workspace and verify caller permissions.

    Precedence order:
      1. Query params (workspace_slug, workspace_id)
      2. Headers (X-Workspace-Slug, X-Workspace-Id)
      3. Cookies (lake_workspace_id, lake_active_workspace)

    Rejects requests with 400 if no active workspace is specified.
    Rejects with 404 if the workspace is not found or inactive.
    Rejects with 403 if user is not superadmin and not a member.
    Allows superadmins to switch between any active workspace (scope-jailed).
    """

    def _clean(val: Any) -> str | None:
        if val is None or not isinstance(val, str | uuid.UUID):
            return None
        s = str(val).strip()
        return s if s else None

    slug = _clean(workspace_slug) or _clean(x_workspace_slug)
    raw_id = (
        _clean(workspace_id)
        or _clean(x_workspace_id)
        or _clean(lake_workspace_id)
        or _clean(lake_active_workspace)
    )

    if not slug and not raw_id:
        raise HTTPException(
            400,
            "Active workspace context (X-Workspace-Id/Slug header or lake_workspace_id cookie) is required",
        )

    ws: Workspace | None = None
    if slug:
        ws = (
            db.query(Workspace).filter(Workspace.slug == slug, Workspace.status == "active").first()
        )
    elif raw_id:
        try:
            ws_uuid = uuid.UUID(str(raw_id))
            ws = (
                db.query(Workspace)
                .filter(Workspace.id == ws_uuid, Workspace.status == "active")
                .first()
            )
        except (ValueError, TypeError):
            ws = None

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
        member.role if member else ("superadmin" if current_user.role == "superadmin" else "read")
    )

    if (
        require_write
        and current_user.role != "superadmin"
        and (role in ("read", "viewer") or role not in ("write", "admin", "superadmin"))
    ):
        raise HTTPException(403, write_error_msg)

    return ws, role


def get_notebook_workspace_and_role(
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
    x_workspace_id: str | None = None,
    x_workspace_slug: str | None = None,
    lake_workspace_id: str | None = None,
    lake_active_workspace: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str]:
    """Resolve workspace and verify user permissions for notebook operations."""
    return get_workspace_context(
        workspace_slug=workspace_slug,
        workspace_id=workspace_id,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=require_write,
        write_error_msg="Workspace viewer role is read-only. Cannot modify or execute notebooks.",
    )


def get_pipeline_workspace_and_role(
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
    x_workspace_id: str | None = None,
    x_workspace_slug: str | None = None,
    lake_workspace_id: str | None = None,
    lake_active_workspace: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str]:
    """Resolve workspace and verify user permissions for pipeline operations."""
    return get_workspace_context(
        workspace_slug=workspace_slug,
        workspace_id=workspace_id,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=require_write,
        write_error_msg="Workspace viewer role is read-only. Cannot modify or trigger pipelines.",
    )


def get_storage_workspace_and_role(
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
    x_workspace_id: str | None = None,
    x_workspace_slug: str | None = None,
    lake_workspace_id: str | None = None,
    lake_active_workspace: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str]:
    """Resolve workspace and verify user permissions for storage operations."""
    return get_workspace_context(
        workspace_slug=workspace_slug,
        workspace_id=workspace_id,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=require_write,
        write_error_msg="Workspace viewer role is read-only. Cannot delete or modify storage objects.",
    )


def get_dashboard_workspace_and_role(
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
    x_workspace_id: str | None = None,
    x_workspace_slug: str | None = None,
    lake_workspace_id: str | None = None,
    lake_active_workspace: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str]:
    """Resolve workspace and verify user permissions for dashboard operations."""
    return get_workspace_context(
        workspace_slug=workspace_slug,
        workspace_id=workspace_id,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=require_write,
        write_error_msg="Workspace viewer role is read-only. Cannot create, edit, or delete dashboards.",
    )


def get_ai_workspace_and_role(
    workspace_id: str | None = None,
    workspace_slug: str | None = None,
    x_workspace_id: str | None = None,
    x_workspace_slug: str | None = None,
    lake_workspace_id: str | None = None,
    lake_active_workspace: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str]:
    """Resolve workspace and verify user permissions for AI & SQL operations."""
    return get_workspace_context(
        workspace_slug=workspace_slug,
        workspace_id=workspace_id,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=require_write,
        write_error_msg="Workspace viewer role is read-only. Cannot execute write operations.",
    )


def get_catalog_workspace_and_role(
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    require_write: bool = False,
) -> tuple[Workspace, str]:
    """Resolve active workspace and verify permissions for catalog operations."""
    return get_workspace_context(
        workspace_slug=workspace_slug,
        workspace_id=workspace_id,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=require_write,
        write_error_msg="Workspace viewer role is read-only. Cannot modify catalogs.",
    )


def get_catalog_write_workspace_and_role(
    workspace_slug: str | None = Query(None),
    workspace_id: str | None = Query(None),
    x_workspace_slug: str | None = Header(None, alias="X-Workspace-Slug"),
    x_workspace_id: str | None = Header(None, alias="X-Workspace-Id"),
    lake_workspace_id: str | None = Cookie(None),
    lake_active_workspace: str | None = Cookie(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[Workspace, str]:
    """Resolve active workspace and require write permissions for catalog operations."""
    return get_catalog_workspace_and_role(
        workspace_slug=workspace_slug,
        workspace_id=workspace_id,
        x_workspace_slug=x_workspace_slug,
        x_workspace_id=x_workspace_id,
        lake_workspace_id=lake_workspace_id,
        lake_active_workspace=lake_active_workspace,
        current_user=current_user,
        db=db,
        require_write=True,
    )
