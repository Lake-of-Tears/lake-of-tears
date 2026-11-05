from __future__ import annotations

import unittest.mock as mock
import uuid

import jwt
import pytest
from starlette.testclient import TestClient

from ui.main import _WORKSPACE_COOKIE, AUTH_SECRET_KEY, app


@pytest.fixture
def test_tokens():
    superadmin_token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "superadmin@lake.local", "role": "superadmin"},
        AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    user_token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "user@lake.local", "role": "user"},
        AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    return {"superadmin": superadmin_token, "user": user_token}


@pytest.fixture
def mock_workspaces():
    return [
        {
            "id": "ws-123",
            "name": "Finance Analytics",
            "slug": "finance-analytics",
            "my_role": "admin",
            "is_shared": False,
        }
    ]


# ── Admin Panel Vault Settings Tests ──────────────────────────────────────────


def test_admin_settings_vault_panel_renders(test_tokens, mock_workspaces):
    """Superadmin accessing /settings/admin sees the Secret Vaults configuration and scope audit inventory."""
    fake_providers = [
        {
            "provider": "internal",
            "display_name": "Internal Encrypted Database",
            "is_configured": True,
            "config": {"encryption": "AES-256-GCM / HMAC-SHA256"},
        },
        {
            "provider": "azure_keyvault",
            "display_name": "Azure Key Vault",
            "is_configured": True,
            "config": {"vault_url": "https://vault-test.vault.azure.net/", "client_secret": "••••••••"},
        },
        {
            "provider": "aws_secrets_manager",
            "display_name": "AWS Secrets Manager",
            "is_configured": False,
            "config": {"region_name": "us-east-1"},
        },
    ]
    fake_scopes = [
        {
            "id": "scope-1",
            "name": "prod-secrets",
            "workspace_id": "ws-123",
            "workspace_name": "Finance Analytics",
            "backend_type": "azure_kv",
            "key_count": 5,
            "created_at": "2026-11-04T12:00:00Z",
        }
    ]

    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces),
        mock.patch("ui.main._backend_get") as mock_get,
    ):
        def fake_backend_get(path, token, workspace_id=None, workspace_slug=None):
            if path == "/api/admin/vault-providers":
                return fake_providers
            if path == "/api/admin/vault-providers/scopes":
                return fake_scopes
            if path == "/api/users":
                return []
            if path == "/api/admin/workspaces":
                return mock_workspaces
            if path == "/api/admin/invitations":
                return []
            if path == "/api/admin/sessions":
                return []
            if path == "/api/admin/settings":
                return {}
            return None

        mock_get.side_effect = fake_backend_get
        client = TestClient(app)
        resp = client.get(
            "/settings/admin",
            cookies={"lake_token": test_tokens["superadmin"]},
        )
        assert resp.status_code == 200
        html = resp.text
        assert "Secret Vaults &amp; Cloud Providers" in html or "Secret Vaults & Cloud Providers" in html
        assert "Azure Key Vault" in html
        assert "AWS Secrets Manager" in html
        assert "prod-secrets" in html
        assert "Finance Analytics" in html


def test_admin_settings_vault_panel_rejects_non_superadmin(test_tokens, mock_workspaces):
    """Non-superadmin accessing /settings/admin gets redirected to /."""
    with mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces):
        client = TestClient(app)
        resp = client.get(
            "/settings/admin",
            cookies={"lake_token": test_tokens["user"]},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"


def test_admin_save_vault_provider_config(test_tokens, mock_workspaces):
    """Superadmin saving Azure Key Vault configuration puts config to backend and redirects with anchor."""
    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces),
        mock.patch("ui.main._backend_put", return_value=(200, {"provider": "azure_keyvault"})) as mock_put,
    ):
        client = TestClient(app)
        form_data = {
            "cfg__azure_keyvault__vault_url": "https://company-vault.vault.azure.net/",
            "cfg__azure_keyvault__auth_mode": "service_principal",
            "cfg__azure_keyvault__tenant_id": "ten-1234",
            "cfg__azure_keyvault__client_id": "app-5678",
            "cfg__azure_keyvault__client_secret": "my-secret-key-999",
        }
        resp = client.post(
            "/settings/admin/vault-providers/azure_keyvault",
            data=form_data,
            cookies={"lake_token": test_tokens["superadmin"]},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/settings/admin?saved=vault#vault-settings"
        mock_put.assert_called_once()
        args, kwargs = mock_put.call_args
        assert args[0] == "/api/admin/vault-providers/azure_keyvault"
        payload = args[2]
        assert payload["config"]["vault_url"] == "https://company-vault.vault.azure.net/"
        assert payload["config"]["client_secret"] == "my-secret-key-999"


def test_admin_test_vault_provider_connection(test_tokens, mock_workspaces):
    """Testing vault connection via AJAX proxies test request to backend and returns JSON result."""
    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces),
        mock.patch("httpx.Client") as mock_client_cls,
    ):
        mock_http_client = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message": "Successfully connected to Azure Key Vault."}
        mock_http_client.post.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_http_client

        client = TestClient(app)
        resp = client.post(
            "/settings/admin/vault-providers/azure_keyvault/test",
            json={"config": {"vault_url": "https://company-vault.vault.azure.net/"}},
            cookies={"lake_token": test_tokens["superadmin"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "Successfully connected" in data["message"]


# ── Workspace Secret Scopes UI Tests ──────────────────────────────────────────


def test_workspace_secret_scopes_list_renders(test_tokens, mock_workspaces):
    """Workspace settings page loads and renders secret scopes list and action buttons."""
    fake_scopes = [
        {
            "id": "scope-abc",
            "name": "etl-db-credentials",
            "workspace_id": "ws-123",
            "backend_type": "internal",
            "key_count": 2,
            "user_permission": "MANAGE",
        }
    ]

    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces),
        mock.patch("ui.main._backend_get") as mock_get,
    ):
        def fake_backend_get(path, token, workspace_id=None, workspace_slug=None):
            if path.endswith("/secrets/scopes"):
                return fake_scopes
            if path.endswith("/members"):
                return [{"user_id": "u-1", "email": "superadmin@lake.local", "role": "admin"}]
            if path.endswith("/settings/catalogs"):
                return {"owned": [], "shared": [], "shared_with_others": []}
            if path.endswith("/access/requests/inbound"):
                return []
            return None

        mock_get.side_effect = fake_backend_get
        client = TestClient(app)
        resp = client.get(
            "/settings/workspace",
            cookies={"lake_token": test_tokens["superadmin"], _WORKSPACE_COOKIE: "ws-123"},
        )
        assert resp.status_code == 200
        html = resp.text
        assert "Secret Scopes" in html
        assert "etl-db-credentials" in html
        assert "Internal DB" in html
        assert "Manage Keys" in html
        assert "Permissions" in html


def test_workspace_create_secret_scope(test_tokens, mock_workspaces):
    """Creating a new secret scope posts to backend and redirects with anchor."""
    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces),
        mock.patch("ui.main._backend_post_with_error", return_value=({"id": "new-scope"}, None)) as mock_post,
    ):
        client = TestClient(app)
        resp = client.post(
            "/settings/workspace/secrets/scopes/create",
            data={"name": "aws-api-scope", "backend_type": "aws_sm"},
            cookies={"lake_token": test_tokens["superadmin"], _WORKSPACE_COOKIE: "ws-123"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/settings/workspace?saved=scope_created#secret-scopes"
        mock_post.assert_called_once()
        path, tok, payload = mock_post.call_args[0]
        assert "/secrets/scopes" in path
        assert payload["name"] == "aws-api-scope"
        assert payload["backend_type"] == "aws_sm"


def test_workspace_delete_secret_scope(test_tokens, mock_workspaces):
    """Deleting a secret scope calls delete on backend and redirects with anchor."""
    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces),
        mock.patch("ui.main._backend_delete", return_value=True) as mock_delete,
    ):
        client = TestClient(app)
        resp = client.post(
            "/settings/workspace/secrets/scopes/scope-xyz/delete",
            cookies={"lake_token": test_tokens["superadmin"], _WORKSPACE_COOKIE: "ws-123"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/settings/workspace?saved=scope_deleted#secret-scopes"
        mock_delete.assert_called_once()


def test_workspace_keys_api_crud(test_tokens, mock_workspaces):
    """AJAX endpoints for listing, creating, and deleting keys in a secret scope."""
    fake_keys = [{"key": "postgres_pw", "version": 1, "updated_at": "2026-11-04T12:00:00Z"}]

    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces),
        mock.patch("ui.main._backend_get", return_value=fake_keys) as mock_get,
        mock.patch("ui.main._backend_post_with_error", return_value=({"key": "api_token", "version": 1}, None)) as mock_post,
        mock.patch("ui.main._backend_delete", return_value=True) as mock_delete,
    ):
        client = TestClient(app)
        cookies = {"lake_token": test_tokens["superadmin"], _WORKSPACE_COOKIE: "ws-123"}

        # 1. List keys
        resp = client.get("/api/workspaces/ws-123/secrets/scopes/scope-123/keys", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["key"] == "postgres_pw"
        mock_get.assert_called_once()

        # 2. Add / Rotate key
        resp = client.post(
            "/api/workspaces/ws-123/secrets/scopes/scope-123/keys",
            json={"key": "api_token", "value": "secret-val-456"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.json()["key"] == "api_token"
        mock_post.assert_called_once()

        # 3. Delete key
        resp = client.delete(
            "/api/workspaces/ws-123/secrets/scopes/scope-123/keys/postgres_pw",
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        mock_delete.assert_called_once()


def test_workspace_acls_api_crud(test_tokens, mock_workspaces):
    """AJAX endpoints for listing, setting, and deleting ACL permissions on a secret scope."""
    fake_acls = [
        {
            "id": str(uuid.uuid4()),
            "scope_id": "scope-123",
            "principal_type": "user",
            "principal_id": "user-456",
            "permission": "READ",
        }
    ]

    with (
        mock.patch("ui.main._fetch_workspaces", return_value=mock_workspaces),
        mock.patch("ui.main._backend_get", return_value=fake_acls) as mock_get,
        mock.patch("ui.main._backend_post_with_error", return_value=({"id": "acl-new", "permission": "WRITE"}, None)) as mock_post,
        mock.patch("ui.main._backend_delete", return_value=True) as mock_delete,
    ):
        client = TestClient(app)
        cookies = {"lake_token": test_tokens["superadmin"], _WORKSPACE_COOKIE: "ws-123"}

        # 1. List ACLs
        resp = client.get("/api/workspaces/ws-123/secrets/scopes/scope-123/acls", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["permission"] == "READ"
        mock_get.assert_called_once()

        # 2. Grant ACL
        resp = client.post(
            "/api/workspaces/ws-123/secrets/scopes/scope-123/acls",
            json={"principal_type": "user", "principal_id": "user-789", "permission": "WRITE"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.json()["permission"] == "WRITE"
        mock_post.assert_called_once()

        # 3. Delete ACL
        resp = client.delete(
            "/api/workspaces/ws-123/secrets/scopes/scope-123/acls/acl-new",
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        mock_delete.assert_called_once()
