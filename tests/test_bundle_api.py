"""Integration and API unit tests for Lake Asset Bundles deployment engine and REST API suite (#423)."""

from __future__ import annotations

import ast
import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient


# Handle SQLite JSONB compilation in unit test environment
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
import pipeline_service  # noqa: E402
import storage_service  # noqa: E402
from database import Base  # noqa: E402
from models import (  # noqa: E402
    Catalog,
    CatalogTable,
    PipelineMetadata,
    User,
    Workspace,
    WorkspaceBundle,
    WorkspaceDashboard,
    WorkspaceMember,
)

spec = importlib.util.spec_from_file_location(
    "backend_main_bundles", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_bundles"] = backend_main
spec.loader.exec_module(backend_main)
app = backend_main.app

from backend.bundles import (  # noqa: E402
    generate_bundle_dag_code,
    parse_bundle_str,
)

SAMPLE_CUSTOMER360_TOON = """
bundle:
  name: customer360-medallion
  version: 0.1.0
  author: data-team@lake.local
  description: Customer 360 Medallion Architecture

variables:
  raw_bucket:
    default: datalake-bronze
    type: string

targets:
  dev:
    workspace: dev-workspace
    default_catalog: main_lake
    variables:
      raw_bucket: datalake-dev
  prod:
    workspace: prod-workspace
    default_catalog: main_lake
    variables:
      raw_bucket: datalake-prod

resources:
  catalogs[1]{name,description}:
    main_lake,Primary analytics lakehouse catalog

  schemas[3]{catalog,name,tier,description}:
    main_lake,bronze,bronze,Raw ingested customer events
    main_lake,silver,silver,Cleaned customer entities
    main_lake,gold,gold,Aggregated revenue analytical marts

  tables[2]{name,catalog,schema,format,s3_path_pattern}:
    raw_customers,main_lake,bronze,parquet,"s3://${var.raw_bucket}/customers/*.parquet"
    dim_customers,main_lake,silver,parquet,"s3://${var.raw_bucket}/silver/dim_customers/*.parquet"

  vector_indexes[1]{name,table,column,metric}:
    customer_notes_idx,main_lake.silver.dim_customers,notes,cosine

  pipelines:
    customer_medallion_pipeline:
      description: Daily ingestion and medallion dimensional build
      schedule: "0 2 * * *"
      tasks[3]{name,type,entrypoint,upstream_tasks}:
        extract_events,duckdb_sql,sql/extract.sql,[]
        clean_silver,duckdb_sql,sql/silver_transform.sql,[extract_events]
        build_gold_marts,duckdb_sql,sql/gold_marts.sql,[clean_silver]

  dashboards:
    executive_overview:
      description: Executive KPI Dashboard
      widgets:
        revenue_metric:
          type: metric
          query: "SELECT sum(ltv) FROM main_lake.gold.customer_ltv"
          width: 4
          height: 2
"""


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db_session(tmp_path):
    """Temporary SQLite test database session with StaticPool."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def mock_dags_dir(tmp_path):
    """Temporary Airflow DAGs directory."""
    dags_dir = tmp_path / "airflow_dags"
    dags_dir.mkdir(parents=True, exist_ok=True)
    with patch.object(pipeline_service, "get_airflow_dags_dir", return_value=dags_dir):
        yield dags_dir


@pytest.fixture
def test_setup(db_session: Session):
    """Create test users, workspace, and workspace memberships."""
    ws_id = uuid.uuid4()
    workspace = Workspace(
        id=ws_id,
        name="Data Platform",
        slug="data-platform",
        status="active",
        created_at=datetime.now(UTC),
    )
    db_session.add(workspace)

    # Admin user
    admin_user = User(
        id=uuid.uuid4(),
        email="admin@lake.local",
        display_name="Admin User",
        role="user",
        is_active=True,
    )
    db_session.add(admin_user)
    db_session.flush()

    admin_membership = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=admin_user.id,
        role="admin",
    )
    db_session.add(admin_membership)

    # Read-only viewer
    viewer_user = User(
        id=uuid.uuid4(),
        email="viewer@lake.local",
        display_name="Viewer User",
        role="user",
        is_active=True,
    )
    db_session.add(viewer_user)
    db_session.flush()

    viewer_membership = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=viewer_user.id,
        role="viewer",
    )
    db_session.add(viewer_membership)

    # Other workspace
    other_ws = Workspace(
        id=uuid.uuid4(),
        name="Other Workspace",
        slug="other-ws",
        status="active",
        created_at=datetime.now(UTC),
    )
    db_session.add(other_ws)

    db_session.commit()

    return {
        "workspace": workspace,
        "admin_user": admin_user,
        "viewer_user": viewer_user,
        "other_workspace": other_ws,
    }


@pytest.fixture
def client_admin(db_session: Session, test_setup: dict, mock_dags_dir: Path):
    """Test client authenticated as admin user."""
    admin_user = test_setup["admin_user"]
    ws = test_setup["workspace"]

    app.dependency_overrides[database.get_db] = lambda: db_session
    app.dependency_overrides[auth.get_current_user] = lambda: admin_user

    test_client = TestClient(app, cookies={"lake_active_workspace": str(ws.id)})
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def client_viewer(db_session: Session, test_setup: dict, mock_dags_dir: Path):
    """Test client authenticated as viewer (read-only) user."""
    viewer_user = test_setup["viewer_user"]
    ws = test_setup["workspace"]

    app.dependency_overrides[database.get_db] = lambda: db_session
    app.dependency_overrides[auth.get_current_user] = lambda: viewer_user

    test_client = TestClient(app, cookies={"lake_active_workspace": str(ws.id)})
    yield test_client
    app.dependency_overrides.clear()


# ── Unit Tests: DAG Code Generation ──────────────────────────────────────────


def test_generate_bundle_dag_code_valid():
    """Verify that multi-task bundle pipelines compile into syntactically valid Python DAGs."""
    spec = parse_bundle_str(SAMPLE_CUSTOMER360_TOON, format="toon")
    pipeline = spec.resources.pipelines[0]

    dag_id, code = generate_bundle_dag_code(
        workspace_slug="analytics",
        bundle_name="customer360",
        target_name="dev",
        pipeline=pipeline,
    )

    assert dag_id == "ws_analytics_customer360_customer_medallion_pipeline"
    assert "from airflow import DAG" in code
    assert "task_extract_events" in code
    assert "task_clean_silver" in code
    assert "task_build_gold_marts" in code
    assert "task_extract_events >> task_clean_silver" in code
    assert "task_clean_silver >> task_build_gold_marts" in code

    # Verify abstract syntax tree parse succeeds without syntax errors
    parsed_ast = ast.parse(code)
    assert parsed_ast is not None


# ── REST API Integration Tests: Validate ─────────────────────────────────────


def test_validate_bundle_endpoint_valid_manifest(client_admin: TestClient, test_setup: dict):
    """Test POST /api/bundles/validate with a valid TOON manifest."""
    ws = test_setup["workspace"]
    resp = client_admin.post(
        f"/api/bundles/validate?workspace_slug={ws.slug}",
        json={
            "manifest_content": SAMPLE_CUSTOMER360_TOON,
            "manifest_format": "toon",
            "target": "dev",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["bundle_name"] == "customer360-medallion"
    assert data["target"] == "dev"
    assert len(data["errors"]) == 0
    assert "diff" in data
    assert data["diff"]["summary"]["create"] >= 3
    assert data["resolved_bundle"] is not None


def test_validate_bundle_endpoint_syntax_error(client_admin: TestClient, test_setup: dict):
    """Test POST /api/bundles/validate with malformed manifest content."""
    ws = test_setup["workspace"]
    resp = client_admin.post(
        f"/api/bundles/validate?workspace_slug={ws.slug}",
        json={
            "manifest_content": "bundle: {invalid_syntax [}",
            "manifest_format": "yaml",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert len(data["errors"]) > 0
    assert "Manifest parse error" in data["errors"][0]


def test_validate_bundle_endpoint_dag_cycle_detected(client_admin: TestClient, test_setup: dict):
    """Test POST /api/bundles/validate catching circular dependencies in pipeline tasks."""
    ws = test_setup["workspace"]
    cycle_manifest = """
bundle:
  name: cyclic-bundle
resources:
  pipelines:
    cyclic_pipe:
      tasks[2]{name,type,entrypoint,upstream_tasks}:
        task_a,duckdb_sql,query_a.sql,[task_b]
        task_b,duckdb_sql,query_b.sql,[task_a]
"""
    resp = client_admin.post(
        f"/api/bundles/validate?workspace_slug={ws.slug}",
        json={
            "manifest_content": cycle_manifest,
            "manifest_format": "toon",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert any("cycle" in err.lower() for err in data["errors"])


def test_validate_bundle_endpoint_sql_syntax_error(client_admin: TestClient, test_setup: dict):
    """Test POST /api/bundles/validate detecting DuckDB SQL syntax errors in widget queries."""
    ws = test_setup["workspace"]
    bad_sql_manifest = """
bundle:
  name: bad-sql-bundle
resources:
  dashboards:
    test_dash:
      widgets:
        bad_widget:
          type: metric
          query: "SELECT FROM WHERE"
"""
    resp = client_admin.post(
        f"/api/bundles/validate?workspace_slug={ws.slug}",
        json={
            "manifest_content": bad_sql_manifest,
            "manifest_format": "toon",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert any("SQL syntax error" in err for err in data["errors"])


# ── REST API Integration Tests: Deploy ───────────────────────────────────────


def test_deploy_bundle_endpoint_dry_run(
    client_admin: TestClient, test_setup: dict, db_session: Session
):
    """Test POST /api/bundles/deploy with dry_run=True performs diff calculation without mutating DB."""
    ws = test_setup["workspace"]
    resp = client_admin.post(
        f"/api/bundles/deploy?workspace_slug={ws.slug}",
        json={
            "manifest_content": SAMPLE_CUSTOMER360_TOON,
            "manifest_format": "toon",
            "target": "dev",
            "dry_run": True,
        },
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "active"
    assert "Dry run succeeded" in data["message"]

    # Verify no bundle persisted in database
    count = db_session.query(WorkspaceBundle).filter_by(workspace_id=ws.id).count()
    assert count == 0


def test_deploy_bundle_endpoint_full_execution(
    client_admin: TestClient,
    test_setup: dict,
    db_session: Session,
    mock_dags_dir: Path,
):
    """Test POST /api/bundles/deploy fully provisions all resources and records bundle state."""
    ws = test_setup["workspace"]

    with patch.object(storage_service, "upload_object") as mock_upload:
        mock_upload.return_value = {"key": "workspaces/data-platform/bundles/customer360/model.sql"}

        resp = client_admin.post(
            f"/api/bundles/deploy?workspace_slug={ws.slug}",
            json={
                "manifest_content": SAMPLE_CUSTOMER360_TOON,
                "manifest_format": "toon",
                "target": "dev",
                "files": {
                    "sql/extract.sql": "SELECT 1;",
                    "sql/silver_transform.sql": "SELECT 2;",
                },
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "active"
    assert data["bundle_name"] == "customer360-medallion"
    assert data["target"] == "dev"

    res_map = data["resources_map"]
    assert len(res_map["catalogs"]) == 1
    assert len(res_map["schemas"]) >= 3
    assert len(res_map["tables"]) == 2
    assert len(res_map["vector_indexes"]) == 1
    assert len(res_map["pipelines"]) == 1
    assert len(res_map["dashboards"]) == 1
    assert len(res_map["artifacts"]) == 2

    # Verify database state
    bundle = (
        db_session.query(WorkspaceBundle)
        .filter_by(
            workspace_id=ws.id,
            bundle_name="customer360-medallion",
            target="dev",
        )
        .first()
    )
    assert bundle is not None
    assert bundle.status == "active"
    assert bundle.deployed_by == test_setup["admin_user"].id

    # Verify catalog and schemas
    cat = db_session.query(Catalog).filter_by(id=uuid.UUID(res_map["catalogs"][0])).first()
    assert cat is not None
    assert cat.name == "main_lake"
    schema_names = [s.name.lower() for s in cat.schemas]
    assert "bronze" in schema_names
    assert "silver" in schema_names
    assert "gold" in schema_names

    # Verify tables
    tables = db_session.query(CatalogTable).all()
    assert len(tables) >= 2
    table_names = [t.name for t in tables]
    assert "raw_customers" in table_names
    assert "dim_customers" in table_names

    # Verify vector index
    dim_cust = db_session.query(CatalogTable).filter_by(name="dim_customers").first()
    assert dim_cust is not None
    assert dim_cust.vector_index is not None
    assert dim_cust.vector_index["index_name"] == "customer_notes_idx"
    assert dim_cust.vector_index["status"] == "ready"

    # Verify pipeline DAG file and PipelineMetadata
    ws_dag_folder = mock_dags_dir / "workspaces" / ws.slug
    assert ws_dag_folder.exists()
    dag_files = list(ws_dag_folder.glob("*.py"))
    assert len(dag_files) == 1
    dag_meta = (
        db_session.query(PipelineMetadata)
        .filter_by(
            workspace_id=ws.id,
            dag_id=res_map["pipelines"][0],
        )
        .first()
    )
    assert dag_meta is not None
    assert dag_meta.color_hex == "#10B981"

    # Verify dashboard
    dash = (
        db_session.query(WorkspaceDashboard)
        .filter_by(id=uuid.UUID(res_map["dashboards"][0]))
        .first()
    )
    assert dash is not None
    assert dash.title == "executive_overview"
    assert len(dash.layout_config) == 1


def test_deploy_bundle_idempotency_and_update(client_admin: TestClient, test_setup: dict):
    """Test deploying the same bundle twice idempotently updates resources."""
    ws = test_setup["workspace"]

    # 1. Initial deployment
    resp1 = client_admin.post(
        f"/api/bundles/deploy?workspace_slug={ws.slug}",
        json={
            "manifest_content": SAMPLE_CUSTOMER360_TOON,
            "manifest_format": "toon",
            "target": "dev",
        },
    )
    assert resp1.status_code == 201
    bundle_id = resp1.json()["id"]

    # 2. Deploy again with updated description
    updated_toon = SAMPLE_CUSTOMER360_TOON.replace(
        "Executive KPI Dashboard", "Updated Executive KPI Dashboard v2"
    )
    resp2 = client_admin.post(
        f"/api/bundles/deploy?workspace_slug={ws.slug}",
        json={
            "manifest_content": updated_toon,
            "manifest_format": "toon",
            "target": "dev",
        },
    )
    assert resp2.status_code == 201
    data2 = resp2.json()
    assert data2["id"] == bundle_id
    assert data2["status"] == "active"


# ── REST API Integration Tests: List & Get ───────────────────────────────────


def test_list_bundles_endpoint(client_admin: TestClient, test_setup: dict):
    """Test GET /api/bundles lists all deployed bundles with filtering."""
    ws = test_setup["workspace"]

    # Deploy customer360
    client_admin.post(
        f"/api/bundles/deploy?workspace_slug={ws.slug}",
        json={
            "manifest_content": SAMPLE_CUSTOMER360_TOON,
            "manifest_format": "toon",
            "target": "dev",
        },
    )

    resp = client_admin.get(f"/api/bundles?workspace_slug={ws.slug}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    item = data["bundles"][0]
    assert item["bundle_name"] == "customer360-medallion"
    assert item["target"] == "dev"
    assert item["status"] == "active"
    assert item["resources_count"] > 0
    assert "resources_map" in item
    assert item["catalogs_count"] == len(item["resources_map"].get("catalogs", []))
    assert item["tables_count"] == len(item["resources_map"].get("tables", []))
    assert item["pipelines_count"] == len(item["resources_map"].get("pipelines", []))
    assert item["dashboards_count"] == len(item["resources_map"].get("dashboards", []))

    # Test filtering by status
    resp_filtered = client_admin.get(f"/api/bundles?workspace_slug={ws.slug}&status=active")
    assert resp_filtered.status_code == 200
    assert resp_filtered.json()["total"] == 1

    resp_empty = client_admin.get(f"/api/bundles?workspace_slug={ws.slug}&status=destroyed")
    assert resp_empty.status_code == 200
    assert resp_empty.json()["total"] == 0


def test_get_bundle_detail_endpoint(client_admin: TestClient, test_setup: dict):
    """Test GET /api/bundles/{bundle_id} returns manifest snapshot, resource map, and drift."""
    ws = test_setup["workspace"]

    deploy_res = client_admin.post(
        f"/api/bundles/deploy?workspace_slug={ws.slug}",
        json={
            "manifest_content": SAMPLE_CUSTOMER360_TOON,
            "manifest_format": "toon",
            "target": "dev",
            "commit_hash": "a1b2c3d4",
        },
    )
    bundle_id = deploy_res.json()["id"]

    resp = client_admin.get(f"/api/bundles/{bundle_id}?workspace_slug={ws.slug}&include_drift=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == bundle_id
    assert data["bundle_name"] == "customer360-medallion"
    assert data["commit_hash"] == "a1b2c3d4"
    assert "manifest_raw" in data
    assert "resources_map" in data
    assert "drift" in data
    assert data["drift"]["in_sync"] is True


def test_get_bundle_detail_not_found(client_admin: TestClient, test_setup: dict):
    """Test GET /api/bundles/{bundle_id} returns 404 for unknown bundle UUID."""
    ws = test_setup["workspace"]
    random_id = uuid.uuid4()
    resp = client_admin.get(f"/api/bundles/{random_id}?workspace_slug={ws.slug}")
    assert resp.status_code == 404


# ── REST API Integration Tests: Run ──────────────────────────────────────────


def test_run_bundle_pipeline_endpoint(client_admin: TestClient, test_setup: dict):
    """Test POST /api/bundles/{bundle_id}/run triggers ad-hoc pipeline execution."""
    ws = test_setup["workspace"]

    deploy_res = client_admin.post(
        f"/api/bundles/deploy?workspace_slug={ws.slug}",
        json={
            "manifest_content": SAMPLE_CUSTOMER360_TOON,
            "manifest_format": "toon",
            "target": "dev",
        },
    )
    bundle_id = deploy_res.json()["id"]

    with patch.object(pipeline_service, "trigger_dag_run", new_callable=AsyncMock) as mock_trigger:
        mock_trigger.return_value = {
            "dag_id": "ws_data_platform_customer360_medallion_customer_medallion_pipeline",
            "dag_run_id": "manual__test_run",
            "state": "running",
            "execution_date": "2026-11-07T12:00:00Z",
        }

        resp = client_admin.post(
            f"/api/bundles/{bundle_id}/run?workspace_slug={ws.slug}",
            json={
                "pipeline_name": "customer_medallion_pipeline",
                "conf": {"batch_id": 123},
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["bundle_id"] == bundle_id
    assert "customer_medallion_pipeline" in data["dag_id"]
    assert data["status"] in ("running", "queued")


# ── REST API Integration Tests: Destroy ──────────────────────────────────────


def test_destroy_bundle_endpoint(
    client_admin: TestClient,
    test_setup: dict,
    db_session: Session,
    mock_dags_dir: Path,
):
    """Test DELETE /api/bundles/{bundle_id} safely purges all provisioned bundle resources."""
    ws = test_setup["workspace"]

    deploy_res = client_admin.post(
        f"/api/bundles/deploy?workspace_slug={ws.slug}",
        json={
            "manifest_content": SAMPLE_CUSTOMER360_TOON,
            "manifest_format": "toon",
            "target": "dev",
        },
    )
    bundle_id = deploy_res.json()["id"]

    # Teardown / destroy bundle
    del_resp = client_admin.delete(
        f"/api/bundles/{bundle_id}?workspace_slug={ws.slug}&purge_storage=true"
    )
    assert del_resp.status_code == 200
    del_data = del_resp.json()
    assert del_data["status"] == "destroyed"
    assert len(del_data["purged_resources"]["pipelines"]) == 1
    assert len(del_data["purged_resources"]["dashboards"]) == 1

    # Verify bundle record status updated to destroyed
    bundle = db_session.query(WorkspaceBundle).filter_by(id=uuid.UUID(bundle_id)).first()
    assert bundle is not None
    assert bundle.status == "destroyed"

    # Verify DAG file was deleted from disk
    ws_dag_folder = mock_dags_dir / "workspaces" / ws.slug
    assert len(list(ws_dag_folder.glob("*.py"))) == 0

    # Verify dashboard was removed
    dashboards = db_session.query(WorkspaceDashboard).filter_by(workspace_id=ws.id).all()
    assert len(dashboards) == 0


# ── RBAC & Isolation Tests ───────────────────────────────────────────────────


def test_viewer_cannot_deploy_or_destroy(client_viewer: TestClient, test_setup: dict):
    """Verify that read-only viewers receive 403 Forbidden when deploying or deleting bundles."""
    ws = test_setup["workspace"]

    # Attempt deploy
    resp_deploy = client_viewer.post(
        f"/api/bundles/deploy?workspace_slug={ws.slug}",
        json={
            "manifest_content": SAMPLE_CUSTOMER360_TOON,
            "manifest_format": "toon",
        },
    )
    assert resp_deploy.status_code == 403
    assert "read-only" in resp_deploy.json()["detail"].lower()

    # Attempt destroy
    fake_id = uuid.uuid4()
    resp_destroy = client_viewer.delete(f"/api/bundles/{fake_id}?workspace_slug={ws.slug}")
    assert resp_destroy.status_code == 403


def test_cross_workspace_isolation(client_admin: TestClient, test_setup: dict):
    """Verify that a bundle belonging to Workspace A is inaccessible from Workspace B."""
    ws_a = test_setup["workspace"]
    ws_b = test_setup["other_workspace"]

    # Deploy in Workspace A
    deploy_res = client_admin.post(
        f"/api/bundles/deploy?workspace_slug={ws_a.slug}",
        json={
            "manifest_content": SAMPLE_CUSTOMER360_TOON,
            "manifest_format": "toon",
            "target": "dev",
        },
    )
    bundle_id = deploy_res.json()["id"]

    # Attempt to access bundle from Workspace B
    resp_b = client_admin.get(f"/api/bundles/{bundle_id}?workspace_slug={ws_b.slug}")
    assert resp_b.status_code in (403, 404)
