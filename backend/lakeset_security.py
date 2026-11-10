"""Zero-Login SSO & Trusted Proxy Security Manager for Lakeset BI Studio.

Provides:
1. Trusted proxy header signing and verification (`X-Lakeset-User-Email`, `X-Lakeset-User-Name`,
   `X-Lakeset-User-Role`, `X-Lakeset-Workspace`, `X-Lakeset-Proxy-Secret`, `X-Lakeset-Signature`).
2. Lake of Tears role to Superset role mapping (`superadmin` -> `Admin`, workspace writers -> `Alpha`,
   workspace viewers -> `Gamma`).
3. `LakesetSecurityManager` compatible with Flask-AppBuilder / Apache Superset (`superset_config.py`)
   for zero-login user auto-provisioning and session authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

import jwt

logger = logging.getLogger("lake.backend.lakeset.security")

HEADER_USER_EMAIL = "X-Lakeset-User-Email"
HEADER_USER_NAME = "X-Lakeset-User-Name"
HEADER_USER_ROLE = "X-Lakeset-User-Role"
HEADER_WORKSPACE = "X-Lakeset-Workspace"
HEADER_PROXY_SECRET = "X-Lakeset-Proxy-Secret"
HEADER_SIGNATURE = "X-Lakeset-Signature"


def get_proxy_secret() -> str:
    """Resolve the shared proxy secret used between LakeUI, Backend, and Superset."""
    return (
        os.getenv("LAKESET_PROXY_SECRET")
        or os.getenv("AUTH_SECRET_KEY")
        or os.getenv("SUPERSET_SECRET_KEY")
        or "dev-secret-please-change-in-production"
    )


def map_lot_role_to_superset(lot_role: str | None) -> str:
    """Map a Lake of Tears global or workspace role to a Superset RBAC role."""
    normalized = (lot_role or "viewer").strip().lower()
    if normalized == "superadmin":
        return "Admin"
    if normalized in ("owner", "admin", "editor", "member", "user", "alpha"):
        return "Alpha"
    return "Gamma"


def compute_proxy_signature(
    email: str,
    role: str,
    workspace: str = "default",
    secret: str | None = None,
) -> str:
    """Compute HMAC-SHA256 signature over the proxy identity tuple."""
    key = (secret or get_proxy_secret()).encode("utf-8")
    msg = f"{email.strip().lower()}|{role.strip().lower()}|{workspace.strip().lower()}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def build_proxy_headers(
    email: str,
    display_name: str | None = None,
    role: str = "viewer",
    workspace: str = "default",
    secret: str | None = None,
) -> dict[str, str]:
    """Construct signed zero-login proxy headers for upstream Lakeset / Superset requests."""
    resolved_secret = secret or get_proxy_secret()
    clean_email = (email or "anonymous@lake.local").strip()
    clean_name = (display_name or clean_email.split("@")[0]).strip()
    clean_role = (role or "viewer").strip().lower()
    clean_ws = (workspace or "default").strip()
    sig = compute_proxy_signature(clean_email, clean_role, clean_ws, secret=resolved_secret)
    return {
        HEADER_USER_EMAIL: clean_email,
        HEADER_USER_NAME: clean_name,
        HEADER_USER_ROLE: clean_role,
        HEADER_WORKSPACE: clean_ws,
        HEADER_PROXY_SECRET: resolved_secret,
        HEADER_SIGNATURE: sig,
    }


def verify_proxy_headers(
    headers: dict[str, Any],
    cookies: dict[str, str] | None = None,
    expected_secret: str | None = None,
) -> dict[str, str] | None:
    """Verify incoming Lakeset proxy headers or `lake_token` cookie.

    Returns a normalized identity dictionary:
      {"email": ..., "username": ..., "first_name": ..., "last_name": ...,
       "lot_role": ..., "superset_role": ..., "workspace": ...}
    or None if unauthenticated / invalid.
    """
    secret = expected_secret or get_proxy_secret()
    norm_headers = {str(k).lower(): str(v) for k, v in (headers or {}).items() if v is not None}

    email = norm_headers.get(HEADER_USER_EMAIL.lower(), "").strip()
    name = norm_headers.get(HEADER_USER_NAME.lower(), "").strip()
    lot_role = norm_headers.get(HEADER_USER_ROLE.lower(), "viewer").strip()
    workspace = norm_headers.get(HEADER_WORKSPACE.lower(), "default").strip()
    proxy_secret = norm_headers.get(HEADER_PROXY_SECRET.lower(), "").strip()
    signature = norm_headers.get(HEADER_SIGNATURE.lower(), "").strip()

    authenticated = False
    if email and proxy_secret and hmac.compare_digest(proxy_secret, secret):
        if signature:
            expected_sig = compute_proxy_signature(email, lot_role, workspace, secret=secret)
            authenticated = hmac.compare_digest(signature, expected_sig)
        else:
            authenticated = True

    if not authenticated and cookies:
        lake_token = cookies.get("lake_token")
        if lake_token:
            try:
                payload = jwt.decode(lake_token, secret, algorithms=["HS256"])
                email = str(payload.get("email") or payload.get("sub") or "").strip()
                name = str(payload.get("display_name") or payload.get("name") or "").strip()
                lot_role = str(payload.get("role") or "user").strip()
                authenticated = bool(email)
            except Exception:
                authenticated = False

    if not authenticated or not email:
        return None

    display = name or email.split("@")[0]
    parts = display.split(None, 1)
    first_name = parts[0] if parts else "Lake"
    last_name = parts[1] if len(parts) > 1 else "User"
    superset_role = map_lot_role_to_superset(lot_role)

    return {
        "email": email,
        "username": email,
        "first_name": first_name,
        "last_name": last_name,
        "display_name": display,
        "lot_role": lot_role,
        "superset_role": superset_role,
        "workspace": workspace or "default",
    }


class LakesetSecurityManagerBase:
    """Mixin / base implementation of the Zero-Login `LakesetSecurityManager` for Superset.

    When running inside the Superset container, `superset_config.py` subclasses
    `SupersetSecurityManager` with this logic so every proxied request automatically
    provisions and logs in the Lake of Tears user with zero login screen.
    """

    def sync_lakeset_user(self, identity: dict[str, str]) -> Any:
        """Find or auto-create a Superset user and synchronize their mapped role."""
        email = identity["email"]
        username = identity["username"]
        superset_role_name = identity["superset_role"]

        role_obj = self.find_role(superset_role_name) or self.find_role("Gamma")
        user = self.find_user(username=username) or self.find_user(email=email)

        if not user:
            user = self.add_user(
                username=username,
                first_name=identity["first_name"],
                last_name=identity["last_name"],
                email=email,
                role=[role_obj] if role_obj else [],
            )
        else:
            user.first_name = identity["first_name"]
            user.last_name = identity["last_name"]
            user.active = True
            if role_obj and role_obj not in getattr(user, "roles", []):
                user.roles = [role_obj]
            if hasattr(self, "update_user"):
                self.update_user(user)

        return user

    def authenticate_request(
        self,
        headers: dict[str, Any],
        cookies: dict[str, str] | None = None,
    ) -> Any | None:
        """Verify request headers/cookies and return the provisioned Superset user."""
        identity = verify_proxy_headers(headers=headers, cookies=cookies)
        if not identity:
            return None
        return self.sync_lakeset_user(identity)
