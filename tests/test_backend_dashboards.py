from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
from starlette.testclient import TestClient

# Ensure backend directory is in sys.path
backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
import superset_service  # noqa: E402
from models import User, Workspace, WorkspaceDashboard, WorkspaceMember  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_main_dashboards", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_dashboards"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


# ── Helpers & Fixtures ────────────────────────────────────────────────────────


def _generate_test_user_and_token(role: str = "viewer", global_role: str = "user"):
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"{role}@lake.local",
        display_name=role.title(),
        role=global_role,
        is_active=True,
    )
    token = jwt.encode(
        {"sub": str(user_id), "email": user.email, "role": user.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    ws_id = uuid.uuid4()
    workspace = Workspace(
        id=ws_id,
        name="Analytics Workspace",
        slug="analytics",
        status="active",
    )
    member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        user_id=user_id,
        role=role,
    )
    return user, workspace, member, token


# ── Backend Dashboards API Tests ──────────────────────────────────────────────


def test_get_dashboard_stats():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    mock_dashboards = [
        WorkspaceDashboard(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            title="Sales Overview",
            slug="sales-overview",
            layout_config=[{"id": "w1"}, {"id": "w2"}, {"id": "w3"}, {"id": "w4"}],
            charts_count=4,
            is_published=True,
        ),
        WorkspaceDashboard(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            title="Draft KPIs",
            slug="draft-kpis",
            layout_config=[{"id": "w5"}, {"id": "w6"}],
            charts_count=2,
            is_published=False,
        ),
    ]

    mock_db.query.return_value.filter.return_value.all.return_value = mock_dashboards

    with (
        patch(
            "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "viewer")
        ),
        patch.object(superset_service.headless_superset, "is_healthy", return_value=True),
    ):
        resp = client.get(
            "/api/v1/dashboards/stats",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_dashboards"] == 2
        assert data["published_dashboards"] == 1
        assert data["total_charts"] == 6
        assert data["superset_online"] is True


def test_list_dashboards():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    dash_id = uuid.uuid4()
    mock_dash = WorkspaceDashboard(
        id=dash_id,
        workspace_id=workspace.id,
        title="Revenue Trends",
        slug="revenue-trends",
        description="Monthly revenue chart",
        layout_config=[{"id": "w1", "type": "line"}],
        charts_count=1,
        is_published=True,
        created_by=user.id,
    )

    mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
        mock_dash
    ]

    with patch(
        "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "viewer")
    ):
        resp = client.get(
            "/api/v1/dashboards",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
        )
        assert resp.status_code == 200
        dashboards = resp.json()
        assert len(dashboards) == 1
        assert dashboards[0]["title"] == "Revenue Trends"
        assert dashboards[0]["slug"] == "revenue-trends"
        assert dashboards[0]["charts_count"] == 1
        assert len(dashboards[0]["layout_config"]) == 1


def test_create_dashboard_with_executive_template():
    user, workspace, member, token = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    # Slug conflict check: None
    mock_db.query.return_value.filter.return_value.first.return_value = None

    with patch(
        "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "editor")
    ):
        payload = {
            "title": "Executive KPI Overview",
            "description": "Weekly executive summary",
            "is_published": True,
            "template": "executive",
        }
        resp = client.post(
            "/api/v1/dashboards",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
            json=payload,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "Executive KPI Overview"
        assert data["slug"] == "executive-kpi-overview"
        assert data["charts_count"] == 5
        assert len(data["layout_config"]) == 5
        assert mock_db.add.called
        assert mock_db.commit.called


def test_create_dashboard_denied_for_viewer():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    from fastapi import HTTPException

    with patch(
        "dashboards_router.get_dashboard_workspace_and_role",
        side_effect=HTTPException(
            status_code=403,
            detail="Workspace viewer role is read-only. Cannot create, edit, or delete dashboards.",
        ),
    ):
        payload = {"title": "Attempted Dashboard"}
        resp = client.post(
            "/api/v1/dashboards",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
            json=payload,
        )
        assert resp.status_code == 403
        assert "viewer role is read-only" in resp.json()["detail"]


def test_widget_crud_lifecycle():
    user, workspace, member, token = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    dash_id = uuid.uuid4()
    mock_dash = WorkspaceDashboard(
        id=dash_id,
        workspace_id=workspace.id,
        title="Custom Dashboard",
        slug="custom-dashboard",
        layout_config=[],
        charts_count=0,
        is_published=True,
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_dash

    with patch(
        "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "editor")
    ):
        # 1. Add Widget
        add_payload = {
            "id": "w_test_bar",
            "title": "Throughput Bar Chart",
            "type": "bar",
            "dataset_id": "ds_datalake_storage",
            "metric": "bytes",
            "width": "half",
        }
        resp = client.post(
            f"/api/v1/dashboards/{dash_id}/widgets",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
            json=add_payload,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data["layout_config"]) == 1
        assert data["layout_config"][0]["id"] == "w_test_bar"

        # 2. Update Widget
        update_payload = {"title": "Updated Bar Chart", "width": "full"}
        resp = client.patch(
            f"/api/v1/dashboards/{dash_id}/widgets/w_test_bar",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
            json=update_payload,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["layout_config"][0]["title"] == "Updated Bar Chart"
        assert data["layout_config"][0]["width"] == "full"

        # 3. Delete Widget
        resp = client.delete(
            f"/api/v1/dashboards/{dash_id}/widgets/w_test_bar",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["layout_config"]) == 0


def test_get_widget_data_series():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    dash_id = uuid.uuid4()
    mock_dash = WorkspaceDashboard(
        id=dash_id,
        workspace_id=workspace.id,
        title="Live Traffic",
        slug="live-traffic",
        layout_config=[
            {
                "id": "w_kpi_1",
                "title": "Active Objects",
                "type": "kpi",
                "metric": "total_rows",
            }
        ],
        charts_count=1,
        is_published=True,
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_dash

    with patch(
        "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "viewer")
    ):
        resp = client.get(
            f"/api/v1/dashboards/{dash_id}/widgets/w_kpi_1/data",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["widget_id"] == "w_kpi_1"
        assert data["type"] == "kpi"
        assert "value" in data["data"]
        assert "sparkline" in data["data"]


def test_delete_dashboard():
    user, workspace, member, token = _generate_test_user_and_token(role="admin")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    dash_id = uuid.uuid4()
    mock_dash = WorkspaceDashboard(
        id=dash_id,
        workspace_id=workspace.id,
        title="Old Dashboard",
        slug="old-dashboard",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_dash

    with patch(
        "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "admin")
    ):
        resp = client.delete(
            f"/api/v1/dashboards/{dash_id}",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert mock_db.delete.called


def test_list_datasets():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    with patch(
        "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "viewer")
    ):
        resp = client.get(
            "/api/v1/dashboards/datasets",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert "name" in data[0]
