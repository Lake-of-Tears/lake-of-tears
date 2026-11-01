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
from models import (
    PersonalAccessToken,
    SystemSetting,
    User,
    UserSession,
    Workspace,
    WorkspaceMember,
    WorkspaceShare,
)
from sqlalchemy.orm import Session

SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "dev-secret-please-change-in-production")
ALGORITHM = "HS256"
COOKIE_NAME = "lake_token"
GEMINI_API_KEY_SETTING = "gemini_api_key"
GEMINI_MODEL_SETTING = "gemini_default_model"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


def is_registration_enabled() -> bool:
    """Return whether user self-registration is enabled in deployment settings."""
    val = os.getenv("AUTH_REGISTRATION_ENABLED", "true").strip().lower()
    return val not in ("false", "0", "no", "off")


# Fallback TTL used when the DB is unavailable (e.g. during first startup).
_FALLBACK_TTL = timedelta(hours=24)

# bcrypt only reads the first 72 bytes of a password and bcrypt >= 5 refuses longer ones. Longer
# passwords are rejected when they are set (see schemas.check_password_length), never truncated.
_BCRYPT_ROUNDS = 12

PAT_PREFIX = "lot_pat_"
MAX_ACTIVE_PATS_PER_USER = 50


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
    session_id: str | None = None,
) -> str:
    now = datetime.now(UTC)
    if expires_delta is None:
        expires_delta = _FALLBACK_TTL
    jti = session_id or data.get("jti") or str(uuid.uuid4())
    payload = {
        **data,
        "jti": str(jti),
        "ss": session_start_ts if session_start_ts is not None else now.timestamp(),
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def create_user_session(
    db: Session,
    user: User,
    expires_delta: timedelta,
    ip_address: str | None = None,
    user_agent: str | None = None,
    session_id: uuid.UUID | None = None,
) -> UserSession:
    """Create and persist a new active session in user_sessions."""
    now = datetime.now(UTC)
    sess_id = session_id or uuid.uuid4()
    session = UserSession(
        id=sess_id,
        user_id=user.id,
        ip_address=ip_address,
        user_agent=user_agent,
        created_at=now,
        last_active_at=now,
        expires_at=now + expires_delta,
        is_revoked=False,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_current_session_id(
    lake_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> uuid.UUID:
    """Extract and validate the current session ID (jti) from the request token."""
    token = lake_token
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(401, "Invalid token")
    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            401, "Legacy session token expired or invalid (re-authentication required)"
        )
    try:
        return uuid.UUID(str(jti))
    except (ValueError, TypeError):
        raise HTTPException(401, "Invalid session identifier")


def revoke_user_session(
    db: Session,
    session_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> bool:
    """Mark a user session as revoked. If user_id is provided, enforces ownership."""
    query = db.query(UserSession).filter(UserSession.id == session_id)
    if user_id is not None:
        query = query.filter(UserSession.user_id == user_id)
    session_rec = query.first()
    if not session_rec:
        return False
    session_rec.is_revoked = True
    session_rec.revoked_at = datetime.now(UTC)
    db.commit()
    return True


def revoke_all_other_sessions(
    db: Session,
    user_id: uuid.UUID,
    current_session_id: uuid.UUID,
) -> int:
    """Revoke all active sessions for a user except the currently active session."""
    now = datetime.now(UTC)
    updated = (
        db.query(UserSession)
        .filter(
            UserSession.user_id == user_id,
            UserSession.id != current_session_id,
            UserSession.is_revoked == False,  # noqa: E712
        )
        .update(
            {"is_revoked": True, "revoked_at": now},
            synchronize_session=False,
        )
    )
    if updated:
        db.commit()
    return updated


def prune_expired_sessions(db: Session, retention_days: int = 30) -> int:
    """Delete session records that were revoked or expired more than retention_days ago."""
    threshold = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = (
        db.query(UserSession)
        .filter(
            ((UserSession.is_revoked == True) & (UserSession.revoked_at < threshold))  # noqa: E712
            | (UserSession.expires_at < threshold)
        )
        .delete(synchronize_session=False)
    )
    if deleted:
        try:
            db.commit()
        except Exception:
            db.rollback()
    return deleted


def create_personal_access_token(
    db: Session,
    user_id: uuid.UUID,
    name: str,
    lifetime_days: int | None = 30,
    workspace_id: uuid.UUID | None = None,
    workspace_ids: list[uuid.UUID] | list[str] | None = None,
    workspace_access: dict[str, str] | None = None,
    scopes: list[str] | None = None,
) -> tuple[PersonalAccessToken, str]:
    """Generate and store a new Personal Access Token for a user."""
    active_count = (
        db.query(PersonalAccessToken)
        .filter(
            PersonalAccessToken.user_id == user_id,
            PersonalAccessToken.is_revoked == False,  # noqa: E712
            (PersonalAccessToken.expires_at == None)  # noqa: E711
            | (PersonalAccessToken.expires_at > datetime.now(UTC)),
        )
        .count()
    )
    if active_count >= MAX_ACTIVE_PATS_PER_USER:
        raise HTTPException(
            400,
            f"Active personal access token limit ({MAX_ACTIVE_PATS_PER_USER}) reached. Please revoke unused tokens.",
        )

    ROLE_LEVELS = {
        "read": 1,
        "write": 2,
        "admin": 3,
        "superadmin": 4,
    }

    resolved_access: dict[str, str] = {}
    if workspace_access:
        for k, v in workspace_access.items():
            resolved_access[str(k).strip()] = str(v).strip().lower()

    if workspace_ids:
        for wid in workspace_ids:
            wid_str = str(wid).strip()
            if wid_str not in resolved_access:
                resolved_access[wid_str] = "read"
    elif workspace_id is not None:
        wid_str = str(workspace_id).strip()
        if wid_str not in resolved_access:
            resolved_access[wid_str] = "read"

    if not resolved_access:
        raise HTTPException(400, "You must explicitly grant access to at least one workspace.")

    user = db.query(User).filter(User.id == user_id).first()
    user_is_superadmin = (user.role == "superadmin") if user else False

    final_access: dict[str, str] = {}
    for wid_str, req_role in resolved_access.items():
        try:
            wid_uuid = uuid.UUID(wid_str)
        except (ValueError, TypeError):
            raise HTTPException(400, f"Invalid workspace ID '{wid_str}'")

        ws = (
            db.query(Workspace)
            .filter(Workspace.id == wid_uuid, Workspace.status == "active")
            .first()
        )
        if not ws:
            raise HTTPException(404, f"Workspace '{wid_str}' not found")

        req_role_clean = req_role.strip().lower()
        if req_role_clean not in ("read", "write", "admin"):
            raise HTTPException(
                400,
                f"Invalid access level '{req_role}' for workspace '{ws.name}'. Must be one of: read, write, admin.",
            )

        if not user_is_superadmin:
            member = (
                db.query(WorkspaceMember)
                .filter(
                    WorkspaceMember.workspace_id == wid_uuid,
                    WorkspaceMember.user_id == user_id,
                )
                .first()
            )
            if not member:
                raise HTTPException(
                    403,
                    f"Cannot grant token access to workspace '{ws.name}': you are not a member of this workspace.",
                )

            user_level = ROLE_LEVELS.get(member.role, 1)
            req_level = ROLE_LEVELS.get(req_role_clean, 0)
            if req_level > user_level:
                raise HTTPException(
                    400,
                    f"Cannot grant '{req_role_clean}' access to workspace '{ws.name}': your permission level in this workspace is only '{member.role}'.",
                )

        final_access[str(wid_uuid)] = req_role_clean

    resolved_wids = list(final_access.keys())

    # Build or reconcile scopes
    final_scopes = list(scopes) if scopes else []
    if not final_scopes:
        derived_scopes = set()
        for r in final_access.values():
            if r == "read":
                derived_scopes.add("read")
            elif r == "write":
                derived_scopes.update(["read", "write"])
            elif r == "admin":
                derived_scopes.update(["read", "write", "admin"])
        final_scopes = sorted(derived_scopes)

    if not final_scopes:
        raise HTTPException(400, "You must explicitly select at least one permission scope.")

    random_hex = secrets.token_hex(32)
    raw_token = f"{PAT_PREFIX}{random_hex}"
    token_prefix = raw_token[:16]
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    now = datetime.now(UTC)
    expires_at = now + timedelta(days=lifetime_days) if lifetime_days is not None else None

    pat = PersonalAccessToken(
        user_id=user_id,
        name=name.strip(),
        token_prefix=token_prefix,
        token_hash=token_hash,
        workspace_id=uuid.UUID(resolved_wids[0]) if resolved_wids else None,
        workspace_ids=resolved_wids,
        workspace_access=final_access,
        scopes=final_scopes,
        created_at=now,
        expires_at=expires_at,
        is_revoked=False,
    )
    db.add(pat)
    db.commit()
    db.refresh(pat)
    return pat, raw_token


def revoke_personal_access_token(
    db: Session,
    token_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> bool:
    """Revoke a Personal Access Token."""
    query = db.query(PersonalAccessToken).filter(PersonalAccessToken.id == token_id)
    if user_id is not None:
        query = query.filter(PersonalAccessToken.user_id == user_id)
    pat = query.first()
    if not pat or pat.is_revoked:
        return False
    pat.is_revoked = True
    pat.revoked_at = datetime.now(UTC)
    db.commit()
    return True


def get_current_user(
    lake_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    token = None
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and isinstance(lake_token, str):
        token = lake_token
    if not token:
        raise HTTPException(401, "Not authenticated")

    if token.startswith(PAT_PREFIX):
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        pat = (
            db.query(PersonalAccessToken)
            .filter(
                PersonalAccessToken.token_hash == token_hash,
                PersonalAccessToken.is_revoked == False,  # noqa: E712
            )
            .first()
        )
        if not pat:
            raise HTTPException(401, "Invalid or revoked personal access token")

        now = datetime.now(UTC)
        if pat.expires_at is not None:
            expires_at = pat.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                raise HTTPException(401, "Personal access token expired")

        last_used = pat.last_used_at
        if last_used is not None and last_used.tzinfo is None:
            last_used = last_used.replace(tzinfo=UTC)
        if last_used is None or (now - last_used).total_seconds() >= 60:
            pat.last_used_at = now
            try:
                db.commit()
            except Exception:
                db.rollback()

        user = db.query(User).filter(User.id == pat.user_id).first()
        if not user or not user.is_active:
            raise HTTPException(401, "User inactive or not found")

        user._current_session_id = None
        user._pat = pat
        user._pat_scopes = pat.scopes or []
        wids = [str(wid) for wid in (pat.workspace_ids or [])]
        if pat.workspace_id and str(pat.workspace_id) not in wids:
            wids.append(str(pat.workspace_id))
        user._pat_workspace_ids = wids
        user._pat_workspace_id = pat.workspace_id or (uuid.UUID(wids[0]) if wids else None)
        user._pat_workspace_access = pat.workspace_access or {}
        for wid in wids:
            if wid not in user._pat_workspace_access:
                user._pat_workspace_access[wid] = "read"
        return user

    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

    sub_val = payload.get("sub")
    user = None
    try:
        user_uuid = uuid.UUID(str(sub_val))
        user = db.query(User).filter(User.id == user_uuid).first()
    except (ValueError, TypeError, AttributeError):
        pass

    if not user and sub_val:
        sub_str = str(sub_val)
        user = db.query(User).filter((User.email == sub_str) | (User.role == sub_str)).first()

    if not user or not user.is_active:
        raise HTTPException(401, "Invalid token")

    # Enforce active session tracking via jti claim in user_sessions
    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            401, "Legacy session token expired or invalid (re-authentication required)"
        )

    try:
        session_id = uuid.UUID(str(jti))
    except (ValueError, TypeError):
        raise HTTPException(401, "Invalid session identifier")

    session_rec = (
        db.query(UserSession)
        .filter(UserSession.id == session_id, UserSession.user_id == user.id)
        .first()
    )
    if not session_rec or session_rec.is_revoked:
        raise HTTPException(401, "Session has been revoked")

    now = datetime.now(UTC)
    expires_at = session_rec.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise HTTPException(401, "Session expired")

    # Throttle last_active_at updates: write at most once per 60 seconds
    last_active = session_rec.last_active_at
    if last_active.tzinfo is None:
        last_active = last_active.replace(tzinfo=UTC)
    if (now - last_active).total_seconds() >= 60:
        session_rec.last_active_at = now
        try:
            db.commit()
        except Exception:
            db.rollback()

    user._current_session_id = session_rec.id
    return user


def require_superadmin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "superadmin":
        raise HTTPException(403, "Superadmin access required")
    if hasattr(current_user, "_pat_scopes") and "admin" not in current_user._pat_scopes:
        raise HTTPException(403, "Personal access token does not have admin scope")
    return current_user


def get_workspace_member(
    workspace_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceMember:
    if hasattr(current_user, "_pat_workspace_ids"):
        if str(workspace_id) not in current_user._pat_workspace_ids:
            raise HTTPException(
                403, "Personal access token does not have access to this workspace"
            )

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
    if hasattr(current_user, "_pat_scopes") and "admin" not in current_user._pat_scopes:
        raise HTTPException(403, "Personal access token does not have admin scope")
    if hasattr(current_user, "_pat_workspace_ids"):
        if str(workspace_id) not in current_user._pat_workspace_ids:
            raise HTTPException(
                403, "Personal access token does not have access to this workspace"
            )
    if hasattr(current_user, "_pat_workspace_access"):
        token_role = current_user._pat_workspace_access.get(str(workspace_id), "read")
        if token_role != "admin":
            raise HTTPException(
                403, "Personal access token does not have admin access to this workspace"
            )

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

    share = None
    if not member and current_user.role != "superadmin":
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

    role = (
        member.role if member else ("superadmin" if current_user.role == "superadmin" else "read")
    )

    if hasattr(current_user, "_pat_workspace_ids"):
        if str(ws.id) not in current_user._pat_workspace_ids:
            raise HTTPException(
                403, f"Personal access token does not have access to workspace '{ws.name}'"
            )

    # Cap effective caller role to token's granted role on this workspace
    if hasattr(current_user, "_pat_workspace_access") and current_user._pat_workspace_access:
        pat_ws_role = current_user._pat_workspace_access.get(str(ws.id), "read")
        role_map = {"read": 1, "write": 2, "admin": 3, "superadmin": 4}
        if role_map.get(pat_ws_role, 1) < role_map.get(role, 1):
            role = pat_ws_role

    if (
        require_write
        and (
            (
                current_user.role != "superadmin"
                and (role in ("read", "viewer") or role not in ("write", "admin", "superadmin"))
            )
            or (
                hasattr(current_user, "_pat_workspace_access")
                and current_user._pat_workspace_access.get(str(ws.id), "read") == "read"
            )
            or (
                hasattr(current_user, "_pat_scopes")
                and "write" not in current_user._pat_scopes
                and "admin" not in current_user._pat_scopes
            )
        )
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
