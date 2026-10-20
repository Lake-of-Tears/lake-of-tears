from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import jwt
from starlette.testclient import TestClient

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
import pipeline_service  # noqa: E402
from models import Catalog, User, Workspace  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_main_isolation", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_isolation"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


def _create_user(role: str = "member", global_role: str = "user") -> tuple[User, str]:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"{role}_{global_role}_{user_id.hex[:6]}@lake.local",
        display_name=f"{role.title()} User",
        role=global_role,
        is_active=True,
    )
    token = jwt.encode(
        {"sub": str(user_id), "email": user.email, "role": user.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    return user, token


def _create_workspace(name: str, slug: str) -> Workspace:
    return Workspace(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        status="active",
    )


# ── Strict Workspace Context Enforcement (400 Bad Request) ────────────────────


def test_missing_workspace_context_returns_400():
    user, token = _create_user(role="member", global_role="user")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = user

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        # 1. Pipelines router without workspace context
        resp = client.get(
            "/api/v1/pipelines",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400
        assert "Active workspace context" in resp.json()["detail"]

        # 2. Catalogs endpoint without workspace context
        resp_cat = client.get(
            "/api/catalogs",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp_cat.status_code == 400
        assert "Active workspace context" in resp_cat.json()["detail"]

        # 3. Dashboards stats without workspace context
        resp_dash = client.get(
            "/api/v1/dashboards/stats",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp_dash.status_code == 400
        assert "Active workspace context" in resp_dash.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()


# ── Workspace Not Found (404) & Non-Member Access (403) ────────────────────────


def test_invalid_workspace_returns_404():
    user, token = _create_user(role="member", global_role="user")
    mock_db = MagicMock()
    # Workspace lookup returns None
    mock_db.query.return_value.filter.return_value.first.return_value = None

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        resp = client.get(
            "/api/v1/pipelines",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Workspace-Slug": "non-existent-workspace",
            },
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Workspace not found"
    finally:
        backend_main.app.dependency_overrides.clear()


def test_cross_workspace_non_member_returns_403():
    user, token = _create_user(role="member", global_role="user")
    ws = _create_workspace("Secret Project", "secret-project")

    mock_db = MagicMock()
    # First query for Workspace returns ws, second query for WorkspaceMember returns None
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, None]

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        resp = client.get(
            "/api/v1/pipelines",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Workspace-Id": str(ws.id),
            },
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not a member of this workspace"
    finally:
        backend_main.app.dependency_overrides.clear()


# ── Superadmin Workspace Switching & Jailing ──────────────────────────────────


def test_superadmin_can_switch_to_any_workspace_without_membership_row():
    superadmin, token = _create_user(role="admin", global_role="superadmin")
    ws = _create_workspace("Target Workspace", "target-ws")

    mock_db = MagicMock()
    # Workspace query returns ws; WorkspaceMember returns None (superadmin has no explicit row)
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, None]

    resolved_ws, role = auth.get_workspace_context(
        x_workspace_id=str(ws.id),
        current_user=superadmin,
        db=mock_db,
    )
    assert resolved_ws.id == ws.id
    assert role == "superadmin"


def test_pipeline_dag_accessibility_strictly_jailed_for_superadmin():
    ws_analytics = _create_workspace("Analytics", "analytics")
    ws_finance = _create_workspace("Finance", "finance")

    dag_analytics = {
        "dag_id": "ws_analytics_etl",
        "tags": [{"name": "workspace:analytics"}],
    }
    dag_finance = {
        "dag_id": "ws_finance_payroll",
        "tags": [{"name": "workspace:finance"}],
    }
    dag_system_runner = {
        "dag_id": "run_notebook",
        "tags": [{"name": "system"}],
    }

    # In analytics workspace:
    # Superadmin must NOT see finance DAG, even with is_superadmin=True
    assert (
        pipeline_service.is_dag_accessible_by_workspace(
            dag_analytics, ws_analytics.slug, is_superadmin=True
        )
        is True
    )
    assert (
        pipeline_service.is_dag_accessible_by_workspace(
            dag_system_runner, ws_analytics.slug, is_superadmin=True
        )
        is True
    )
    assert (
        pipeline_service.is_dag_accessible_by_workspace(
            dag_finance, ws_analytics.slug, is_superadmin=True
        )
        is False
    )

    # In finance workspace:
    assert (
        pipeline_service.is_dag_accessible_by_workspace(
            dag_finance, ws_finance.slug, is_superadmin=True
        )
        is True
    )
    assert (
        pipeline_service.is_dag_accessible_by_workspace(
            dag_analytics, ws_finance.slug, is_superadmin=True
        )
        is False
    )


def test_superadmin_catalogs_jailed_to_active_workspace():
    superadmin, token = _create_user(role="admin", global_role="superadmin")
    ws_a = _create_workspace("Workspace A", "ws-a")
    ws_b = _create_workspace("Workspace B", "ws-b")

    cat_a = Catalog(
        id=uuid.uuid4(),
        name="Catalog Alpha",
        slug="catalog-alpha",
        owner_workspace_id=ws_a.id,
        created_at=datetime.now(UTC),
        is_default=False,
        schemas=[],
    )
    cat_b = Catalog(
        id=uuid.uuid4(),
        name="Catalog Beta",
        slug="catalog-beta",
        owner_workspace_id=ws_b.id,
        created_at=datetime.now(UTC),
        is_default=False,
        schemas=[],
    )

    mock_db = MagicMock()
    # Workspace resolution for ws_a
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws_a, None]
    # Catalogs query returns only cat_a
    mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [cat_a]

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: superadmin

    try:
        resp = client.get(
            "/api/catalogs",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Workspace-Id": str(ws_a.id),
            },
        )
        assert resp.status_code == 200
        catalogs = resp.json()
        assert len(catalogs) == 1
        assert catalogs[0]["name"] == "Catalog Alpha"
        assert cat_b.name == "Catalog Beta"
    finally:
        backend_main.app.dependency_overrides.clear()


def test_superadmin_cross_workspace_dashboard_not_found():
    superadmin, token = _create_user(role="admin", global_role="superadmin")
    ws_a = _create_workspace("Workspace A", "ws-a")
    ws_b = _create_workspace("Workspace B", "ws-b")
    assert ws_b.id != ws_a.id

    dashboard_b_id = uuid.uuid4()

    mock_db = MagicMock()
    # Workspace resolution resolves ws_a
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        ws_a,  # workspace query
        None,  # membership query
        None,  # dashboard query in ws_a returns None
    ]

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: superadmin

    try:
        resp = client.get(
            f"/api/v1/dashboards/{dashboard_b_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Workspace-Id": str(ws_a.id),
            },
        )
        # Dashboard from Workspace B accessed via Workspace A context must return 404
        assert resp.status_code == 404
    finally:
        backend_main.app.dependency_overrides.clear()
