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
            superset_dashboard_id=1,
            embedded_uuid="uuid-1",
            title="Sales Overview",
            slug="sales-overview",
            charts_count=4,
            is_published=True,
        ),
        WorkspaceDashboard(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            superset_dashboard_id=2,
            embedded_uuid="uuid-2",
            title="Draft KPIs",
            slug="draft-kpis",
            charts_count=2,
            is_published=False,
        ),
    ]

    mock_db.query.return_value.filter.return_value.all.return_value = mock_dashboards

    with (
        patch(
            "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "viewer")
        ),
        patch.object(superset_service.superset_gateway, "is_healthy", return_value=True),
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
        superset_dashboard_id=10,
        embedded_uuid="embed-uuid-10",
        title="Revenue Trends",
        slug="revenue-trends",
        description="Monthly revenue chart",
        charts_count=5,
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
        assert dashboards[0]["charts_count"] == 5
        assert dashboards[0]["embedded_uuid"] == "embed-uuid-10"


def test_create_dashboard_as_editor():
    user, workspace, member, token = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    # Slug conflict check: None
    mock_db.query.return_value.filter.return_value.first.return_value = None

    with (
        patch(
            "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "editor")
        ),
        patch.object(
            superset_service.superset_gateway, "create_dashboard", return_value=(42, "embed-42")
        ),
    ):
        payload = {
            "title": "Marketing Funnel",
            "description": "Weekly lead attribution",
            "is_published": True,
        }
        resp = client.post(
            "/api/v1/dashboards",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
            json=payload,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "Marketing Funnel"
        assert data["slug"] == "marketing-funnel"
        assert data["superset_dashboard_id"] == 42
        assert data["embedded_uuid"] == "embed-42"
        assert mock_db.add.called
        assert mock_db.commit.called


def test_create_dashboard_denied_for_viewer():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    # Simulating auth check raising 403 for viewer
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


def test_link_existing_superset_dashboard():
    user, workspace, member, token = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    # 1. Existing link check -> None (not linked)
    # 2. Slug check -> None (slug free)
    mock_db.query.return_value.filter.return_value.first.side_effect = [None, None]

    mock_superset_info = {
        "dashboard_title": "Imported Logistics",
        "position_json": '{"CHART-1": {"type": "CHART"}, "CHART-2": {"type": "CHART"}}',
    }

    with (
        patch(
            "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "editor")
        ),
        patch.object(
            superset_service.superset_gateway, "get_dashboard", return_value=mock_superset_info
        ),
        patch.object(
            superset_service.superset_gateway,
            "get_or_create_embedded_config",
            return_value="embed-logistics",
        ),
    ):
        payload = {
            "superset_dashboard_id": 99,
        }
        resp = client.post(
            "/api/v1/dashboards/link",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
            json=payload,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "Imported Logistics"
        assert data["superset_dashboard_id"] == 99
        assert data["embedded_uuid"] == "embed-logistics"
        assert data["charts_count"] == 2


def test_link_duplicate_dashboard_rejected():
    user, workspace, member, token = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    existing_dash = WorkspaceDashboard(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        superset_dashboard_id=99,
        embedded_uuid="existing-uuid",
        title="Already Linked",
        slug="already-linked",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = existing_dash

    with patch(
        "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "editor")
    ):
        resp = client.post(
            "/api/v1/dashboards/link",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
            json={"superset_dashboard_id": 99},
        )
        assert resp.status_code == 400
        assert "already linked" in resp.json()["detail"]


def test_get_dashboard_guest_token():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    dash_id = uuid.uuid4()
    mock_dash = WorkspaceDashboard(
        id=dash_id,
        workspace_id=workspace.id,
        superset_dashboard_id=12,
        embedded_uuid="uuid-guest-12",
        title="Live Traffic",
        slug="live-traffic",
        charts_count=3,
        is_published=True,
    )

    mock_db.query.return_value.filter.return_value.first.return_value = mock_dash

    with (
        patch(
            "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "viewer")
        ),
        patch.object(
            superset_service.superset_gateway,
            "mint_guest_token",
            return_value="signed-guest-token-xyz",
        ),
    ):
        resp = client.get(
            f"/api/v1/dashboards/{dash_id}/guest-token",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["token"] == "signed-guest-token-xyz"
        assert data["embedded_uuid"] == "uuid-guest-12"
        assert data["superset_dashboard_id"] == 12


def test_delete_dashboard_with_purge_superset():
    user, workspace, member, token = _generate_test_user_and_token(role="admin")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    dash_id = uuid.uuid4()
    mock_dash = WorkspaceDashboard(
        id=dash_id,
        workspace_id=workspace.id,
        superset_dashboard_id=55,
        embedded_uuid="embed-55",
        title="Old Quarterly",
        slug="old-quarterly",
    )

    mock_db.query.return_value.filter.return_value.first.return_value = mock_dash

    with (
        patch(
            "dashboards_router.get_dashboard_workspace_and_role", return_value=(workspace, "admin")
        ),
        patch.object(
            superset_service.superset_gateway, "delete_dashboard", return_value=True
        ) as mock_delete_superset,
    ):
        resp = client.delete(
            f"/api/v1/dashboards/{dash_id}?purge_superset=true",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Slug": workspace.slug},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["purged_from_superset"] is True
        assert mock_delete_superset.called
        assert mock_db.delete.called
