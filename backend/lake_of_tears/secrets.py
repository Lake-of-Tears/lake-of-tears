from __future__ import annotations

import os
import uuid

import httpx
from secrets_manager.redaction import register_secret_for_redaction


class _SecretsClient:
    """Client implementing Databricks dbutils.secrets parity."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        workspace_id: str | None = None,
        workspace_slug: str | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("LAKE_API_BASE")
            or os.environ.get("LAKE_URL")
            or "http://localhost:8000"
        ).rstrip("/")
        self.token = (
            token
            or os.environ.get("LAKE_TOKEN")
            or os.environ.get("LAKE_API_KEY")
            or os.environ.get("LAKE_AUTH_TOKEN")
        )
        self.workspace_id = (
            workspace_id or os.environ.get("LAKE_WORKSPACE_ID") or os.environ.get("WORKSPACE_ID")
        )
        self.workspace_slug = workspace_slug or os.environ.get("WORKSPACE_SLUG")

    @property
    def current_workspace_id(self) -> str | None:
        return (
            self.workspace_id
            or os.environ.get("LAKE_WORKSPACE_ID")
            or os.environ.get("WORKSPACE_ID")
        )

    @property
    def current_token(self) -> str | None:
        return (
            self.token
            or os.environ.get("LAKE_TOKEN")
            or os.environ.get("LAKE_API_KEY")
            or os.environ.get("LAKE_AUTH_TOKEN")
        )

    def _get_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        token = self.current_token
        ws_id = self.current_workspace_id
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if ws_id:
            headers["X-Workspace-Id"] = str(ws_id)
        if self.workspace_slug or os.environ.get("WORKSPACE_SLUG"):
            headers["X-Workspace-Slug"] = str(
                self.workspace_slug or os.environ.get("WORKSPACE_SLUG")
            )
        return headers

    def get(self, scope: str, key: str) -> str:
        """Fetch plaintext secret value for a key in a given scope.

        Redacts the value from execution logs automatically.
        """
        # 1. Try local direct DB resolution if running in-process
        ws_id = self.current_workspace_id
        if ws_id:
            try:
                from database import SessionLocal
                from secrets_manager.service import SecretManagerService

                db = SessionLocal()
                try:
                    svc = SecretManagerService(db)
                    parsed_ws_id = uuid.UUID(str(ws_id))
                    sc = svc.get_scope(parsed_ws_id, scope)
                    val, _, _ = svc.get_secret_value(sc, key)
                    register_secret_for_redaction(val)
                    return val
                finally:
                    db.close()
            except Exception:
                pass

        # 2. Remote HTTP API call
        url = f"{self.base_url}/api/v1/secrets/scopes/{scope}/keys/{key}"
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=self._get_headers())
            if resp.status_code == 404:
                raise KeyError(f"Secret '{key}' not found in scope '{scope}'")
            if resp.status_code == 403:
                raise PermissionError(f"Permission denied for secret '{key}' in scope '{scope}'")
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch secret: HTTP {resp.status_code} - {resp.text}")

            val = resp.json().get("value", "")
            register_secret_for_redaction(val)
            return val

    def list(self, scope: str) -> list[str]:
        """List all secret keys in a scope without revealing their values."""
        # Local direct DB resolution
        ws_id = self.current_workspace_id
        if ws_id:
            try:
                from database import SessionLocal
                from secrets_manager.service import SecretManagerService

                db = SessionLocal()
                try:
                    svc = SecretManagerService(db)
                    parsed_ws_id = uuid.UUID(str(ws_id))
                    sc = svc.get_scope(parsed_ws_id, scope)
                    keys = svc.list_keys_in_scope(sc)
                    return [k.key for k in keys]
                finally:
                    db.close()
            except Exception:
                pass

        url = f"{self.base_url}/api/v1/secrets/scopes/{scope}/keys"
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=self._get_headers())
            if resp.status_code == 404:
                raise KeyError(f"Secret scope '{scope}' not found")
            if resp.status_code == 403:
                raise PermissionError(f"Permission denied for scope '{scope}'")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to list secret keys: HTTP {resp.status_code} - {resp.text}"
                )

            return [item["key"] for item in resp.json()]

    def list_scopes(self) -> list[str]:
        """List all secret scopes accessible in the current workspace."""
        # Local direct DB resolution
        ws_id = self.current_workspace_id
        if ws_id:
            try:
                from database import SessionLocal
                from models import SecretScope

                db = SessionLocal()
                try:
                    parsed_ws_id = uuid.UUID(str(ws_id))
                    scopes = (
                        db.query(SecretScope.name)
                        .filter(SecretScope.workspace_id == parsed_ws_id)
                        .order_by(SecretScope.name.asc())
                        .all()
                    )
                    return [s[0] for s in scopes]
                finally:
                    db.close()
            except Exception:
                pass

        url = f"{self.base_url}/api/v1/secrets/scopes"
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to list scopes: HTTP {resp.status_code} - {resp.text}")

            return [item["name"] for item in resp.json()]


_default_client = _SecretsClient()


def get(scope: str, key: str) -> str:
    """Retrieve secret value (matches dbutils.secrets.get)."""
    return _default_client.get(scope, key)


def list(scope: str) -> list[str]:
    """List keys in secret scope (matches dbutils.secrets.list)."""
    return _default_client.list(scope)


def list_scopes() -> list[str]:
    """List accessible secret scopes (matches dbutils.secrets.listScopes)."""
    return _default_client.list_scopes()
