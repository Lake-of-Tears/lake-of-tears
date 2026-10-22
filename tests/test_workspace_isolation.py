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
from models import Catalog, User, Workspace, WorkspaceMember  # noqa: E402

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
    # Workspace returns ws, WorkspaceMember returns None, WorkspaceShare returns None
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, None, None]

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


# ── Role Simplification (read, write, admin) & Superadmin Workspace Creation ──


def test_non_superadmin_cannot_create_workspace():
    user, token = _create_user(role="member", global_role="user")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        resp = client.post(
            "/api/workspaces",
            json={"name": "Engineering Workspace", "description": "Eng team"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert "Superadmin access required" in resp.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()


def test_superadmin_can_create_workspace():
    superadmin, token = _create_user(role="admin", global_role="superadmin")
    mock_db = MagicMock()
    # Name conflict lookup returns None
    mock_db.query.return_value.filter.return_value.first.return_value = None

    def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = datetime.now(UTC)
        obj.is_system = False
        obj.status = "active"
        obj.members = [WorkspaceMember(workspace_id=obj.id, user_id=superadmin.id, role="admin")]

    mock_db.refresh.side_effect = _refresh

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: superadmin

    try:
        resp = client.post(
            "/api/workspaces",
            json={"name": "Analytics Dept", "description": "Analytics Workspace"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Analytics Dept"
        assert data["slug"] == "analytics-dept"
        # Verify WorkspaceMember created with role 'admin'
        added_member = next(
            (
                call.args[0]
                for call in mock_db.add.call_args_list
                if isinstance(call.args[0], WorkspaceMember)
            ),
            None,
        )
        assert added_member is not None
        assert added_member.role == "admin"
    finally:
        backend_main.app.dependency_overrides.clear()


def test_workspace_member_management_roles_and_permissions():
    user_read, token_read = _create_user(role="reader", global_role="user")
    user_write, token_write = _create_user(role="writer", global_role="user")
    user_admin, token_admin = _create_user(role="admin", global_role="user")
    target_user = User(
        id=uuid.uuid4(),
        email="target_user@example.com",
        display_name="Target User",
        role="user",
        is_active=True,
    )

    ws = _create_workspace("Shared WS", "shared-ws")
    ws.members = [
        WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=user_read.id, role="read"),
        WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=user_write.id, role="write"),
        WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=user_admin.id, role="admin"),
    ]

    mock_db = MagicMock()

    # 1. Read member attempting to add a member gets 403
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, None]
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_read
    try:
        resp = client.post(
            f"/api/workspaces/{ws.id}/members",
            json={"email": target_user.email, "role": "write"},
            headers={"Authorization": f"Bearer {token_read}"},
        )
        assert resp.status_code == 403
        assert "Workspace admin access required" in resp.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()

    # 2. Write member attempting to add a member gets 403
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, None]
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_write
    try:
        resp = client.post(
            f"/api/workspaces/{ws.id}/members",
            json={"email": target_user.email, "role": "read"},
            headers={"Authorization": f"Bearer {token_write}"},
        )
        assert resp.status_code == 403
        assert "Workspace admin access required" in resp.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()

    # 3. Workspace admin adding member with invalid role gets 422
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, None]
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_admin
    try:
        resp = client.post(
            f"/api/workspaces/{ws.id}/members",
            json={"email": target_user.email, "role": "manager"},
            headers={"Authorization": f"Bearer {token_admin}"},
        )
        assert resp.status_code == 422
    finally:
        backend_main.app.dependency_overrides.clear()

    # 4. Workspace admin successfully adding member with 'write' role
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, target_user]
    mock_db.query.return_value.filter_by.return_value.first.return_value = None
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_admin
    try:
        resp = client.post(
            f"/api/workspaces/{ws.id}/members",
            json={"email": target_user.email, "role": "write"},
            headers={"Authorization": f"Bearer {token_admin}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        added = next(
            (
                call.args[0]
                for call in mock_db.add.call_args_list
                if isinstance(call.args[0], WorkspaceMember)
            ),
            None,
        )
        assert added is not None
        assert added.role == "write"
    finally:
        backend_main.app.dependency_overrides.clear()


def test_superadmin_workspace_member_role_is_locked_to_admin():
    admin_user, token_admin = _create_user(role="admin", global_role="user")
    superadmin_user, _ = _create_user(role="root", global_role="superadmin")
    superadmin_user.email = "superadmin@example.com"
    ws = _create_workspace("Production", "production")

    sa_member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        user_id=superadmin_user.id,
        role="read",
        joined_at=datetime.now(UTC),
    )
    sa_member.user = superadmin_user
    regular_member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        user_id=admin_user.id,
        role="admin",
        joined_at=datetime.now(UTC),
    )
    regular_member.user = admin_user
    ws.members = [regular_member, sa_member]

    mock_db = MagicMock()

    # 1. Listing members returns role="admin" and is_superadmin=True for superadmin
    mock_db.query.return_value.filter.return_value.first.return_value = ws
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: admin_user
    try:
        resp = client.get(
            f"/api/workspaces/{ws.id}/members",
            headers={"Authorization": f"Bearer {token_admin}"},
        )
        assert resp.status_code == 200
        members = resp.json()
        sa_entry = next((m for m in members if m["user_id"] == str(superadmin_user.id)), None)
        assert sa_entry is not None
        assert sa_entry["role"] == "admin"
        assert sa_entry["is_superadmin"] is True
        assert sa_entry["global_role"] == "superadmin"
    finally:
        backend_main.app.dependency_overrides.clear()

    # 2. Attempting to PATCH superadmin's workspace role returns 400
    mock_db.query.return_value.filter.return_value.first.return_value = ws
    mock_db.query.return_value.filter_by.return_value.first.return_value = sa_member
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: admin_user
    try:
        resp = client.patch(
            f"/api/workspaces/{ws.id}/members/{superadmin_user.id}",
            json={"role": "read"},
            headers={"Authorization": f"Bearer {token_admin}"},
        )
        assert resp.status_code == 400
        assert "Cannot change the workspace role of a superadmin" in resp.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()

    # 3. Attempting to DELETE superadmin from workspace returns 400
    mock_db.query.return_value.filter.return_value.first.return_value = ws
    mock_db.query.return_value.filter_by.return_value.first.return_value = sa_member
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: admin_user
    try:
        resp = client.delete(
            f"/api/workspaces/{ws.id}/members/{superadmin_user.id}",
            headers={"Authorization": f"Bearer {token_admin}"},
        )
        assert resp.status_code == 400
        assert "Cannot remove a superadmin from a workspace" in resp.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()

    # 4. Adding superadmin to workspace always forces role='admin'
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, superadmin_user]
    mock_db.query.return_value.filter_by.return_value.first.return_value = None
    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: admin_user
    try:
        resp = client.post(
            f"/api/workspaces/{ws.id}/members",
            json={"email": superadmin_user.email, "role": "read"},
            headers={"Authorization": f"Bearer {token_admin}"},
        )
        assert resp.status_code == 200
        added = next(
            (
                call.args[0]
                for call in mock_db.add.call_args_list
                if isinstance(call.args[0], WorkspaceMember)
            ),
            None,
        )
        assert added is not None
        assert added.role == "admin"
    finally:
        backend_main.app.dependency_overrides.clear()


def test_workspace_read_role_write_enforcement():
    user, _ = _create_user(role="reader", global_role="user")
    superadmin, _ = _create_user(role="admin", global_role="superadmin")
    ws = _create_workspace("Test WS", "test-ws")

    member_read = WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=user.id, role="read")
    member_write = WorkspaceMember(
        id=uuid.uuid4(), workspace_id=ws.id, user_id=user.id, role="write"
    )
    member_admin = WorkspaceMember(
        id=uuid.uuid4(), workspace_id=ws.id, user_id=user.id, role="admin"
    )

    mock_db = MagicMock()

    # 1. Read role with require_write=True raises 403
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, member_read]
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        auth.get_workspace_context(
            x_workspace_id=str(ws.id),
            current_user=user,
            db=mock_db,
            require_write=True,
        )
    assert exc_info.value.status_code == 403
    assert "Workspace read role is read-only" in exc_info.value.detail

    # 2. Write role with require_write=True succeeds
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, member_write]
    resolved_ws, role = auth.get_workspace_context(
        x_workspace_id=str(ws.id),
        current_user=user,
        db=mock_db,
        require_write=True,
    )
    assert resolved_ws.id == ws.id
    assert role == "write"

    # 3. Admin role with require_write=True succeeds
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, member_admin]
    resolved_ws, role = auth.get_workspace_context(
        x_workspace_id=str(ws.id),
        current_user=user,
        db=mock_db,
        require_write=True,
    )
    assert resolved_ws.id == ws.id
    assert role == "admin"

    # 4. Superadmin succeeds even if without explicit member row
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, None]
    resolved_ws, role = auth.get_workspace_context(
        x_workspace_id=str(ws.id),
        current_user=superadmin,
        db=mock_db,
        require_write=True,
    )
    assert resolved_ws.id == ws.id
    assert role == "superadmin"


def test_alembic_migration_0010_script():
    migration_file = (
        Path(__file__).parents[1]
        / "backend"
        / "alembic"
        / "versions"
        / "0010_workspace_member_roles_read_write_admin.py"
    )
    assert migration_file.exists()

    spec = importlib.util.spec_from_file_location("mig_0010", migration_file)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    assert mig.revision == "0010"
    assert mig.down_revision == "0009"
    assert callable(mig.upgrade)
    assert callable(mig.downgrade)
