from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON

repo_root = Path(__file__).parents[1]
backend_dir = str(repo_root / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

# Load backend app
backend_spec = importlib.util.spec_from_file_location(
    "backend_main_cluster_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_cluster_test"] = backend_main
backend_spec.loader.exec_module(backend_main)

import auth  # noqa: E402
import database  # noqa: E402
from models import Base, User, UserSession  # noqa: E402
from services_manager.k8s_manager import (  # noqa: E402
    KubernetesServiceManager,
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
# AUTHENTICATION & ACCESS CONTROL TESTS
# =============================================================================


def test_cluster_status_unauthenticated(backend_client):
    """GET /api/cluster/status requires authentication."""
    resp = backend_client.get("/api/cluster/status")
    assert resp.status_code == 401


def test_cluster_topology_unauthenticated(backend_client):
    """GET /api/cluster/topology requires authentication."""
    resp = backend_client.get("/api/cluster/topology")
    assert resp.status_code == 401


# =============================================================================
# CLUSTER STATUS API TESTS
# =============================================================================


def test_cluster_status_authenticated(backend_client, regular_user, db_session):
    """Authenticated users can inspect real-time cluster status."""
    token = make_token(regular_user, db_session)
    resp = backend_client.get(
        "/api/cluster/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] in ("healthy", "degraded", "down")
    assert "cluster_connected" in data
    assert "mode" in data
    assert "namespace" in data
    assert "timestamp" in data
    assert "total_services" in data
    assert "healthy_count" in data
    assert "services" in data

    services = data["services"]
    assert "duckdb" in services
    assert services["duckdb"]["status"] == "running"
    assert services["duckdb"]["display_name"] == "DuckDB Analytical Engine"
    assert services["duckdb"]["latency_ms"] is not None

    assert "backend" in services
    assert services["backend"]["status"] == "running"
    assert services["backend"]["port"] == 8000

    # Ensure all core services from issue #348 are reported
    for expected_svc in ("duckdb", "backend", "minio", "airflow", "jupyter", "superset"):
        assert expected_svc in services, f"Missing service {expected_svc}"
        svc_data = services[expected_svc]
        assert "name" in svc_data
        assert "display_name" in svc_data
        assert "status" in svc_data
        assert "category" in svc_data


def test_cluster_status_v1_alias(backend_client, regular_user, db_session):
    """GET /api/v1/cluster/status works as an alias."""
    token = make_token(regular_user, db_session)
    resp = backend_client.get(
        "/api/v1/cluster/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "duckdb" in data["services"]


# =============================================================================
# CLUSTER TOPOLOGY API TESTS
# =============================================================================


def test_cluster_topology_authenticated(backend_client, regular_user, db_session):
    """Authenticated users can inspect ingress routes and storage volume topology."""
    token = make_token(regular_user, db_session)
    resp = backend_client.get(
        "/api/cluster/topology",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert "namespace" in data
    assert "mode" in data
    assert "ingress_routes" in data
    assert "storage_volumes" in data

    routes = data["ingress_routes"]
    assert len(routes) > 0
    route_services = {r["service"] for r in routes}
    assert "lakeui" in route_services
    assert (
        "minio" in route_services
        or "minio-api" in route_services
        or "minio-console" in route_services
    )

    for r in routes:
        assert "url" in r
        assert "host" in r
        assert "path" in r
        assert "tls_enabled" in r

    volumes = data["storage_volumes"]
    assert len(volumes) > 0
    volume_names = {v["name"] for v in volumes}
    assert any("minio" in v for v in volume_names)
    assert any("postgres" in v for v in volume_names)

    for v in volumes:
        assert "name" in v
        assert "mount_path" in v
        assert "status" in v


# =============================================================================
# KUBERNETES LIVE HARNESS MOCK TESTS
# =============================================================================


def test_cluster_status_with_mocked_k8s(backend_client, regular_user, db_session):
    """When k8s returns deployments and pods, cluster status reflects pod metadata."""
    token = make_token(regular_user, db_session)
    mock_deployments = [
        {
            "metadata": {"name": "lake-of-tears-minio"},
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "image": "minio/minio:RELEASE.2024",
                                "resources": {"limits": {"cpu": "2", "memory": "4Gi"}},
                                "ports": [{"containerPort": 9000}],
                            }
                        ]
                    }
                },
            },
            "status": {"readyReplicas": 1},
        }
    ]
    mock_pods = [
        {
            "metadata": {
                "name": "lake-of-tears-minio-78b6c-x9k2l",
                "creationTimestamp": "2026-11-06T10:00:00Z",
                "labels": {"app": "lake-of-tears-minio"},
            },
            "spec": {
                "nodeName": "node-prod-1",
                "containers": [
                    {
                        "image": "minio/minio:RELEASE.2024",
                        "resources": {"limits": {"cpu": "2", "memory": "4Gi"}},
                    }
                ],
            },
            "status": {
                "containerStatuses": [{"restartCount": 2, "ready": True}],
            },
        }
    ]

    with patch.object(KubernetesServiceManager, "_get_deployments", return_value=mock_deployments):
        with patch.object(KubernetesServiceManager, "_get_pods", return_value=mock_pods):
            resp = backend_client.get(
                "/api/cluster/status",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            minio_info = data["services"]["minio"]
            assert minio_info["status"] == "running"
            assert minio_info["pod_name"] == "lake-of-tears-minio-78b6c-x9k2l"
            assert minio_info["node_name"] == "node-prod-1"
            assert minio_info["restarts"] == 2
            assert minio_info["cpu_limit"] == "2"
            assert minio_info["memory_limit"] == "4Gi"


def test_cluster_topology_with_mocked_k8s(backend_client, regular_user, db_session):
    """When k8s returns live Ingresses and PVCs, cluster topology parses them correctly."""
    token = make_token(regular_user, db_session)
    mock_ingresses = [
        {
            "metadata": {"name": "lake-ingress"},
            "spec": {
                "tls": [{"hosts": ["lake.internal.domain"]}],
                "rules": [
                    {
                        "host": "lake.internal.domain",
                        "http": {
                            "paths": [
                                {
                                    "path": "/api",
                                    "backend": {"service": {"name": "lake-of-tears-backend"}},
                                }
                            ]
                        },
                    }
                ],
            },
        }
    ]
    mock_pvcs = [
        {
            "metadata": {"name": "custom-lake-pvc"},
            "spec": {
                "storageClassName": "nfs-storage",
                "resources": {"requests": {"storage": "1Ti"}},
            },
            "status": {"phase": "Bound"},
        }
    ]

    with patch.object(KubernetesServiceManager, "_get_ingresses", return_value=mock_ingresses):
        with patch.object(KubernetesServiceManager, "_get_pvcs", return_value=mock_pvcs):
            resp = backend_client.get(
                "/api/cluster/topology",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()

            routes = data["ingress_routes"]
            assert len(routes) == 1
            assert routes[0]["service"] in ("backend", "lake-of-tears-backend")
            assert routes[0]["url"] == "https://lake.internal.domain/api"
            assert routes[0]["tls_enabled"] is True

            volumes = data["storage_volumes"]
            assert len(volumes) == 1
            assert volumes[0]["name"] == "custom-lake-pvc"
            assert volumes[0]["size"] == "1Ti"
            assert volumes[0]["storage_class"] == "nfs-storage"
            assert volumes[0]["status"] == "Bound"
