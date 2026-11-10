"""Comprehensive Unit & Integration Tests for Epic #453 (Phases 1-4: #454, #455, #456, #457).

Covers:
- Phase 1 (#454): Zero-Login `LakesetSecurityManager`, proxy header HMAC signing/verification,
  role mapping (`superadmin` -> `Admin`, workspace members -> `Alpha`/`Gamma`), and Helm templates
  (`superset.yaml`, `superset-config.yaml`) with `duckdb-engine`.
- Phase 2 (#455): Automated DuckDB + MinIO S3 database registration, Catalog-to-Lakeset semantic
  dataset sync (`POST /api/v1/lakeset/sync`), column descriptions & metrics generation, and
  RBAC-scoped dataset listing & Guest Token minting (`GET /api/v1/lakeset/datasets`,
  `POST /api/v1/lakeset/guest-token`).
- Phase 3 (#456): Native `/lakeset` UI section, `lakesetManager` Alpine.js controller, mode tabs,
  and `/lakeset/proxy/{path}` zero-login reverse auth proxy.
- Phase 4 (#457): Catalog Explorer & SQL Editor & Genie "Open in Lakeset" deep-linking,
  `BundleDeployer` auto-sync hook, and `docs/wiki/Lakeset-BI-Studio.md`.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


repo_root = Path(__file__).resolve().parents[1]
backend_dir = str(repo_root / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
import lakeset_security  # noqa: E402
import superset_service  # noqa: E402
from database import Base  # noqa: E402
from models import (  # noqa: E402
    Catalog,
    CatalogAccess,
    CatalogSchema,
    CatalogTable,
    User,
    Workspace,
    WorkspaceMember,
)

import backend.main as backend_main  # noqa: E402
import ui.main as ui_main  # noqa: E402

# ── Phase 1 (#454): Zero-Login Security Manager & Helm Configuration ─────────


def test_phase1_helm_superset_duckdb_engine_and_config():
    superset_yaml = (
        repo_root / "deploy" / "helm" / "lake-of-tears" / "templates" / "superset.yaml"
    ).read_text(encoding="utf-8")
    superset_cfg = (
        repo_root / "deploy" / "helm" / "lake-of-tears" / "templates" / "superset-config.yaml"
    ).read_text(encoding="utf-8")

    # Verify duckdb and duckdb-engine installation in Superset runtime
    assert "duckdb" in superset_yaml
    assert "duckdb-engine" in superset_yaml
    assert "LAKESET_PROXY_SECRET" in superset_yaml
    assert "MINIO_ENDPOINT" in superset_yaml

    # Verify zero-login LakesetSecurityManager and embedded flags in superset_config.py
    assert "class LakesetSecurityManager(SupersetSecurityManager):" in superset_cfg
    assert "CUSTOM_SECURITY_MANAGER = LakesetSecurityManager" in superset_cfg
    assert "EMBEDDED_SUPERSET = True" in superset_cfg
    assert "ENABLE_TEMPLATE_PROCESSING = True" in superset_cfg
    assert "TALISMAN_ENABLED = False" in superset_cfg
    assert "GUEST_TOKEN_JWT_SECRET" in superset_cfg
    assert "X-Lakeset-User-Email" in superset_cfg
    assert "X-Lakeset-User-Role" in superset_cfg


def test_phase1_role_mapping_and_signed_proxy_headers():
    assert lakeset_security.map_lot_role_to_superset("superadmin") == "Admin"
    assert lakeset_security.map_lot_role_to_superset("owner") == "Alpha"
    assert lakeset_security.map_lot_role_to_superset("admin") == "Alpha"
    assert lakeset_security.map_lot_role_to_superset("editor") == "Alpha"
    assert lakeset_security.map_lot_role_to_superset("viewer") == "Gamma"

    headers = lakeset_security.build_proxy_headers(
        email="data.scientist@lake.local",
        display_name="Ada Lovelace",
        role="superadmin",
        workspace="biomedical",
        secret="test-proxy-secret",
    )
    assert headers["X-Lakeset-User-Email"] == "data.scientist@lake.local"
    assert headers["X-Lakeset-User-Name"] == "Ada Lovelace"
    assert headers["X-Lakeset-User-Role"] == "superadmin"
    assert headers["X-Lakeset-Workspace"] == "biomedical"
    assert headers["X-Lakeset-Signature"]

    identity = lakeset_security.verify_proxy_headers(headers, expected_secret="test-proxy-secret")
    assert identity is not None
    assert identity["email"] == "data.scientist@lake.local"
    assert identity["first_name"] == "Ada"
    assert identity["last_name"] == "Lovelace"
    assert identity["superset_role"] == "Admin"
    assert identity["workspace"] == "biomedical"

    # Tampered secret or signature must fail verification
    assert lakeset_security.verify_proxy_headers(headers, expected_secret="wrong-secret") is None


def test_phase1_security_manager_user_auto_provisioning():
    class DummyRole:
        def __init__(self, name: str):
            self.name = name

    class DummyUser:
        def __init__(self, username: str, first_name: str, last_name: str, email: str, roles: list):
            self.username = username
            self.first_name = first_name
            self.last_name = last_name
            self.email = email
            self.roles = roles
            self.active = True

    class MockLakesetSecurityManager(lakeset_security.LakesetSecurityManagerBase):
        def __init__(self):
            self.users: dict[str, DummyUser] = {}
            self.roles = {
                "Admin": DummyRole("Admin"),
                "Alpha": DummyRole("Alpha"),
                "Gamma": DummyRole("Gamma"),
            }
            self.updated_count = 0

        def find_role(self, name: str):
            return self.roles.get(name)

        def find_user(self, username: str | None = None, email: str | None = None):
            key = username or email
            return self.users.get(str(key))

        def add_user(self, username: str, first_name: str, last_name: str, email: str, role: list):
            u = DummyUser(username, first_name, last_name, email, role)
            self.users[username] = u
            return u

        def update_user(self, user: DummyUser):
            self.updated_count += 1

    sm = MockLakesetSecurityManager()
    headers = lakeset_security.build_proxy_headers(
        email="analyst@lake.local",
        display_name="Grace Hopper",
        role="editor",
        workspace="finance",
    )
    user = sm.authenticate_request(headers)
    assert user is not None
    assert user.email == "analyst@lake.local"
    assert user.first_name == "Grace"
    assert user.last_name == "Hopper"
    assert user.roles[0].name == "Alpha"

    # Promoting user to superadmin updates their Superset role to Admin
    admin_headers = lakeset_security.build_proxy_headers(
        email="analyst@lake.local",
        display_name="Grace Hopper",
        role="superadmin",
        workspace="finance",
    )
    updated_user = sm.authenticate_request(admin_headers)
    assert updated_user.roles[0].name == "Admin"
    assert sm.updated_count == 1


# ── Phase 2 (#455): DuckDB Provisioning, Semantic Dataset Sync & RBAC ────────


@pytest.fixture()
def lakeset_db_session(tmp_path):
    db_file = tmp_path / "test_lakeset.sqlite"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_workspaces_and_catalogs(db):
    user_alpha = User(
        id=uuid.uuid4(),
        email="alpha@lake.local",
        display_name="Alpha Engineer",
        role="user",
        is_active=True,
    )
    user_beta = User(
        id=uuid.uuid4(),
        email="beta@lake.local",
        display_name="Beta Viewer",
        role="user",
        is_active=True,
    )
    ws_alpha = Workspace(
        id=uuid.uuid4(),
        name="Alpha Analytics",
        slug="alpha-ws",
        status="active",
    )
    ws_beta = Workspace(
        id=uuid.uuid4(),
        name="Beta Secret Lab",
        slug="beta-ws",
        status="active",
    )
    db.add_all([user_alpha, user_beta, ws_alpha, ws_beta])
    db.flush()

    db.add_all(
        [
            WorkspaceMember(
                id=uuid.uuid4(), workspace_id=ws_alpha.id, user_id=user_alpha.id, role="owner"
            ),
            WorkspaceMember(
                id=uuid.uuid4(), workspace_id=ws_beta.id, user_id=user_beta.id, role="owner"
            ),
        ]
    )

    cat_alpha = Catalog(
        id=uuid.uuid4(),
        name="commerce",
        slug="commerce",
        description="E-Commerce Medallion Catalog",
        owner_workspace_id=ws_alpha.id,
        created_by=user_alpha.id,
    )
    cat_beta = Catalog(
        id=uuid.uuid4(),
        name="classified",
        slug="classified",
        description="Restricted Beta Catalog",
        owner_workspace_id=ws_beta.id,
        created_by=user_beta.id,
    )
    db.add_all([cat_alpha, cat_beta])
    db.flush()

    sch_gold = CatalogSchema(
        id=uuid.uuid4(),
        catalog_id=cat_alpha.id,
        name="gold",
        slug="gold",
        tier="gold",
        description="Curated business metrics",
    )
    sch_beta = CatalogSchema(
        id=uuid.uuid4(),
        catalog_id=cat_beta.id,
        name="silver",
        slug="silver",
        tier="silver",
    )
    db.add_all([sch_gold, sch_beta])
    db.flush()

    tbl_orders = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch_gold.id,
        name="revenue_orders",
        slug="revenue_orders",
        description="Daily aggregated customer revenue orders",
        s3_path_pattern="s3://datalake/workspaces/alpha-ws/gold/revenue_orders/*.parquet",
        column_defs=[
            {
                "name": "order_date",
                "type": "TIMESTAMP",
                "nullable": False,
                "description": "UTC timestamp when the order was completed",
            },
            {
                "name": "channel",
                "type": "VARCHAR",
                "nullable": False,
                "description": "Acquisition marketing channel (Stripe, Shopify, Direct)",
            },
            {
                "name": "net_revenue",
                "type": "DOUBLE",
                "nullable": False,
                "description": "Net order revenue in USD after discounts",
            },
        ],
    )
    tbl_secret = CatalogTable(
        id=uuid.uuid4(),
        schema_id=sch_beta.id,
        name="payroll_records",
        slug="payroll_records",
        description="Confidential payroll table",
        s3_path_pattern="s3://datalake/workspaces/beta-ws/silver/payroll_records/*.parquet",
        column_defs=[
            {"name": "employee_id", "type": "VARCHAR", "description": "Internal ID"},
            {"name": "salary", "type": "DOUBLE", "description": "Annual base salary"},
        ],
    )
    db.add_all([tbl_orders, tbl_secret])
    db.commit()

    return {
        "user_alpha": user_alpha,
        "user_beta": user_beta,
        "ws_alpha": ws_alpha,
        "ws_beta": ws_beta,
        "cat_alpha": cat_alpha,
        "cat_beta": cat_beta,
        "tbl_orders": tbl_orders,
        "tbl_secret": tbl_secret,
    }


def test_phase2_sync_registers_duckdb_and_pushes_semantic_columns(lakeset_db_session):
    seed = _seed_workspaces_and_catalogs(lakeset_db_session)
    user_alpha = seed["user_alpha"]
    ws_alpha = seed["ws_alpha"]

    backend_main.app.dependency_overrides[database.get_db] = lambda: lakeset_db_session
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_alpha
    client = TestClient(backend_main.app)

    try:
        # 1. Trigger POST /api/v1/lakeset/sync
        sync_resp = client.post(
            "/api/v1/lakeset/sync",
            headers={"X-Workspace-Slug": ws_alpha.slug},
        )
        assert sync_resp.status_code == 200
        sync_data = sync_resp.json()
        assert sync_data["ok"] is True
        assert sync_data["status"] == "synced"
        assert sync_data["database"]["database_name"] == "Lake of Tears DuckDB Lakehouse"
        assert "duckdb:///" in sync_data["database"]["sqlalchemy_uri"]
        assert sync_data["synced_datasets_count"] == 1
        assert "gold.revenue_orders" in sync_data["views_materialized"]

        ds = sync_data["datasets"][0]
        assert ds["full_name"] == "commerce.gold.revenue_orders"
        assert ds["synced"] is True
        assert ds["column_count"] == 3

        col_map = {c["column_name"]: c for c in ds["columns"]}
        assert col_map["net_revenue"]["description"] == "Net order revenue in USD after discounts"
        assert col_map["order_date"]["is_dttm"] is True

        metric_names = {m["metric_name"] for m in ds["metrics"]}
        assert "count" in metric_names
        assert "sum__net_revenue" in metric_names
        assert "avg__net_revenue" in metric_names
    finally:
        backend_main.app.dependency_overrides.clear()


def test_phase2_rbac_dataset_isolation_and_catalog_access_shares(lakeset_db_session):
    seed = _seed_workspaces_and_catalogs(lakeset_db_session)
    user_alpha = seed["user_alpha"]
    ws_alpha = seed["ws_alpha"]
    cat_beta = seed["cat_beta"]

    backend_main.app.dependency_overrides[database.get_db] = lambda: lakeset_db_session
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user_alpha
    client = TestClient(backend_main.app)

    try:
        # Alpha user only sees commerce.gold.revenue_orders, NOT classified.silver.payroll_records
        resp = client.get(
            "/api/v1/lakeset/datasets",
            headers={"X-Workspace-Slug": ws_alpha.slug},
        )
        assert resp.status_code == 200
        datasets = resp.json()["datasets"]
        full_names = [d["full_name"] for d in datasets]
        assert "commerce.gold.revenue_orders" in full_names
        assert "classified.silver.payroll_records" not in full_names

        # Alpha user requesting Guest Token for unauthorized Beta dataset gets 403 Forbidden
        forbidden_gt = client.post(
            "/api/v1/lakeset/guest-token",
            headers={"X-Workspace-Slug": ws_alpha.slug},
            json={"dataset": "classified.silver.payroll_records"},
        )
        assert forbidden_gt.status_code == 403

        # Alpha user requesting Guest Token for authorized dataset succeeds
        valid_gt = client.post(
            "/api/v1/lakeset/guest-token",
            headers={"X-Workspace-Slug": ws_alpha.slug},
            json={"dataset": "commerce.gold.revenue_orders"},
        )
        assert valid_gt.status_code == 200
        gt_data = valid_gt.json()
        assert gt_data["ok"] is True
        assert gt_data["token"]
        assert gt_data["user"]["email"] == "alpha@lake.local"
        assert gt_data["resources"][0]["full_name"] == "commerce.gold.revenue_orders"

        # Now grant an approved CatalogAccess share of cat_beta to ws_alpha
        share = CatalogAccess(
            id=uuid.uuid4(),
            catalog_id=cat_beta.id,
            workspace_id=ws_alpha.id,
            status="approved",
        )
        lakeset_db_session.add(share)
        lakeset_db_session.commit()

        shared_resp = client.get(
            "/api/v1/lakeset/datasets",
            headers={"X-Workspace-Slug": ws_alpha.slug},
        )
        assert shared_resp.status_code == 200
        shared_names = {d["full_name"]: d for d in shared_resp.json()["datasets"]}
        assert "classified.silver.payroll_records" in shared_names
        assert shared_names["classified.silver.payroll_records"]["is_shared"] is True
    finally:
        backend_main.app.dependency_overrides.clear()


# ── Phase 3 (#456): Native /lakeset UI Section & Zero-Login Reverse Proxy ────


def test_phase3_ui_lakeset_route_and_reverse_auth_proxy():
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "email": "architect@lake.local",
            "display_name": "Data Architect",
            "role": "superadmin",
        },
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    mock_workspaces = [
        {"id": "ws-1", "name": "Enterprise Lake", "slug": "enterprise-lake", "my_role": "owner"}
    ]
    ui_client = TestClient(ui_main.app)

    # 1. Unauthenticated request to /lakeset redirects to /login
    unauth_resp = ui_client.get("/lakeset", follow_redirects=False)
    assert unauth_resp.status_code == 302
    assert "/login" in unauth_resp.headers["location"]

    # 2. Authenticated request renders /lakeset Studio with mode tabs and zero login prompt
    ui_client.cookies.set("lake_token", token)
    ui_client.cookies.set(ui_main._WORKSPACE_COOKIE, "ws-1")

    with patch.object(ui_main, "_fetch_workspaces", return_value=mock_workspaces):
        resp = ui_client.get("/lakeset")
        assert resp.status_code == 200
        html = resp.text
        assert "Lakeset BI Studio" in html
        assert 'x-data="lakesetManager()"' in html
        assert "/static/js/controllers/lakeset.js" in html
        assert "Sync Catalogs to Lakeset" in html
        assert "Datasets &amp; Semantic Layer" in html
        assert "Chart Builder (Explore)" in html
        assert "SQL Lab" in html

        # 3. Verify /lakeset/proxy/{path} injects zero-login headers and returns 200 OK
        proxy_resp = ui_client.get("/lakeset/proxy/superset/welcome/")
        assert proxy_resp.status_code == 200
        assert proxy_resp.headers.get("X-Lakeset-Zero-Login") == "active"
        assert "architect@lake.local" in proxy_resp.text


# ── Phase 4 (#457): Open in Lakeset Deep-Links & BundleDeployer Auto-Sync ────


def test_phase4_catalog_sql_genie_deep_links_and_docs():
    catalog_html = (repo_root / "ui" / "templates" / "catalog.html").read_text(encoding="utf-8")
    catalog_js = (repo_root / "ui" / "static" / "js" / "controllers" / "catalog.js").read_text(
        encoding="utf-8"
    )
    sql_html = (repo_root / "ui" / "templates" / "sql.html").read_text(encoding="utf-8")
    sql_js = (repo_root / "ui" / "static" / "js" / "controllers" / "sql.js").read_text(
        encoding="utf-8"
    )
    ai_html = (repo_root / "ui" / "templates" / "ai.html").read_text(encoding="utf-8")
    wiki_doc = repo_root / "docs" / "wiki" / "Lakeset-BI-Studio.md"

    # Catalog Explorer 1-click Open in Lakeset
    assert 'id="panel-lakeset-btn"' in catalog_html
    assert "Open in Lakeset" in catalog_html
    assert "/lakeset?mode=explore&dataset=" in catalog_js

    # SQL Editor 1-click Open in Lakeset
    assert 'id="btnOpenInLakeset"' in sql_html
    assert "openQueryInLakeset()" in sql_html
    assert "openQueryInLakeset" in sql_js
    assert "/lakeset?mode=explore&sql=" in sql_js

    # Genie AI 1-click Open in Lakeset
    assert "/lakeset?mode=explore&sql=" in ai_html
    assert "Open in Lakeset" in ai_html

    # Documentation
    assert wiki_doc.exists()
    doc_text = wiki_doc.read_text(encoding="utf-8")
    assert "LakesetSecurityManager" in doc_text
    assert "/api/v1/lakeset/sync" in doc_text


def test_phase4_bundle_deployer_triggers_lakeset_sync(lakeset_db_session, tmp_path):
    from backend.bundles.deployer import BundleDeployer

    seed = _seed_workspaces_and_catalogs(lakeset_db_session)
    ws_alpha = seed["ws_alpha"]
    user_alpha = seed["user_alpha"]

    manifest_yaml = """
bundle:
  name: lakeset-sales-bundle
  version: 1.0.0
  description: Sales bundle with automatic Lakeset dataset sync
resources:
  catalogs:
    - name: commerce
      description: Commerce catalog
  schemas:
    - name: gold
      catalog: commerce
      tier: gold
  tables:
    - name: monthly_kpis
      catalog: commerce
      schema: gold
      description: Monthly KPIs synced to Lakeset
      s3_path_pattern: s3://datalake/workspaces/alpha-ws/gold/monthly_kpis/*.parquet
      columns:
        - name: month
          type: VARCHAR
          description: Reporting month YYYY-MM
        - name: mrr
          type: DOUBLE
          description: Monthly recurring revenue in USD
"""

    with (
        patch("pipeline_service.get_airflow_dags_dir", return_value=tmp_path),
        patch(
            "superset_service.sync_workspace_to_lakeset",
            wraps=superset_service.sync_workspace_to_lakeset,
        ) as spy_sync,
    ):
        deploy_res = BundleDeployer.deploy(
            db=lakeset_db_session,
            workspace=ws_alpha,
            user=user_alpha,
            manifest_content=manifest_yaml,
            manifest_format="yaml",
        )
        assert deploy_res.status == "active"
        assert spy_sync.called

    # Verify the newly deployed bundle table `commerce.gold.monthly_kpis` is synced in Lakeset
    datasets = superset_service.get_authorized_workspace_tables(
        db=lakeset_db_session, workspace=ws_alpha, user=user_alpha
    )
    by_name = {d["full_name"]: d for d in datasets}
    assert "commerce.gold.monthly_kpis" in by_name
    assert by_name["commerce.gold.monthly_kpis"]["synced"] is True
