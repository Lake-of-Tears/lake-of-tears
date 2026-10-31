from __future__ import annotations

import os
import secrets
from urllib.parse import urlencode

import httpx
import jwt
from auth import (
    COOKIE_NAME,
    SECRET_KEY,
    create_token,
    get_session_expires_delta,
    is_registration_enabled,
)
from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from models import OAuthAccount, User, Workspace, WorkspaceMember
from sqlalchemy.orm import Session

_STATE_COOKIE = "oauth_state"
_BASE_URL = lambda: os.getenv("AUTH_BASE_URL", "http://localhost")  # noqa: E731
_OIDC_DISCOVERY_CACHE: dict[str, dict] = {}


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


def enabled_providers() -> list[dict]:
    checks = [
        ("google", "GOOGLE_CLIENT_ID", "Google"),
        ("github", "GITHUB_CLIENT_ID", "GitHub"),
        ("microsoft", "MICROSOFT_CLIENT_ID", "Microsoft Entra ID"),
        ("okta", "OKTA_CLIENT_ID", "Okta"),
        ("gitlab", "GITLAB_CLIENT_ID", "GitLab"),
        ("oidc", "OIDC_CLIENT_ID", os.getenv("OIDC_DISPLAY_NAME", "SSO")),
    ]
    return [{"key": k, "label": lbl} for k, env, lbl in checks if os.getenv(env, "")]


def get_oauth_redirect(provider: str, request: Request, response: Response) -> RedirectResponse:
    cfg = _provider_config(provider)
    if not cfg["client_id"]:
        raise HTTPException(400, f"{provider} OAuth not configured")

    state = secrets.token_urlsafe(32)
    next_url = request.query_params.get("next", "/")
    state_token = jwt.encode({"state": state, "next": next_url}, SECRET_KEY, algorithm="HS256")

    callback_url = f"{_BASE_URL()}/api/auth/oauth/{provider}/callback"
    auth_url = (
        cfg["auth_url"]
        + "?"
        + urlencode(
            {
                "client_id": cfg["client_id"],
                "redirect_uri": callback_url,
                "response_type": "code",
                "scope": cfg["scope"],
                "state": state,
            }
        )
    )

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


def handle_oauth_callback(provider: str, request: Request, response: Response, db: Session):
    # Verify state cookie
    state_token = request.cookies.get(_STATE_COOKIE)
    if not state_token:
        raise HTTPException(400, "Missing OAuth state — please try again")
    try:
        state_data = jwt.decode(state_token, SECRET_KEY, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(400, "Invalid OAuth state")
    if state_data.get("state") != request.query_params.get("state", ""):
        raise HTTPException(400, "OAuth state mismatch")

    code = request.query_params.get("code")
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

    access_token = token_resp.json().get("access_token", "")
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
        if not is_registration_enabled() and not is_first:
            resp = RedirectResponse(url="/login?error=registration_disabled")
            resp.delete_cookie(_STATE_COOKIE)
            return resp

        user = User(
            email=email,
            display_name=display_name or email.split("@")[0],
            role="superadmin" if is_first else "user",
        )
        db.add(user)
        db.flush()

        if is_first:
            # Bootstrap default workspace
            from main import _create_default_workspace

            _create_default_workspace(db, user)
        else:
            default_ws = db.query(Workspace).filter(Workspace.slug == "default").first()
            if default_ws:
                db.add(WorkspaceMember(workspace_id=default_ws.id, user_id=user.id, role="read"))

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
    token = create_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "display_name": user.display_name or "",
        },
        expires_delta=expires_delta,
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
