from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
from starlette.testclient import TestClient

# Ensure ui directory is in sys.path
sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import main  # noqa: E402

client = TestClient(main.app)


def _make_token(sub="00000000-0000-0000-0000-000000000001", role="user"):
    return jwt.encode(
        {"sub": sub, "email": "test@lake.local", "role": role},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )


def test_base_html_renders_global_shared_toggle_and_disconnect_modal():
    """Verify base.html only renders global toggle button when shared access exists."""
    tok = _make_token()
    workspaces_no_shared = [
        {
            "id": "ws-1",
            "name": "Marketing",
            "slug": "marketing",
            "my_role": "admin",
            "is_shared": False,
            "has_shared_catalogs": False,
        },
    ]

    with patch.object(main, "_fetch_workspaces", return_value=workspaces_no_shared):
        resp = client.get("/", cookies={"lake_token": tok, "lake_workspace_id": "ws-1"})
        assert resp.status_code == 200
        html = resp.text
        assert 'id="global-shared-objects-toggle"' not in html

    workspaces_with_shared_catalog = [
        {
            "id": "ws-1",
            "name": "Marketing",
            "slug": "marketing",
            "my_role": "admin",
            "is_shared": False,
            "has_shared_catalogs": True,
        },
    ]

    with patch.object(main, "_fetch_workspaces", return_value=workspaces_with_shared_catalog):
        resp = client.get("/", cookies={"lake_token": tok, "lake_workspace_id": "ws-1"})
        assert resp.status_code == 200
        html = resp.text
        # Global shared objects toggle
        assert 'id="global-shared-objects-toggle"' in html
        assert "toggleGlobalSharedObjects()" in html
        assert "hide-shared-objects" in html
        # Disconnect modal
        assert 'id="disconnect-share-modal"' in html
        assert 'id="btn-confirm-disconnect"' in html
        assert "executeDisconnectShare()" in html


def test_catalog_renders_shared_with_you_section():
    """Verify catalog.html groups native catalogs and renders collapsible 'Shared with You' section."""
    tok = _make_token()
    workspaces = [
        {
            "id": "ws-native",
            "name": "Native Workspace",
            "slug": "native",
            "my_role": "admin",
            "is_shared": False,
        },
        {
            "id": "ws-shared",
            "name": "Shared Analytics",
            "slug": "shared-analytics",
            "my_role": "read",
            "is_shared": True,
        },
    ]

    native_catalogs = [
        {
            "id": "cat-1",
            "name": "native_dw",
            "owner_workspace_id": "ws-native",
            "my_access": "owner",
            "schemas": [],
        }
    ]
    shared_catalogs = [
        {
            "id": "cat-2",
            "name": "analytics_mart",
            "owner_workspace_id": "ws-shared",
            "my_access": "read",
            "schemas": [],
        }
    ]

    def mock_backend_get(path, token, workspace_id=None, **kwargs):
        if "ws-native" in path:
            return native_catalogs
        if "ws-shared" in path:
            return shared_catalogs
        return []

    with (
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(main, "_backend_get", side_effect=mock_backend_get),
    ):
        resp = client.get("/catalog", cookies={"lake_token": tok, "lake_workspace_id": "ws-native"})
        assert resp.status_code == 200
        html = resp.text
        assert "Native Workspace Catalogs" in html
        assert "native_dw" in html
        assert "Shared with You" in html
        assert "Shared Analytics" in html
        assert "analytics_mart" in html
        assert "🔗 Shared · Read-Only" in html
        assert "Disconnect Share" in html
        assert 'data-shared-workspace-id="ws-shared"' in html


def test_sql_renders_shared_catalogs_in_sidebar():
    """Verify sql.html renders shared catalogs in the reference schema sidebar."""
    tok = _make_token()
    workspaces = [
        {
            "id": "ws-native",
            "name": "Native WS",
            "slug": "native",
            "my_role": "admin",
            "is_shared": False,
        },
        {
            "id": "ws-shared",
            "name": "Partner WS",
            "slug": "partner",
            "my_role": "read",
            "is_shared": True,
        },
    ]

    shared_catalogs = [
        {
            "id": "cat-shared",
            "name": "partner_dw",
            "owner_workspace_id": "ws-shared",
            "my_access": "read",
            "schemas": [
                {
                    "id": "sch-1",
                    "name": "public",
                    "slug": "public",
                    "tables": [
                        {
                            "id": "t1",
                            "name": "customers",
                            "slug": "customers",
                            "s3_path_pattern": "s3://...",
                        }
                    ],
                }
            ],
        }
    ]

    def mock_backend_get(path, token, workspace_id=None, **kwargs):
        if "ws-shared" in path:
            return shared_catalogs
        return []

    with (
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(main, "_backend_get", side_effect=mock_backend_get),
    ):
        resp = client.get("/sql", cookies={"lake_token": tok, "lake_workspace_id": "ws-native"})
        assert resp.status_code == 200
        html = resp.text
        assert "Shared with You" in html
        assert "Partner WS" in html
        assert "customers" in html


def test_notebooks_renders_shared_with_you_section():
    """Verify notebooks.html renders shared workspaces section with disconnect button."""
    tok = _make_token()
    workspaces = [
        {
            "id": "ws-native",
            "name": "Native WS",
            "slug": "native",
            "my_role": "admin",
            "is_shared": False,
        },
        {
            "id": "ws-shared",
            "name": "Shared WS",
            "slug": "shared-ws",
            "my_role": "read",
            "is_shared": True,
        },
    ]

    with patch.object(main, "_fetch_workspaces", return_value=workspaces):
        resp = client.get(
            "/notebooks", cookies={"lake_token": tok, "lake_workspace_id": "ws-native"}
        )
        assert resp.status_code == 200
        html = resp.text
        assert "Native Files" in html
        assert "Shared with You" in html
        assert "Shared WS" in html
        assert "loadSharedWorkspaceFiles" in html
        assert "Disconnect" in html


def test_pipelines_renders_shared_with_you_section():
    """Verify pipelines.html renders shared workspaces section."""
    tok = _make_token()
    workspaces = [
        {
            "id": "ws-native",
            "name": "Native WS",
            "slug": "native",
            "my_role": "admin",
            "is_shared": False,
        },
        {
            "id": "ws-shared",
            "name": "Pipelines WS",
            "slug": "pipelines-ws",
            "my_role": "read",
            "is_shared": True,
        },
    ]

    with patch.object(main, "_fetch_workspaces", return_value=workspaces):
        resp = client.get(
            "/pipelines", cookies={"lake_token": tok, "lake_workspace_id": "ws-native"}
        )
        assert resp.status_code == 200
        html = resp.text
        assert "Shared with You" in html
        assert "Pipelines WS" in html
        assert "fetchSharedWorkspacePipelines" in html


def test_storage_renders_shared_with_you_section():
    """Verify storage.html renders shared workspaces section."""
    tok = _make_token()
    workspaces = [
        {
            "id": "ws-native",
            "name": "Native WS",
            "slug": "native",
            "my_role": "admin",
            "is_shared": False,
        },
        {
            "id": "ws-shared",
            "name": "Storage WS",
            "slug": "storage-ws",
            "my_role": "read",
            "is_shared": True,
        },
    ]

    with patch.object(main, "_fetch_workspaces", return_value=workspaces):
        resp = client.get("/storage", cookies={"lake_token": tok, "lake_workspace_id": "ws-native"})
        assert resp.status_code == 200
        html = resp.text
        assert "Shared with You" in html
        assert "Storage WS" in html
        assert "fetchSharedWorkspaceStorage" in html


def test_ui_disconnect_workspace_share_proxy():
    """Verify DELETE /api/workspaces/{workspace_id}/shares proxies to backend."""
    tok = _make_token()
    mock_resp = MagicMock()
    mock_resp.status_code = 204

    with patch("httpx.AsyncClient.delete", return_value=mock_resp):
        resp = client.delete("/api/workspaces/ws-target/shares", cookies={"lake_token": tok})
        assert resp.status_code == 204


def test_notebook_auth_and_workspace_targets_shared_workspace():
    """Verify _get_notebook_auth_and_workspace resolves shared workspace when passed in query."""
    tok = _make_token()
    workspaces = [
        {
            "id": "ws-native",
            "name": "Native WS",
            "slug": "native",
            "my_role": "admin",
            "is_shared": False,
        },
        {
            "id": "ws-shared",
            "name": "Shared WS",
            "slug": "shared-slug",
            "my_role": "read",
            "is_shared": True,
        },
    ]

    with (
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch("notebook_proxy.list_workspace_notebooks", return_value={"items": []}) as mock_list,
    ):
        resp = client.get(
            "/notebooks/api?workspace=shared-slug",
            cookies={"lake_token": tok, "lake_workspace_id": "ws-native"},
        )
        assert resp.status_code == 200
        # Verified it queried the shared workspace slug!
        mock_list.assert_called_once_with("shared-slug", "", auth_token=tok)
