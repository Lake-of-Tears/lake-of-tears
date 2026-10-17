from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import jwt
from starlette.testclient import TestClient

# Ensure ui directory is in sys.path
sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import main  # noqa: E402

client = TestClient(main.app)


def _make_token(sub="00000000-0000-0000-0000-000000000001", role="admin"):
    return jwt.encode(
        {"sub": sub, "email": "test@lake.local", "role": role},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )


# ── Backend Helper Tests ───────────────────────────────────────────────────────


def test_backend_post_with_error_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": "cat-1", "name": "production"}

    with patch("httpx.Client.post", return_value=mock_resp):
        data, err = main._backend_post_with_error("/api/catalogs", "tok", {"name": "production"})
        assert data == {"id": "cat-1", "name": "production"}
        assert err is None


def test_backend_post_with_error_conflict_409():
    mock_resp = MagicMock()
    mock_resp.status_code = 409
    mock_resp.json.return_value = {
        "detail": "A catalog with that name already exists in this workspace"
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        data, err = main._backend_post_with_error("/api/catalogs", "tok", {"name": "default"})
        assert data is None
        assert err == "A catalog with that name already exists in this workspace"


def test_backend_post_with_error_validation_422():
    mock_resp = MagicMock()
    mock_resp.status_code = 422
    mock_resp.json.return_value = {"detail": [{"msg": "workspace_id is required"}]}

    with patch("httpx.Client.post", return_value=mock_resp):
        data, err = main._backend_post_with_error("/api/catalogs", "tok", {"name": "foo"})
        assert data is None
        assert "workspace_id is required" in err


def test_backend_post_with_error_timeout():
    with patch("httpx.Client.post", side_effect=httpx.TimeoutException("timeout")):
        data, err = main._backend_post_with_error("/api/catalogs", "tok", {"name": "foo"})
        assert data is None
        assert err == "Backend request timed out"


def test_backend_post_with_error_unreachable():
    with patch("httpx.Client.post", side_effect=Exception("connection refused")):
        data, err = main._backend_post_with_error("/api/catalogs", "tok", {"name": "foo"})
        assert data is None
        assert err == "Backend service unreachable"


def test_backend_post_delegates_to_with_error():
    with patch.object(
        main,
        "_backend_post_with_error",
        return_value=({"id": "cat-1"}, None),
    ):
        assert main._backend_post("/api/catalogs", "tok", {}) == {"id": "cat-1"}

    with patch.object(
        main,
        "_backend_post_with_error",
        return_value=(None, "error"),
    ):
        assert main._backend_post("/api/catalogs", "tok", {}) is None


# ── _fetch_workspaces & AuthMiddleware Tests ───────────────────────────────────


def test_fetch_workspaces_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"id": "ws-1", "name": "Default"}]

    with patch("httpx.Client.get", return_value=mock_resp):
        res = main._fetch_workspaces("valid-token")
        assert res == [{"id": "ws-1", "name": "Default"}]


def test_fetch_workspaces_401_returns_none():
    mock_resp = MagicMock()
    mock_resp.status_code = 401

    with patch("httpx.Client.get", return_value=mock_resp):
        res = main._fetch_workspaces("invalid-or-expired-token")
        assert res is None


def test_auth_middleware_redirects_and_clears_cookie_on_401():
    token = _make_token()
    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=None),
    ):
        res = client.get(
            "/catalog",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            follow_redirects=False,
        )
        assert res.status_code == 302
        assert "/login?reason=session_expired" in res.headers.get("location", "")
        # Verify cookies are cleared
        set_cookie = res.headers.get("set-cookie", "")
        assert 'lake_token=""' in set_cookie or "Max-Age=0" in set_cookie


# ── Catalog Page Rendering Tests ───────────────────────────────────────────────


def test_catalog_page_renders_without_error():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default"}]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_get", return_value=[]),
    ):
        res = client.get(
            "/catalog",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert "catalog-error-banner" not in res.text
        assert "Create catalog" in res.text


def test_catalog_page_renders_error_banner_when_present():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default"}]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_get", return_value=[]),
    ):
        res = client.get(
            "/catalog?error=A+catalog+with+that+name+already+exists",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert "catalog-error-banner" in res.text
        assert "A catalog with that name already exists" in res.text


def test_catalog_page_missing_workspace_renders_modal_warning():
    token = _make_token()

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=[]),
        patch.object(main, "_backend_get", return_value=[]),
    ):
        res = client.get(
            "/catalog",
            cookies={"lake_token": token},
        )
        assert res.status_code == 200
        assert "No active workspace found" in res.text


# ── Catalog Create Route Tests ─────────────────────────────────────────────────


def test_catalog_create_unauthenticated_redirects_login():
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.post(
            "/catalog/create",
            data={"name": "test", "description": ""},
            follow_redirects=False,
        )
        assert res.status_code == 302
        assert "/login" in res.headers.get("location", "")


def test_catalog_create_no_workspace_redirects_with_error():
    token = _make_token()

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=[]),
    ):
        res = client.post(
            "/catalog/create",
            data={"name": "new_cat", "workspace_id": ""},
            cookies={"lake_token": token},
            follow_redirects=False,
        )
        assert res.status_code == 302
        loc = res.headers.get("location", "")
        assert "/catalog?error=" in loc
        assert "No+active+workspace+selected" in loc or "workspace" in loc.lower()


def test_catalog_create_empty_name_redirects_with_error():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default"}]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
    ):
        res = client.post(
            "/catalog/create",
            data={"name": "   ", "workspace_id": "ws-1"},
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            follow_redirects=False,
        )
        assert res.status_code == 302
        loc = res.headers.get("location", "")
        assert "/catalog?error=" in loc
        assert "empty" in loc.lower()


def test_catalog_create_backend_conflict_redirects_with_error():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default"}]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(
            main,
            "_backend_post_with_error",
            return_value=(None, "A catalog with that name already exists in this workspace"),
        ),
    ):
        res = client.post(
            "/catalog/create",
            data={"name": "default", "workspace_id": "ws-1"},
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            follow_redirects=False,
        )
        assert res.status_code == 302
        loc = res.headers.get("location", "")
        assert "/catalog?error=" in loc
        assert "already+exists" in loc or "already exists" in loc


def test_catalog_create_success_redirects_to_catalog():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default"}]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(
            main,
            "_backend_post_with_error",
            return_value=({"id": "cat-new", "name": "analytics"}, None),
        ),
    ):
        res = client.post(
            "/catalog/create",
            data={"name": "analytics", "workspace_id": "ws-1"},
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            follow_redirects=False,
        )
        assert res.status_code == 302
        assert res.headers.get("location") == "/catalog"


# ── /api/catalog/preview Tests ────────────────────────────────────────────────


def test_catalog_preview_requires_auth():
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.get("/catalog/preview?view=bronze.tmdb_trending")
        assert res.status_code == 401
        assert res.json()["ok"] is False


def test_catalog_preview_rejects_invalid_view_name():
    token = _make_token()
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.get(
            "/catalog/preview?view=invalid;drop table;--",
            cookies={"lake_token": token},
        )
        assert res.status_code == 400
        assert res.json()["ok"] is False
        assert "Invalid view identifier" in res.json()["error"]


def test_catalog_preview_success():
    token = _make_token()
    mock_query_result = {
        "ok": True,
        "columns": ["id", "title"],
        "rows": [[1, "Inception"], [2, "Interstellar"]],
        "row_count": 2,
        "execution_ms": 1.25,
    }

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_resolve_ui_catalogs", return_value=[]),
        patch.object(main, "_run_query", return_value=mock_query_result),
    ):
        res = client.get(
            "/catalog/preview?view=bronze.tmdb_trending&limit=10",
            cookies={"lake_token": token},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["columns"] == ["id", "title"]
        assert len(data["rows"]) == 2
        assert data["row_count"] == 2
        assert data["execution_ms"] == 1.25
