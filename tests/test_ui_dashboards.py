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

        # Verify NO raw iframe on gallery page
        assert "<iframe" not in response.text

        # Verify native gallery components
        assert 'id="dashboards-gallery"' in response.text
        assert 'id="dashboards-grid"' in response.text
        assert 'id="stats-container"' in response.text
        assert 'id="search-input"' in response.text
        assert 'id="create-modal"' in response.text
        assert "New Dashboard" in response.text


def test_get_dashboard_view_renders_native_viewer():
    token, mock_workspaces = _create_auth_cookie(role="viewer")
    dash_id = str(uuid.uuid4())

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            f"/dashboards/{dash_id}",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200

        # Verify native canvas & Apache ECharts (zero superset iframes/embedded-sdk)
        assert 'id="dashboard-view-container"' in response.text
        assert 'id="widgets-grid"' in response.text
        assert 'id="btn-fullscreen"' in response.text
        assert 'id="btn-refresh"' in response.text
        assert "echarts.min.js" in response.text
        assert "embedded-sdk" not in response.text
        assert "<iframe" not in response.text


def test_ui_proxy_get_dashboards():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_resp = [
        {
            "id": "dash-1",
            "title": "Ops Metrics",
            "slug": "ops-metrics",
            "charts_count": 3,
            "layout_config": [{"id": "w1"}],
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


def test_ui_proxy_get_widget_data():
    token, mock_workspaces = _create_auth_cookie(role="member")
    dash_id = "test-uuid"
    widget_id = "w-kpi-1"

    mock_widget_data = {
        "widget_id": widget_id,
        "type": "kpi",
        "title": "Total Rows",
        "data": {
            "value": "128,450",
            "trend": "+12%",
            "sparkline": [10, 20, 30],
        },
    }

    mock_httpx_resp = httpx.Response(
        status_code=200,
        json=mock_widget_data,
        request=httpx.Request(
            "GET", f"http://backend/api/v1/dashboards/{dash_id}/widgets/{widget_id}/data"
        ),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_httpx_resp),
    ):
        response = client.get(
            f"/api/dashboards/{dash_id}/widgets/{widget_id}/data",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200
        assert response.json()["widget_id"] == widget_id
        assert response.json()["data"]["value"] == "128,450"


def test_ui_proxy_create_dashboard():
    token, mock_workspaces = _create_auth_cookie(role="admin")

    mock_created = {
        "id": "dash-new",
        "title": "Finance Review",
        "slug": "finance-review",
        "charts_count": 0,
        "layout_config": [],
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
            json={"title": "Finance Review", "template": "blank"},
        )
        assert response.status_code == 201
        assert response.json()["title"] == "Finance Review"
        assert mock_post.called


def test_ui_proxy_add_widget():
    token, mock_workspaces = _create_auth_cookie(role="admin")
    dash_id = "dash-new"

    mock_updated_dash = {
        "id": dash_id,
        "title": "Finance Review",
        "charts_count": 1,
        "layout_config": [{"id": "w1", "type": "kpi", "title": "Processed"}],
    }

    mock_httpx_resp = httpx.Response(
        status_code=201,
        json=mock_updated_dash,
        request=httpx.Request("POST", f"http://backend/api/v1/dashboards/{dash_id}/widgets"),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch(
            "httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_httpx_resp
        ) as mock_post,
    ):
        response = client.post(
            f"/api/dashboards/{dash_id}/widgets",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            json={"title": "Processed", "type": "kpi"},
        )
        assert response.status_code == 201
        assert response.json()["charts_count"] == 1
        assert mock_post.called
