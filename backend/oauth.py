from __future__ import annotations

import base64
import json
import os
import secrets
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
import jwt
import sqlalchemy as sa
from auth import (
    COOKIE_NAME,
    SECRET_KEY,
    create_token,
    create_user_session,
    get_session_expires_delta,
    is_registration_enabled,
)
from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from models import OAuthAccount, User, UserInvitation, Workspace, WorkspaceMember
from sqlalchemy import func
from sqlalchemy.orm import Session

_STATE_COOKIE = "oauth_state"
_BASE_URL = lambda: os.getenv("AUTH_BASE_URL", "http://localhost")  # noqa: E731
_OIDC_DISCOVERY_CACHE: dict[str, dict] = {}


def _normalize_private_key(key: str) -> str:
    """Normalize EC private key from multiline, escaped-newlines, or base64 format."""
    cleaned = key.strip()
    if not cleaned.startswith("-----") and "\n" not in cleaned:
        try:
            decoded = base64.b64decode(cleaned).decode("utf-8")
            if "BEGIN" in decoded:
                cleaned = decoded.strip()
        except Exception:
            pass
    if "\\n" in cleaned:
        cleaned = cleaned.replace("\\n", "\n")
    if not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned


def _generate_apple_client_secret(
    client_id: str, team_id: str, key_id: str, private_key: str
) -> str:
    """Generate dynamic ES256 client secret JWT for Sign in with Apple."""
    now = int(time.time())
    headers = {
        "kid": key_id,
        "alg": "ES256",
    }
    payload = {
        "iss": team_id,
        "iat": now,
        "exp": now + 300,  # 5 minutes expiration
        "aud": "https://appleid.apple.com",
        "sub": client_id,
    }
    norm_key = _normalize_private_key(private_key)
    return jwt.encode(payload, norm_key, algorithm="ES256", headers=headers)


def _fetch_oidc_discovery(issuer: str) -> dict:
    clean_issuer = issuer.strip().rstrip("/")
    if not clean_issuer.startswith("http"):
        clean_issuer = f"https://{clean_issuer}"
    if clean_issuer in _OIDC_DISCOVERY_CACHE:
        return _OIDC_DISCOVERY_CACHE[clean_issuer]
    discovery_url = f"{clean_issuer}/.well-known/openid-configuration"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(discovery_url)
            if resp.status_code == 200:
                data = resp.json()
                _OIDC_DISCOVERY_CACHE[clean_issuer] = data
                return data
    except Exception:
        pass
    return {}


def _provider_config(provider: str) -> dict:
    if provider == "google":
        return {
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
            "scope": "openid email profile",
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        }
    if provider == "github":
        return {
            "auth_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "userinfo_url": "https://api.github.com/user",
            "scope": "read:user user:email",
            "client_id": os.getenv("GITHUB_CLIENT_ID", ""),
            "client_secret": os.getenv("GITHUB_CLIENT_SECRET", ""),
        }
    if provider == "microsoft":
        tenant = os.getenv("MICROSOFT_TENANT_ID", "common")
        return {
            "auth_url": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
            "token_url": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            "userinfo_url": "https://graph.microsoft.com/v1.0/me",
            "scope": "openid email profile User.Read",
            "client_id": os.getenv("MICROSOFT_CLIENT_ID", ""),
            "client_secret": os.getenv("MICROSOFT_CLIENT_SECRET", ""),
        }
    if provider == "apple":
        client_id = os.getenv("APPLE_CLIENT_ID", "")
        team_id = os.getenv("APPLE_TEAM_ID", "")
        key_id = os.getenv("APPLE_KEY_ID", "")
        private_key = os.getenv("APPLE_PRIVATE_KEY") or os.getenv("APPLE_CLIENT_SECRET", "")
        client_secret = ""
        if client_id and team_id and key_id and private_key:
            client_secret = _generate_apple_client_secret(client_id, team_id, key_id, private_key)
        return {
            "auth_url": "https://appleid.apple.com/auth/authorize",
            "token_url": "https://appleid.apple.com/auth/token",
            "userinfo_url": "",
            "scope": "name email",
            "response_mode": "form_post",
            "client_id": client_id,
            "client_secret": client_secret,
            "team_id": team_id,
            "key_id": key_id,
            "private_key": private_key,
        }
    if provider == "okta":
        raw_domain = os.getenv("OKTA_DOMAIN") or os.getenv("OKTA_ISSUER_URL", "")
        domain = raw_domain.strip().rstrip("/")
        if domain and not domain.startswith("http"):
            domain = f"https://{domain}"
        base = domain if "/oauth2" in domain else f"{domain}/oauth2/v1"
        return {
            "auth_url": f"{base}/authorize",
            "token_url": f"{base}/token",
            "userinfo_url": f"{base}/userinfo",
            "scope": os.getenv("OKTA_SCOPE", "openid email profile"),
            "client_id": os.getenv("OKTA_CLIENT_ID", ""),
            "client_secret": os.getenv("OKTA_CLIENT_SECRET", ""),
        }
    if provider == "gitlab":
        raw_url = os.getenv("GITLAB_URL", "https://gitlab.com").strip().rstrip("/")
        if not raw_url.startswith("http"):
            raw_url = f"https://{raw_url}"
        return {
            "auth_url": f"{raw_url}/oauth/authorize",
            "token_url": f"{raw_url}/oauth/token",
            "userinfo_url": f"{raw_url}/api/v4/user",
            "scope": os.getenv("GITLAB_SCOPE", "read_user openid email profile"),
            "client_id": os.getenv("GITLAB_CLIENT_ID", ""),
            "client_secret": os.getenv("GITLAB_CLIENT_SECRET", ""),
        }
    if provider == "oidc":
        issuer = os.getenv("OIDC_ISSUER_URL", "").strip()
        auth_url = os.getenv("OIDC_AUTH_URL", "")
        token_url = os.getenv("OIDC_TOKEN_URL", "")
        userinfo_url = os.getenv("OIDC_USERINFO_URL", "")
        if issuer and (not auth_url or not token_url or not userinfo_url):
            disc = _fetch_oidc_discovery(issuer)
            auth_url = auth_url or disc.get("authorization_endpoint", "")
            token_url = token_url or disc.get("token_endpoint", "")
            userinfo_url = userinfo_url or disc.get("userinfo_endpoint", "")
        return {
            "auth_url": auth_url,
            "token_url": token_url,
            "userinfo_url": userinfo_url,
            "scope": os.getenv("OIDC_SCOPE", "openid email profile"),
            "client_id": os.getenv("OIDC_CLIENT_ID", ""),
            "client_secret": os.getenv("OIDC_CLIENT_SECRET", ""),
        }
    raise HTTPException(400, f"Unknown OAuth provider: {provider}")


def is_provider_enabled(provider: str) -> bool:
    env_keys = {
        "google": ("GOOGLE_CLIENT_ID", "GOOGLE_ENABLED"),
        "github": ("GITHUB_CLIENT_ID", "GITHUB_ENABLED"),
        "microsoft": ("MICROSOFT_CLIENT_ID", "MICROSOFT_ENABLED"),
        "apple": ("APPLE_CLIENT_ID", "APPLE_ENABLED"),
        "okta": ("OKTA_CLIENT_ID", "OKTA_ENABLED"),
        "gitlab": ("GITLAB_CLIENT_ID", "GITLAB_ENABLED"),
        "oidc": ("OIDC_CLIENT_ID", "OIDC_ENABLED"),
    }
    if provider not in env_keys:
        return False
    cid_var, enabled_var = env_keys[provider]
    cid = os.getenv(cid_var, "").strip()
    if not cid:
        return False
    if provider == "apple":
        has_secret = bool(os.getenv("APPLE_PRIVATE_KEY") or os.getenv("APPLE_CLIENT_SECRET"))
        if not (
            os.getenv("APPLE_TEAM_ID", "").strip()
            and os.getenv("APPLE_KEY_ID", "").strip()
            and has_secret
        ):
            return False

    enabled_val = os.getenv(enabled_var)
    if enabled_val is not None and enabled_val.strip() != "":
        return enabled_val.strip().lower() in ("true", "1", "yes")
    return True


def enabled_providers() -> list[dict]:
    providers = [
        ("google", "Google"),
        ("github", "GitHub"),
        ("microsoft", "Microsoft Entra ID"),
        ("apple", "Apple"),
        ("okta", "Okta"),
        ("gitlab", "GitLab"),
        ("oidc", os.getenv("OIDC_DISPLAY_NAME", "SSO")),
    ]
    return [{"key": k, "label": lbl} for k, lbl in providers if is_provider_enabled(k)]


def get_oauth_redirect(provider: str, request: Request, response: Response) -> RedirectResponse:
    if not is_provider_enabled(provider):
        raise HTTPException(400, f"{provider} OAuth is disabled or not configured")

    cfg = _provider_config(provider)
    if not cfg["client_id"]:
        raise HTTPException(400, f"{provider} OAuth not configured")

    state = secrets.token_urlsafe(32)
    next_url = request.query_params.get("next", "/")
    state_token = jwt.encode({"state": state, "next": next_url}, SECRET_KEY, algorithm="HS256")

    callback_url = f"{_BASE_URL()}/api/auth/oauth/{provider}/callback"
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": cfg["scope"],
        "state": state,
    }
    if provider in ("google", "microsoft"):
        params["prompt"] = "select_account"
    if cfg.get("response_mode"):
        params["response_mode"] = cfg["response_mode"]

    auth_url = cfg["auth_url"] + "?" + urlencode(params)

    resp = RedirectResponse(url=auth_url)
    resp.set_cookie(_STATE_COOKIE, state_token, httponly=True, samesite="lax", max_age=600)
    return resp


def _github_primary_email(access_token: str) -> str:
    with httpx.Client(timeout=10) as client:
        r = client.get(
            "https://api.github.com/user/emails",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
    if r.status_code == 200:
        for entry in r.json():
            if entry.get("primary") and entry.get("verified"):
                return entry["email"]
    return ""


def _extract_user_info(provider: str, userinfo: dict, access_token: str) -> tuple[str, str, str]:
    """Returns (email, display_name, provider_user_id)."""
    if provider == "google":
        return userinfo.get("email", ""), userinfo.get("name", ""), userinfo.get("sub", "")
    if provider == "github":
        email = userinfo.get("email") or _github_primary_email(access_token)
        return email, userinfo.get("name") or userinfo.get("login", ""), str(userinfo.get("id", ""))
    if provider == "microsoft":
        email = userinfo.get("mail") or userinfo.get("userPrincipalName", "")
        return email, userinfo.get("displayName", ""), userinfo.get("id", "")
    if provider == "okta":
        return (
            userinfo.get("email", ""),
            userinfo.get("name") or userinfo.get("preferred_username", ""),
            str(userinfo.get("sub", "")),
        )
    if provider == "gitlab":
        email = userinfo.get("email", "")
        name = userinfo.get("name") or userinfo.get("username", "")
        uid = str(userinfo.get("id", ""))
        return email, name, uid
    # generic OIDC
    email = userinfo.get("email", "")
    name = userinfo.get("name") or userinfo.get("preferred_username", "")
    uid = str(userinfo.get("sub") or userinfo.get("id", ""))
    return email, name, uid


def handle_oauth_callback(
    provider: str,
    request: Request,
    response: Response,
    db: Session,
    form_data: dict | None = None,
):
    if not is_provider_enabled(provider):
        raise HTTPException(400, f"{provider} OAuth is disabled or not configured")

    # Verify state cookie
    state_token = request.cookies.get(_STATE_COOKIE)
    if not state_token:
        raise HTTPException(400, "Missing OAuth state — please try again")
    try:
        state_data = jwt.decode(state_token, SECRET_KEY, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(400, "Invalid OAuth state")

    incoming_state = request.query_params.get("state")
    if not incoming_state and form_data:
        incoming_state = form_data.get("state")
    if not incoming_state and hasattr(request, "_form") and request._form is not None:
        incoming_state = dict(request._form).get("state")

    if state_data.get("state") != (incoming_state or ""):
        raise HTTPException(400, "OAuth state mismatch")

    code = request.query_params.get("code")
    if not code and form_data:
        code = form_data.get("code")
    if not code and hasattr(request, "_form") and request._form is not None:
        code = dict(request._form).get("code")

    if not code:
        raise HTTPException(400, "No authorization code received")

    cfg = _provider_config(provider)
    callback_url = f"{_BASE_URL()}/api/auth/oauth/{provider}/callback"

    # Exchange code for access token
    with httpx.Client(timeout=15) as client:
        token_resp = client.post(
            cfg["token_url"],
            data={
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "code": code,
                "redirect_uri": callback_url,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )

    if token_resp.status_code != 200:
        raise HTTPException(502, f"OAuth token exchange failed: {token_resp.text[:200]}")

    token_json = token_resp.json()

    if provider == "apple":
        id_token = token_json.get("id_token", "")
        if not id_token:
            raise HTTPException(502, "No id_token in Apple OAuth response")
        try:
            id_claims = jwt.decode(id_token, options={"verify_signature": False})
        except Exception as e:
            raise HTTPException(502, f"Failed to parse Apple id_token: {e}")

        email = id_claims.get("email", "")
        provider_user_id = str(id_claims.get("sub", ""))
        display_name = ""

        # Check initial user name from Apple form_post payload if present
        user_raw = (form_data or {}).get("user")
        if not user_raw and hasattr(request, "_form") and request._form is not None:
            user_raw = dict(request._form).get("user")

        if user_raw:
            user_obj = None
            if isinstance(user_raw, str):
                try:
                    user_obj = json.loads(user_raw)
                except Exception:
                    pass
            elif isinstance(user_raw, dict):
                user_obj = user_raw

            if isinstance(user_obj, dict):
                name_obj = user_obj.get("name")
                if isinstance(name_obj, dict):
                    fn = name_obj.get("firstName", "").strip()
                    ln = name_obj.get("lastName", "").strip()
                    if fn or ln:
                        display_name = f"{fn} {ln}".strip()
                if not email and user_obj.get("email"):
                    email = user_obj["email"].strip()

        if not display_name:
            display_name = email.split("@")[0] if email else "Apple User"
    else:
        access_token = token_json.get("access_token", "")
        if not access_token:
            raise HTTPException(502, "No access_token in OAuth response")

        # Fetch user info
        with httpx.Client(timeout=10) as client:
            userinfo_resp = client.get(
                cfg["userinfo_url"],
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )

        if userinfo_resp.status_code != 200:
            raise HTTPException(502, "Failed to fetch user info from OAuth provider")

        email, display_name, provider_user_id = _extract_user_info(
            provider, userinfo_resp.json(), access_token
        )

    if not email:
        raise HTTPException(400, "OAuth provider did not return an email address")

    # Find or create user
    user = db.query(User).filter(User.email == email).first()
    is_first = db.query(User).count() == 0
    if not user:
        invite = (
            db.query(UserInvitation)
            .filter(
                func.lower(UserInvitation.email) == func.lower(email.strip()),
                UserInvitation.status == "pending",
            )
            .first()
        )

        if not is_registration_enabled() and not is_first and not invite:
            resp = RedirectResponse(url="/login?error=registration_disabled")
            resp.delete_cookie(_STATE_COOKIE)
            return resp

        assigned_role = "superadmin" if is_first else (invite.role if invite else "user")

        user = User(
            email=email,
            display_name=display_name or email.split("@")[0],
            role=assigned_role,
            is_active=True,
        )
        db.add(user)
        db.flush()

        if is_first:
            # Bootstrap default workspace
            from main import _create_default_workspace

            _create_default_workspace(db, user)
        else:
            ws_access = invite.workspace_access if (invite and invite.workspace_access) else {}
            default_ws = db.query(Workspace).filter(Workspace.slug == "default").first()
            if default_ws:
                default_role = ws_access.get("default") or (
                    "write" if assigned_role == "superadmin" else "read"
                )
                db.add(
                    WorkspaceMember(workspace_id=default_ws.id, user_id=user.id, role=default_role)
                )

            for ws_slug_or_id, ws_role in ws_access.items():
                if ws_slug_or_id == "default":
                    continue
                other_ws = (
                    db.query(Workspace)
                    .filter(
                        (Workspace.slug == ws_slug_or_id)
                        | (Workspace.id.cast(sa.String) == str(ws_slug_or_id))
                    )
                    .first()
                )
                if other_ws and other_ws.id != (default_ws.id if default_ws else None):
                    db.add(WorkspaceMember(workspace_id=other_ws.id, user_id=user.id, role=ws_role))

        if invite:
            invite.status = "accepted"
            invite.accepted_at = datetime.now(UTC)

    # Link OAuth account
    if (
        not db.query(OAuthAccount)
        .filter_by(provider=provider, provider_user_id=provider_user_id)
        .first()
    ):
        db.add(OAuthAccount(user_id=user.id, provider=provider, provider_user_id=provider_user_id))

    db.commit()

    if not user.is_active:
        resp = RedirectResponse(url="/login?error=disabled")
        resp.delete_cookie(_STATE_COOKIE)
        return resp

    expires_delta = get_session_expires_delta(db)
    ip_addr = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    user_sess = create_user_session(db, user, expires_delta, ip_address=ip_addr, user_agent=ua)
    token = create_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "display_name": user.display_name or "",
        },
        expires_delta=expires_delta,
        session_id=str(user_sess.id),
    )
    next_url = state_data.get("next", "/")
    resp = RedirectResponse(url=next_url)
    resp.delete_cookie(_STATE_COOKIE)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(expires_delta.total_seconds()),
        httponly=True,
        samesite="lax",
    )
    return resp


def _mask_client_id(cid: str) -> str | None:
    if not cid or not cid.strip():
        return None
    clean = cid.strip()
    if len(clean) <= 8:
        return "••••••••"
    return f"{clean[:4]}...{clean[-4:]}"


def get_auth_overview(db: Session | None = None) -> dict:
    base_url = _BASE_URL()
    user_count = db.query(User).count() if db else 0
    reg_enabled = is_registration_enabled()

    providers = [
        {
            "key": "google",
            "name": "Google Workspace",
            "configured": bool(os.getenv("GOOGLE_CLIENT_ID")),
            "client_id_masked": _mask_client_id(os.getenv("GOOGLE_CLIENT_ID", "")),
            "details": {},
            "env_vars": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
            "callback_url": f"{base_url}/api/auth/oauth/google/callback",
        },
        {
            "key": "microsoft",
            "name": "Microsoft Entra ID",
            "configured": bool(os.getenv("MICROSOFT_CLIENT_ID")),
            "client_id_masked": _mask_client_id(os.getenv("MICROSOFT_CLIENT_ID", "")),
            "details": {"tenant": os.getenv("MICROSOFT_TENANT_ID", "common")},
            "env_vars": [
                "MICROSOFT_CLIENT_ID",
                "MICROSOFT_CLIENT_SECRET",
                "MICROSOFT_TENANT_ID",
            ],
            "callback_url": f"{base_url}/api/auth/oauth/microsoft/callback",
        },
        {
            "key": "apple",
            "name": "Apple ID",
            "configured": bool(os.getenv("APPLE_CLIENT_ID")),
            "client_id_masked": _mask_client_id(os.getenv("APPLE_CLIENT_ID", "")),
            "details": {
                "team_id": os.getenv("APPLE_TEAM_ID", ""),
                "key_id": os.getenv("APPLE_KEY_ID", ""),
            },
            "env_vars": [
                "APPLE_CLIENT_ID",
                "APPLE_TEAM_ID",
                "APPLE_KEY_ID",
                "APPLE_PRIVATE_KEY",
            ],
            "callback_url": f"{base_url}/api/auth/oauth/apple/callback",
        },
        {
            "key": "okta",
            "name": "Okta",
            "configured": bool(os.getenv("OKTA_CLIENT_ID")),
            "client_id_masked": _mask_client_id(os.getenv("OKTA_CLIENT_ID", "")),
            "details": {
                "domain": os.getenv("OKTA_DOMAIN") or os.getenv("OKTA_ISSUER_URL") or "Not set"
            },
            "env_vars": ["OKTA_CLIENT_ID", "OKTA_CLIENT_SECRET", "OKTA_DOMAIN"],
            "callback_url": f"{base_url}/api/auth/oauth/okta/callback",
        },
        {
            "key": "github",
            "name": "GitHub",
            "configured": bool(os.getenv("GITHUB_CLIENT_ID")),
            "client_id_masked": _mask_client_id(os.getenv("GITHUB_CLIENT_ID", "")),
            "details": {},
            "env_vars": ["GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"],
            "callback_url": f"{base_url}/api/auth/oauth/github/callback",
        },
        {
            "key": "gitlab",
            "name": "GitLab",
            "configured": bool(os.getenv("GITLAB_CLIENT_ID")),
            "client_id_masked": _mask_client_id(os.getenv("GITLAB_CLIENT_ID", "")),
            "details": {"url": os.getenv("GITLAB_URL", "https://gitlab.com")},
            "env_vars": ["GITLAB_CLIENT_ID", "GITLAB_CLIENT_SECRET", "GITLAB_URL"],
            "callback_url": f"{base_url}/api/auth/oauth/gitlab/callback",
        },
        {
            "key": "oidc",
            "name": os.getenv("OIDC_DISPLAY_NAME", "OpenID Connect (OIDC)"),
            "configured": bool(os.getenv("OIDC_CLIENT_ID")),
            "client_id_masked": _mask_client_id(os.getenv("OIDC_CLIENT_ID", "")),
            "details": {
                "issuer": os.getenv("OIDC_ISSUER_URL") or os.getenv("OIDC_AUTH_URL") or "Not set"
            },
            "env_vars": [
                "OIDC_CLIENT_ID",
                "OIDC_CLIENT_SECRET",
                "OIDC_ISSUER_URL",
            ],
            "callback_url": f"{base_url}/api/auth/oauth/oidc/callback",
        },
    ]

    return {
        "registration_enabled": reg_enabled,
        "effective_registration_enabled": reg_enabled or (user_count == 0),
        "user_count": user_count,
        "base_url": base_url,
        "callback_url_template": f"{base_url}/api/auth/oauth/{{provider}}/callback",
        "providers": providers,
    }
