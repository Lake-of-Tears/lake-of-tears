from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON

repo_root = Path(__file__).parents[1]
backend_dir = str(repo_root / "backend")
ui_dir = str(repo_root / "ui")

if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

# Load backend app
backend_spec = importlib.util.spec_from_file_location(
    "backend_main_service_mgmt_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_service_mgmt_test"] = backend_main
backend_spec.loader.exec_module(backend_main)

# Load UI app
ui_spec = importlib.util.spec_from_file_location(
    "ui_main_service_mgmt_test", repo_root / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_service_mgmt_test"] = ui_main
ui_spec.loader.exec_module(ui_main)

import auth  # noqa: E402
import database  # noqa: E402
from models import Base, User, UserSession  # noqa: E402
from services_manager.k8s_manager import (  # noqa: E402
    SAFEGUARDED_SERVICES,
    KubernetesServiceManager,
    get_service_manager,
)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionTesting = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionTesting()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def superadmin_user(db_session):
    user = User(
        id=uuid.uuid4(),
        email="superadmin@lakeoftears.io",
        display_name="Super Admin",
        role="superadmin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def regular_user(db_session):
    user = User(
        id=uuid.uuid4(),
        email="dev@lakeoftears.io",
        display_name="Developer",
        role="user",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def backend_client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    backend_main.app.dependency_overrides[database.get_db] = override_get_db
    client = TestClient(backend_main.app)
    yield client
    backend_main.app.dependency_overrides.clear()


def make_token(user: User, db_session) -> str:
    sess_id = uuid.uuid4()
    db_session.add(
        UserSession(
            id=sess_id,
            user_id=user.id,
            created_at=datetime.now(UTC),
            last_active_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            is_revoked=False,
        )
    )
    db_session.commit()
    db_session.refresh(user)
    return auth.create_token(
        {"sub": str(user.id), "email": user.email, "role": user.role},
        session_id=str(sess_id),
    )


# =============================================================================
# UNIT TESTS: KUBERNETES SERVICE MANAGER
# =============================================================================


def test_service_manager_safeguards():
    mgr = KubernetesServiceManager()

    # Attempting to stop core services must raise ValueError
    for srv in SAFEGUARDED_SERVICES:
        with pytest.raises(ValueError) as exc:
            mgr.stop_service(srv)
        assert "safeguarded and cannot be stopped" in str(exc.value)


def test_service_manager_status_mapping():
    mgr = KubernetesServiceManager()

    # Mock deployments and pods
    mock_deployments = [
        {
            "metadata": {"name": "lake-of-tears-backend"},
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "image": "lake-of-tears-backend:latest",
                                "ports": [{"containerPort": 8000}],
                            }
                        ]
                    }
                },
            },
            "status": {"readyReplicas": 1},
        },
        {
            "metadata": {"name": "lake-of-tears-jupyter"},
            "spec": {"replicas": 0},
            "status": {"readyReplicas": 0},
        },
        {
            "metadata": {"name": "lake-of-tears-airflow"},
            "spec": {"replicas": 1},
            "status": {"readyReplicas": 0},
        },
    ]
    mock_pods = [
        {
            "metadata": {
                "name": "lake-of-tears-backend-789-abc",
                "labels": {"app": "lake-of-tears-backend"},
                "creationTimestamp": "2026-11-05T04:00:00Z",
            },
            "spec": {
                "nodeName": "node-1",
                "containers": [{"resources": {"limits": {"cpu": "1000m", "memory": "2Gi"}}}],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [{"ready": True, "restartCount": 2}],
            },
        },
        {
            "metadata": {
                "name": "lake-of-tears-airflow-123-xyz",
                "labels": {"app": "lake-of-tears-airflow"},
                "creationTimestamp": "2026-11-05T04:30:00Z",
            },
            "spec": {"nodeName": "node-1"},
            "status": {
                "phase": "Pending",
                "containerStatuses": [{"state": {"waiting": {"reason": "ContainerCreating"}}}],
            },
        },
    ]

    with (
        patch.object(mgr, "_get_deployments", return_value=mock_deployments),
        patch.object(mgr, "_get_pods", return_value=mock_pods),
        patch.object(mgr, "is_cluster_accessible", return_value=True),
    ):
        overview = mgr.list_services()
        assert overview["cluster_connected"] is True
        assert overview["total_services"] == 3
        assert overview["healthy_count"] == 1

        services = {s["id"]: s for s in overview["services"]}

        # Backend should be running & core
        backend = services["backend"]
        assert backend["status"] == "running"
        assert backend["is_core"] is True
        assert backend["ready_replicas"] == 1
        assert backend["desired_replicas"] == 1
        assert backend["restarts"] == 2
        assert backend["cpu_limit"] == "1000m"
        assert backend["image"] == "lake-of-tears-backend:latest"
        assert backend["port"] == 8000

        # Jupyter should be stopped
        jupyter = services["jupyter"]
        assert jupyter["status"] == "stopped"
        assert jupyter["desired_replicas"] == 0
        assert jupyter["ready_replicas"] == 0

        # Airflow should be restarting
        airflow = services["airflow"]
        assert airflow["status"] == "restarting"
        assert airflow["is_core"] is False


def test_service_manager_lifecycle_actions():
    mgr = KubernetesServiceManager()
    mgr.mode = "kubectl_cli"

    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Restart
        res_restart = mgr.restart_service("airflow")
        assert res_restart["success"] is True
        assert res_restart["action"] == "restart"

        # Stop (non-core)
        res_stop = mgr.stop_service("airflow")
        assert res_stop["success"] is True
        assert res_stop["action"] == "stop"

        # Start
        res_start = mgr.start_service("airflow")
        assert res_start["success"] is True
        assert res_start["action"] == "start"


# =============================================================================
# INTEGRATION TESTS: BACKEND API ENDPOINTS
# =============================================================================


def test_backend_services_endpoints_auth(backend_client, regular_user, db_session):
    # Unauthenticated
    resp = backend_client.get("/api/admin/services")
    assert resp.status_code == 401

    # Regular user (forbidden)
    user_token = make_token(regular_user, db_session)
    resp = backend_client.get(
        "/api/admin/services", headers={"Authorization": f"Bearer {user_token}"}
    )
    assert resp.status_code == 403


def test_backend_services_list(backend_client, superadmin_user, db_session):
    admin_token = make_token(superadmin_user, db_session)
    resp = backend_client.get(
        "/api/admin/services", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "cluster_connected" in data
    assert "services" in data
    assert isinstance(data["services"], list)


def test_backend_services_lifecycle_api(backend_client, superadmin_user, db_session):
    admin_token = make_token(superadmin_user, db_session)
    headers = {"Authorization": f"Bearer {admin_token}"}

    mgr = get_service_manager()

    with (
        patch.object(mgr, "restart_service", return_value={"success": True, "action": "restart"}),
        patch.object(mgr, "stop_service", return_value={"success": True, "action": "stop"}),
        patch.object(mgr, "start_service", return_value={"success": True, "action": "start"}),
    ):
        # Restart
        resp = backend_client.post("/api/admin/services/airflow/restart", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["action"] == "restart"

        # Stop auxiliary service
        resp = backend_client.post("/api/admin/services/airflow/stop", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["action"] == "stop"

        # Start auxiliary service
        resp = backend_client.post("/api/admin/services/airflow/start", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["action"] == "start"

    # Stop Core Service must return 400 Bad Request
    resp = backend_client.post("/api/admin/services/backend/stop", headers=headers)
    assert resp.status_code == 400
    assert "safeguarded and cannot be stopped" in resp.json()["detail"]


# =============================================================================
# INTEGRATION TESTS: UI PROXY ENDPOINTS
# =============================================================================


def test_ui_proxy_services_endpoints(superadmin_user, regular_user, db_session):
    ui_client = TestClient(ui_main.app)
    admin_token = make_token(superadmin_user, db_session)
    user_token = make_token(regular_user, db_session)

    # Regular user forbidden
    ui_client.cookies.set("lake_token", user_token)
    resp = ui_client.get("/settings/admin/services")
    assert resp.status_code == 403

    # Superadmin proxy listing
    ui_client.cookies.set("lake_token", admin_token)
    mock_resp_data = {
        "cluster_connected": True,
        "services": [{"id": "airflow", "display_name": "Apache Airflow", "status": "running"}],
        "healthy_count": 1,
        "total_services": 1,
    }

    with patch("httpx.Client.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: mock_resp_data)
        resp = ui_client.get("/settings/admin/services")
        assert resp.status_code == 200
        assert resp.json()["cluster_connected"] is True

    # Superadmin proxy actions
    with patch("httpx.Client.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"success": True, "message": "Service restart initiated."},
        )
        resp = ui_client.post("/settings/admin/services/airflow/restart")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        resp = ui_client.post("/settings/admin/services/airflow/stop")
        assert resp.status_code == 200

        resp = ui_client.post("/settings/admin/services/airflow/start")
        assert resp.status_code == 200


# =============================================================================
# INTEGRATION TESTS: UI ADMIN PANEL TEMPLATE & TABS
# =============================================================================


def test_admin_panel_template_tabs_structure():
    template_path = repo_root / "ui" / "templates" / "settings" / "admin.html"
    assert template_path.exists()
    content = template_path.read_text(encoding="utf-8")

    # Expanded real estate
    assert "max-w-6xl" in content

    # All 5 tab navigation buttons
    assert 'id="admin-tab-btn-services"' in content
    assert 'id="admin-tab-btn-users"' in content
    assert 'id="admin-tab-btn-workspaces"' in content
    assert 'id="admin-tab-btn-vaults"' in content
    assert 'id="admin-tab-btn-platform"' in content

    # All 5 tab content panels
    assert 'id="admin-panel-services"' in content
    assert 'id="admin-panel-users"' in content
    assert 'id="admin-panel-workspaces"' in content
    assert 'id="admin-panel-vaults"' in content
    assert 'id="admin-panel-platform"' in content

    # Minimalist List View & Slide-Over Drawer
    assert 'id="services-list-container"' in content
    assert 'id="service-drawer"' in content
    assert 'id="service-drawer-overlay"' in content
    assert "openServiceDrawer(" in content
    assert "closeServiceDrawer(" in content
    assert 'id="drawer-service-actions"' in content

    # Confirmation modal & Auto-poll controls
    assert 'id="service-confirm-modal"' in content
    assert 'id="services-autopoll-toggle"' in content
    assert 'id="services-health-badge"' in content
    assert "switchAdminTab(" in content
