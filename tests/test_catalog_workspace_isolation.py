from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import jwt
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON

backend_dir = str(Path(__file__).parents[1] / "backend")
ui_dir = str(Path(__file__).parents[1] / "ui")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)

import auth  # noqa: E402
import database  # noqa: E402
from models import (  # noqa: E402
    Base,
    Catalog,
    CatalogAccess,
    CatalogSchema,
    CatalogTable,
    User,
    Workspace,
    WorkspaceMember,
)

spec = importlib.util.spec_from_file_location(
    "backend_main_cat_isolation", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_cat_isolation"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


def _setup_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    # Workspaces
    ws_a = Workspace(id=uuid.uuid4(), name="Workspace Alpha", slug="ws-alpha", status="active")
    ws_b = Workspace(id=uuid.uuid4(), name="Workspace Beta", slug="ws-beta", status="active")
    session.add_all([ws_a, ws_b])
    session.commit()

    # Users
    alice_id = uuid.uuid4()
    alice = User(
        id=alice_id,
        email="alice@lake.local",
        display_name="Alice Alpha",
        role="user",
        is_active=True,
    )
    alice_token = jwt.encode(
        {"sub": str(alice_id), "email": alice.email, "role": alice.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )

    bob_id = uuid.uuid4()
    bob = User(
        id=bob_id, email="bob@lake.local", display_name="Bob Beta", role="user", is_active=True
    )
    bob_token = jwt.encode(
        {"sub": str(bob_id), "email": bob.email, "role": bob.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )

    charlie_id = uuid.uuid4()
    charlie = User(
        id=charlie_id,
        email="charlie@lake.local",
        display_name="Charlie Multi",
        role="user",
        is_active=True,
    )
    charlie_token = jwt.encode(
        {"sub": str(charlie_id), "email": charlie.email, "role": charlie.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )

    sam_id = uuid.uuid4()
    sam = User(
        id=sam_id,
        email="sam@lake.local",
        display_name="Sam Superadmin",
        role="superadmin",
        is_active=True,
    )
    sam_token = jwt.encode(
        {"sub": str(sam_id), "email": sam.email, "role": sam.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )

    session.add_all([alice, bob, charlie, sam])
    session.commit()

    # Memberships
    m_alice_a = WorkspaceMember(workspace_id=ws_a.id, user_id=alice.id, role="admin")
    m_bob_b = WorkspaceMember(workspace_id=ws_b.id, user_id=bob.id, role="admin")
    m_charlie_a = WorkspaceMember(workspace_id=ws_a.id, user_id=charlie.id, role="admin")
    m_charlie_b = WorkspaceMember(workspace_id=ws_b.id, user_id=charlie.id, role="admin")

    session.add_all([m_alice_a, m_bob_b, m_charlie_a, m_charlie_b])
    session.commit()

    # Catalogs
    cat_a_private = Catalog(
        id=uuid.uuid4(),
        name="Alpha Confidential",
        slug="alpha-confidential",
        owner_workspace_id=ws_a.id,
        created_at=datetime.now(UTC),
    )
    cat_a_shared = Catalog(
        id=uuid.uuid4(),
        name="Alpha Shared Data",
        slug="alpha-shared-data",
        owner_workspace_id=ws_a.id,
        created_at=datetime.now(UTC),
    )
    cat_b_owned = Catalog(
        id=uuid.uuid4(),
        name="Beta Operations",
        slug="beta-operations",
        owner_workspace_id=ws_b.id,
        created_at=datetime.now(UTC),
    )
    session.add_all([cat_a_private, cat_a_shared, cat_b_owned])
    session.commit()

    # Schema & Table on cat_a_private
    schema_priv = CatalogSchema(
        id=uuid.uuid4(), catalog_id=cat_a_private.id, name="Secret", slug="secret", tier="custom"
    )
    session.add(schema_priv)
    session.commit()

    table_priv = CatalogTable(
        id=uuid.uuid4(),
        schema_id=schema_priv.id,
        name="Payroll",
        slug="payroll",
        s3_path_pattern="s3://datalake/alpha/secret/payroll.parquet",
    )
    session.add(table_priv)
    session.commit()

    # Share cat_a_shared to ws_b (read-only)
    grant = CatalogAccess(
        id=uuid.uuid4(),
        catalog_id=cat_a_shared.id,
        workspace_id=ws_b.id,
        mode="read",
        status="approved",
        suspended=False,
    )
    session.add(grant)
    session.commit()

    # Refresh objects to ensure relationships are loaded
    session.refresh(alice)
    session.refresh(bob)
    session.refresh(charlie)
    session.refresh(sam)

    return {
        "session": session,
        "ws_a": ws_a,
        "ws_b": ws_b,
        "alice": alice,
        "alice_token": alice_token,
        "bob": bob,
        "bob_token": bob_token,
        "charlie": charlie,
        "charlie_token": charlie_token,
        "sam": sam,
        "sam_token": sam_token,
        "cat_a_private": cat_a_private,
        "cat_a_shared": cat_a_shared,
        "cat_b_owned": cat_b_owned,
        "schema_priv": schema_priv,
        "table_priv": table_priv,
    }


def test_catalog_listing_isolation_and_context_enforcement():
    fix = _setup_fixture()
    session = fix["session"]

    backend_main.app.dependency_overrides[database.get_db] = lambda: session
    try:
        # 1. No workspace context returns 400
        resp = client.get(
            "/api/catalogs",
            headers={"Authorization": f"Bearer {fix['alice_token']}"},
        )
        assert resp.status_code == 400
        assert "Active workspace context" in resp.json()["detail"]

        # 2. Alice in ws_a sees only ws_a catalogs
        resp = client.get(
            "/api/catalogs",
            headers={
                "Authorization": f"Bearer {fix['alice_token']}",
                "X-Workspace-Id": str(fix["ws_a"].id),
            },
        )
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()]
        assert "Alpha Confidential" in names
        assert "Alpha Shared Data" in names
        assert "Beta Operations" not in names

        # 3. Alice attempting to access ws_b gets 403 Forbidden
        resp = client.get(
            "/api/catalogs",
            headers={
                "Authorization": f"Bearer {fix['alice_token']}",
                "X-Workspace-Id": str(fix["ws_b"].id),
            },
        )
        assert resp.status_code == 403

        # 4. Bob in ws_b sees Beta Operations (owned) and Alpha Shared Data (shared)
        resp = client.get(
            "/api/catalogs",
            headers={
                "Authorization": f"Bearer {fix['bob_token']}",
                "X-Workspace-Id": str(fix["ws_b"].id),
            },
        )
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()]
        assert "Beta Operations" in names
        assert "Alpha Shared Data" in names
        assert "Alpha Confidential" not in names

        # 5. Superadmin Sam is scope-jailed to the active workspace
        resp = client.get(
            "/api/catalogs",
            headers={
                "Authorization": f"Bearer {fix['sam_token']}",
                "X-Workspace-Id": str(fix["ws_a"].id),
            },
        )
        assert resp.status_code == 200
        sam_ws_a_names = [c["name"] for c in resp.json()]
        assert "Beta Operations" not in sam_ws_a_names
        assert "Alpha Confidential" in sam_ws_a_names
    finally:
        backend_main.app.dependency_overrides.clear()


def test_workspaces_catalogs_path_route_authorization():
    fix = _setup_fixture()
    session = fix["session"]

    backend_main.app.dependency_overrides[database.get_db] = lambda: session
    try:
        # Alice can list ws_a
        resp = client.get(
            f"/api/workspaces/{fix['ws_a'].id}/catalogs",
            headers={"Authorization": f"Bearer {fix['alice_token']}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

        # Alice cannot list ws_b (403 Forbidden)
        resp = client.get(
            f"/api/workspaces/{fix['ws_b'].id}/catalogs",
            headers={"Authorization": f"Bearer {fix['alice_token']}"},
        )
        assert resp.status_code == 403
        assert "Not a member" in resp.json()["detail"]

        # Non-existent workspace returns 404
        fake_ws_id = uuid.uuid4()
        resp = client.get(
            f"/api/workspaces/{fake_ws_id}/catalogs",
            headers={"Authorization": f"Bearer {fix['sam_token']}"},
        )
        assert resp.status_code == 404
        assert "Workspace not found" in resp.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()


def test_single_catalog_strict_invisibility_and_superadmin_jailing():
    fix = _setup_fixture()
    session = fix["session"]

    backend_main.app.dependency_overrides[database.get_db] = lambda: session
    try:
        # Charlie belongs to both Workspace Alpha and Beta.
        # When Charlie acts in Workspace Beta, Alpha Confidential is NOT shared to Beta.
        # GET /api/catalogs/{id} must return 404 Not Found (invisible, not 403).
        resp = client.get(
            f"/api/catalogs/{fix['cat_a_private'].id}",
            headers={
                "Authorization": f"Bearer {fix['charlie_token']}",
                "X-Workspace-Id": str(fix["ws_b"].id),
            },
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Catalog not found"

        # Schemas & tables on private catalog are also invisible from Workspace Beta
        resp_schemas = client.get(
            f"/api/catalogs/{fix['cat_a_private'].id}/schemas",
            headers={
                "Authorization": f"Bearer {fix['charlie_token']}",
                "X-Workspace-Id": str(fix["ws_b"].id),
            },
        )
        assert resp_schemas.status_code == 404

        resp_tables = client.get(
            f"/api/catalogs/{fix['cat_a_private'].id}/schemas/{fix['schema_priv'].id}/tables",
            headers={
                "Authorization": f"Bearer {fix['charlie_token']}",
                "X-Workspace-Id": str(fix["ws_b"].id),
            },
        )
        assert resp_tables.status_code == 404

        # Superadmin Sam active in Workspace Beta is also scope-jailed and cannot access Alpha Confidential
        resp_sam = client.get(
            f"/api/catalogs/{fix['cat_a_private'].id}",
            headers={
                "Authorization": f"Bearer {fix['sam_token']}",
                "X-Workspace-Id": str(fix["ws_b"].id),
            },
        )
        assert resp_sam.status_code == 404
        assert resp_sam.json()["detail"] == "Catalog not found"

        # When Sam switches to Workspace Alpha, access succeeds
        resp_sam_alpha = client.get(
            f"/api/catalogs/{fix['cat_a_private'].id}",
            headers={
                "Authorization": f"Bearer {fix['sam_token']}",
                "X-Workspace-Id": str(fix["ws_a"].id),
            },
        )
        assert resp_sam_alpha.status_code == 200
        assert resp_sam_alpha.json()["name"] == "Alpha Confidential"
    finally:
        backend_main.app.dependency_overrides.clear()


def test_shared_catalog_read_only_access_and_mutation_rejection():
    fix = _setup_fixture()
    session = fix["session"]

    backend_main.app.dependency_overrides[database.get_db] = lambda: session
    try:
        # Bob in Workspace Beta accesses Alpha Shared Data: read succeeds, my_access is 'read'
        resp = client.get(
            f"/api/catalogs/{fix['cat_a_shared'].id}",
            headers={
                "Authorization": f"Bearer {fix['bob_token']}",
                "X-Workspace-Id": str(fix["ws_b"].id),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Alpha Shared Data"
        assert data["my_access"] == "read"

        # Bob attempts to PATCH the shared catalog -> 403 Forbidden
        resp_patch = client.patch(
            f"/api/catalogs/{fix['cat_a_shared'].id}",
            json={"name": "Hacked Name"},
            headers={
                "Authorization": f"Bearer {fix['bob_token']}",
                "X-Workspace-Id": str(fix["ws_b"].id),
            },
        )
        assert resp_patch.status_code == 403
        assert "owner access required" in resp_patch.json()["detail"]

        # Bob attempts to create a schema in shared catalog -> 403 Forbidden
        resp_create_schema = client.post(
            f"/api/catalogs/{fix['cat_a_shared'].id}/schemas",
            json={"name": "Illegal Schema"},
            headers={
                "Authorization": f"Bearer {fix['bob_token']}",
                "X-Workspace-Id": str(fix["ws_b"].id),
            },
        )
        assert resp_create_schema.status_code == 403
        assert "Write access required" in resp_create_schema.json()["detail"]

        # Bob attempts to DELETE the shared catalog -> 403 Forbidden
        resp_delete = client.delete(
            f"/api/catalogs/{fix['cat_a_shared'].id}",
            headers={
                "Authorization": f"Bearer {fix['bob_token']}",
                "X-Workspace-Id": str(fix["ws_b"].id),
            },
        )
        assert resp_delete.status_code == 403
        assert "owner access required" in resp_delete.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()


def test_ui_resolve_catalogs_removes_global_fallback():
    import main as ui_main

    # 1. When request has no workspace, _resolve_ui_catalogs returns empty list without making backend calls
    mock_req_no_ws = MagicMock()
    mock_req_no_ws.cookies.get.return_value = "fake-token"
    mock_req_no_ws.state.workspace = None

    catalogs = ui_main._resolve_ui_catalogs(mock_req_no_ws)
    assert catalogs == []

    # 2. When request has workspace, _resolve_ui_catalogs calls workspace-scoped endpoint
    mock_req_ws = MagicMock()
    mock_req_ws.cookies.get.return_value = "fake-token"
    mock_req_ws.state.workspace = {"id": "ws-123", "slug": "analytics"}

    mock_backend_data = [{"id": "cat-1", "name": "Marketing", "schemas": []}]
    mock_get = MagicMock(return_value=mock_backend_data)
    original_get = ui_main._backend_get
    ui_main._backend_get = mock_get
    try:
        result = ui_main._resolve_ui_catalogs(mock_req_ws)
        assert len(result) == 1
        assert result[0]["name"] == "Marketing"
        mock_get.assert_called_once_with(
            "/api/workspaces/ws-123/catalogs",
            "fake-token",
            workspace_id="ws-123",
        )
    finally:
        ui_main._backend_get = original_get


def test_catalog_access_request_requires_reason_and_exposes_requester_details():
    fix = _setup_fixture()
    session = fix["session"]

    backend_main.app.dependency_overrides[database.get_db] = lambda: session
    try:
        # 1. Request without reason -> 400
        resp_empty = client.post(
            f"/api/catalogs/{fix['cat_a_private'].id}/access/request",
            json={"mode": "read", "workspace_id": str(fix["ws_b"].id), "message": "   "},
            headers={"Authorization": f"Bearer {fix['bob_token']}"},
        )
        assert resp_empty.status_code == 400
        assert "reason for the request is required" in resp_empty.json()["detail"]

        # 2. Request with reason > 500 chars -> 400
        resp_long = client.post(
            f"/api/catalogs/{fix['cat_a_private'].id}/access/request",
            json={"mode": "read", "workspace_id": str(fix["ws_b"].id), "message": "x" * 501},
            headers={"Authorization": f"Bearer {fix['bob_token']}"},
        )
        assert resp_long.status_code == 400
        assert "500 characters" in resp_long.json()["detail"]

        # 3. Request with valid reason -> 200
        resp_ok = client.post(
            f"/api/catalogs/{fix['cat_a_private'].id}/access/request",
            json={
                "mode": "read",
                "workspace_id": str(fix["ws_b"].id),
                "message": "  Need payroll benchmarks for Q4 audit  ",
            },
            headers={"Authorization": f"Bearer {fix['bob_token']}"},
        )
        assert resp_ok.status_code == 200

        # 4. Alice (admin of ws_a) inspects inbound access requests and settings/catalogs
        resp_inbound = client.get(
            f"/api/workspaces/{fix['ws_a'].id}/access/requests/inbound",
            headers={"Authorization": f"Bearer {fix['alice_token']}"},
        )
        assert resp_inbound.status_code == 200
        inbound_items = resp_inbound.json()
        assert len(inbound_items) == 1
        assert inbound_items[0]["requester_name"] == "Bob Beta"
        assert inbound_items[0]["requester_email"] == "bob@lake.local"
        assert inbound_items[0]["message"] == "Need payroll benchmarks for Q4 audit"

        resp_settings = client.get(
            f"/api/workspaces/{fix['ws_a'].id}/settings/catalogs",
            headers={"Authorization": f"Bearer {fix['alice_token']}"},
        )
        assert resp_settings.status_code == 200
        pending_out = [
            o
            for o in resp_settings.json()["shared_with_others"]
            if o["catalog_id"] == str(fix["cat_a_private"].id)
        ]
        assert len(pending_out) == 1
        assert pending_out[0]["requester_name"] == "Bob Beta"
        assert pending_out[0]["requester_email"] == "bob@lake.local"
        assert pending_out[0]["message"] == "Need payroll benchmarks for Q4 audit"
    finally:
        backend_main.app.dependency_overrides.clear()
