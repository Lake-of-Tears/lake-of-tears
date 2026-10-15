import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import jwt
from starlette.testclient import TestClient

# Ensure ui directory is in sys.path
sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import main  # noqa: E402

client = TestClient(main.app)


def _create_auth_cookie(role: str = "member", global_role: str = "user") -> tuple[str, list[dict]]:
    token = jwt.encode(
        {"sub": f"{role}@lake.local", "email": f"{role}@lake.local", "role": global_role},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    workspaces = [{"id": "ws-1", "name": "Analytics", "slug": "analytics", "my_role": role}]
    return token, workspaces


# ── UI Page Rendering Tests ───────────────────────────────────────────────────


def test_get_dashboards_gallery_renders_native_template():
    token, mock_workspaces = _create_auth_cookie(role="editor")

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            "/dashboards",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200

        # Verify NO legacy raw iframe on gallery page
        assert "<iframe" not in response.text

        # Verify native gallery components
        assert 'id="dashboards-gallery"' in response.text
        assert 'id="dashboards-grid"' in response.text
        assert 'id="stats-container"' in response.text
        assert 'id="search-input"' in response.text
        assert 'id="create-modal"' in response.text
        assert "New Dashboard" in response.text


def test_get_dashboard_view_renders_in_app_viewer():
    token, mock_workspaces = _create_auth_cookie(role="viewer")
    dash_id = str(uuid.uuid4())

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            f"/dashboards/{dash_id}",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200

        # Verify native toolbar & SDK mount point
        assert 'id="dashboard-view-container"' in response.text
        assert 'id="dashboard-mount-point"' in response.text
        assert 'id="btn-fullscreen"' in response.text
        assert 'id="btn-refresh"' in response.text
        assert "embedded-sdk" in response.text


def test_ui_proxy_get_dashboards():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_resp = [
        {
            "id": "dash-1",
            "title": "Ops Metrics",
            "slug": "ops-metrics",
            "charts_count": 3,
            "is_published": True,
        }
    ]

    mock_httpx_resp = httpx.Response(
        status_code=200,
        json=mock_resp,
        request=httpx.Request("GET", "http://backend/api/v1/dashboards"),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch(
            "httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_httpx_resp
        ) as mock_get,
    ):
        response = client.get(
            "/api/dashboards",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["title"] == "Ops Metrics"
        assert mock_get.called


def test_ui_proxy_get_guest_token():
    token, mock_workspaces = _create_auth_cookie(role="member")
    dash_id = "test-uuid"

    mock_token_data = {
        "token": "valid-guest-jwt-token",
        "embedded_uuid": "embed-uuid-1",
        "dashboard_id": dash_id,
        "superset_dashboard_id": 42,
        "superset_domain": "/superset",
    }

    mock_httpx_resp = httpx.Response(
        status_code=200,
        json=mock_token_data,
        request=httpx.Request("GET", f"http://backend/api/v1/dashboards/{dash_id}/guest-token"),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_httpx_resp),
    ):
        response = client.get(
            f"/api/dashboards/{dash_id}/guest-token",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200
        assert response.json()["token"] == "valid-guest-jwt-token"


def test_ui_proxy_create_dashboard():
    token, mock_workspaces = _create_auth_cookie(role="admin")

    mock_created = {
        "id": "dash-new",
        "title": "Finance Review",
        "slug": "finance-review",
        "superset_dashboard_id": 101,
        "embedded_uuid": "embed-101",
    }

    mock_httpx_resp = httpx.Response(
        status_code=201,
        json=mock_created,
        request=httpx.Request("POST", "http://backend/api/v1/dashboards"),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch(
            "httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_httpx_resp
        ) as mock_post,
    ):
        response = client.post(
            "/api/dashboards",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            json={"title": "Finance Review"},
        )
        assert response.status_code == 201
        assert response.json()["title"] == "Finance Review"
        assert mock_post.called
