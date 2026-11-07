"""Tests for catalog inline description editing and drag-and-drop reordering with user preferences (#399)."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker


# Handle SQLite JSONB compilation
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


backend_dir = str(Path(__file__).parents[1] / "backend")
ui_dir = str(Path(__file__).parents[1] / "ui")
for d in (backend_dir, ui_dir):
    if d not in sys.path:
        sys.path.insert(0, d)

import pytest  # noqa: E402
from database import Base  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from models import Catalog, User, Workspace, WorkspaceMember  # noqa: E402
from schemas import CatalogOrderPreferenceRequest, UpdateCatalogRequest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import backend.main as backend_main  # noqa: E402
import ui.main as ui_main  # noqa: E402


@pytest.fixture
def db_session(tmp_path):
    """In-memory SQLite session with all models created."""
    db_file = tmp_path / "test_catalog_ordering.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionMaker = sessionmaker(bind=engine)
    session = SessionMaker()

    yield session

    session.close()


def test_update_catalog_description_permissions(db_session: Session):
    """Verify owner and write permissions can update catalog description, while read-only users are refused (403)."""
    ws = Workspace(id=uuid.uuid4(), name="Engineering", slug="engineering", status="active")
    db_session.add(ws)

    owner_user = User(
        id=uuid.uuid4(),
        email="owner@lot.test",
        display_name="Owner User",
        role="user",
        is_active=True,
    )
    writer_user = User(
        id=uuid.uuid4(),
        email="writer@lot.test",
        display_name="Writer User",
        role="user",
        is_active=True,
    )
    reader_user = User(
        id=uuid.uuid4(),
        email="reader@lot.test",
        display_name="Reader User",
        role="user",
        is_active=True,
    )
    db_session.add_all([owner_user, writer_user, reader_user])
    db_session.commit()

    # Owner membership
    db_session.add(
        WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=owner_user.id, role="admin")
    )
    # Writer membership
    db_session.add(
        WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=writer_user.id, role="write")
    )
    # Reader membership
    db_session.add(
        WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=reader_user.id, role="read")
    )

    catalog = Catalog(
        id=uuid.uuid4(),
        owner_workspace_id=ws.id,
        name="Telemetry",
        slug="telemetry",
        description="",
    )
    db_session.add(catalog)
    db_session.commit()

    # 1. Owner can update description
    req = UpdateCatalogRequest(description="Real-time IoT metrics and telemetry data")
    res = backend_main.update_catalog(
        catalog_id=str(catalog.id),
        req=req,
        db=db_session,
        current_user=owner_user,
        ws_ctx=(ws, "admin"),
    )
    assert res.description == "Real-time IoT metrics and telemetry data"

    # 2. Writer can update description inline
    req_writer = UpdateCatalogRequest(description="Updated by writer with inline editing")
    res_writer = backend_main.update_catalog(
        catalog_id=str(catalog.id),
        req=req_writer,
        db=db_session,
        current_user=writer_user,
        ws_ctx=(ws, "write"),
    )
    assert res_writer.description == "Updated by writer with inline editing"

    # 3. Read-only user cannot update description -> 403 Forbidden
    req_reader = UpdateCatalogRequest(description="Hacked by reader")
    with pytest.raises(HTTPException) as exc_info:
        backend_main.update_catalog(
            catalog_id=str(catalog.id),
            req=req_reader,
            db=db_session,
            current_user=reader_user,
            ws_ctx=(ws, "read"),
        )
    assert exc_info.value.status_code == 403
    assert "Write access required" in str(exc_info.value.detail)

    # 4. Whitespace description is stripped cleanly
    req_empty = UpdateCatalogRequest(description="   ")
    res_empty = backend_main.update_catalog(
        catalog_id=str(catalog.id),
        req=req_empty,
        db=db_session,
        current_user=owner_user,
        ws_ctx=(ws, "admin"),
    )
    assert res_empty.description == ""


def test_user_workspace_preferences_persistence_and_scoping(db_session: Session):
    """Verify PUT and GET user preferences endpoints, persistence in user_workspace_preferences, and per-user scoping."""
    ws = Workspace(id=uuid.uuid4(), name="Analytics", slug="analytics", status="active")
    db_session.add(ws)

    alice = User(id=uuid.uuid4(), email="alice@lot.test", role="user", is_active=True)
    bob = User(id=uuid.uuid4(), email="bob@lot.test", role="user", is_active=True)
    db_session.add_all([alice, bob])
    db_session.commit()

    db_session.add_all(
        [
            WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=alice.id, role="admin"),
            WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=bob.id, role="admin"),
        ]
    )
    db_session.commit()

    cat1 = str(uuid.uuid4())
    cat2 = str(uuid.uuid4())
    cat3 = str(uuid.uuid4())

    # 1. Alice sets custom order
    alice_order = [cat3, cat1, cat2]
    req = CatalogOrderPreferenceRequest(
        native_catalog_order=alice_order,
        shared_catalog_order=[],
    )
    res = backend_main.save_catalog_order_preference(
        workspace_id=str(ws.id),
        req=req,
        db=db_session,
        current_user=alice,
    )
    assert res.native_catalog_order == alice_order

    # Verify retrieval
    alice_prefs = backend_main.get_catalog_order_preference(
        workspace_id=str(ws.id),
        db=db_session,
        current_user=alice,
    )
    assert alice_prefs.native_catalog_order == alice_order

    # 2. Bob's preferences are completely separate and unaffected by Alice
    bob_prefs = backend_main.get_catalog_order_preference(
        workspace_id=str(ws.id),
        db=db_session,
        current_user=bob,
    )
    assert bob_prefs.native_catalog_order == []

    # 3. Bob sets a different preference order
    bob_order = [cat2, cat3, cat1]
    backend_main.save_catalog_order_preference(
        workspace_id=str(ws.id),
        req=CatalogOrderPreferenceRequest(native_catalog_order=bob_order),
        db=db_session,
        current_user=bob,
    )

    # Confirm Alice's order remained completely untouched
    alice_prefs_after = backend_main.get_catalog_order_preference(
        workspace_id=str(ws.id),
        db=db_session,
        current_user=alice,
    )
    assert alice_prefs_after.native_catalog_order == alice_order

    # 4. Non-member access check -> 403
    stranger = User(id=uuid.uuid4(), email="stranger@lot.test", role="user", is_active=True)
    db_session.add(stranger)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        backend_main.save_catalog_order_preference(
            workspace_id=str(ws.id),
            req=req,
            db=db_session,
            current_user=stranger,
        )
    assert exc_info.value.status_code == 403


def test_workspace_catalogs_ordering_by_preference(db_session: Session):
    """Verify workspace_catalogs returns catalogs ordered according to user-specific preference list."""
    ws = Workspace(id=uuid.uuid4(), name="Finance", slug="finance", status="active")
    db_session.add(ws)

    user = User(id=uuid.uuid4(), email="trader@lot.test", role="user", is_active=True)
    db_session.add(user)
    db_session.commit()

    db_session.add(
        WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=user.id, role="admin")
    )

    c1 = Catalog(
        id=uuid.uuid4(),
        owner_workspace_id=ws.id,
        name="CatAlpha",
        slug="alpha",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    c2 = Catalog(
        id=uuid.uuid4(),
        owner_workspace_id=ws.id,
        name="CatBeta",
        slug="beta",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    c3 = Catalog(
        id=uuid.uuid4(),
        owner_workspace_id=ws.id,
        name="CatGamma",
        slug="gamma",
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    c4 = Catalog(
        id=uuid.uuid4(),
        owner_workspace_id=ws.id,
        name="CatDelta",
        slug="delta",
        created_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    db_session.add_all([c1, c2, c3, c4])
    db_session.commit()

    # Default order without preferences: created_at ascending [c1, c2, c3, c4]
    default_res = backend_main.workspace_catalogs(str(ws.id), db=db_session, current_user=user)
    assert [c.name for c in default_res] == ["CatAlpha", "CatBeta", "CatGamma", "CatDelta"]

    # Set user preference order: [c3, c1] (c2 and c4 unranked -> should follow in created_at order)
    backend_main.save_catalog_order_preference(
        workspace_id=str(ws.id),
        req=CatalogOrderPreferenceRequest(native_catalog_order=[str(c3.id), str(c1.id)]),
        db=db_session,
        current_user=user,
    )

    custom_res = backend_main.workspace_catalogs(str(ws.id), db=db_session, current_user=user)
    assert [c.name for c in custom_res] == ["CatGamma", "CatAlpha", "CatBeta", "CatDelta"]


def test_ui_catalog_view_default_description_and_dnd_affordances():
    """Verify UI templates render 'No description' placeholder, inline click-to-edit elements, and drag-and-drop markup."""
    import jwt

    client = TestClient(ui_main.app)

    cat_id1 = str(uuid.uuid4())
    cat_id2 = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())

    mock_workspaces = [
        {"id": ws_id, "name": "Default Workspace", "slug": "default", "role": "admin"}
    ]

    mock_catalogs = [
        {
            "id": cat_id1,
            "name": "Production Data",
            "slug": "production-data",
            "description": "",  # Empty -> triggers "No description"
            "owner_workspace_id": ws_id,
            "my_access": "owner",
            "schemas": [],
        },
        {
            "id": cat_id2,
            "name": "Analytics Warehouse",
            "slug": "analytics-warehouse",
            "description": "Warehouse marts for BI",
            "owner_workspace_id": ws_id,
            "my_access": "write",
            "schemas": [],
        },
    ]

    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "test@lot.test", "role": "admin"},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    def fake_backend_get(path, *args, **kwargs):
        if "user-preferences/catalog-order" in path:
            return {"native_catalog_order": [cat_id2, cat_id1], "shared_catalog_order": []}
        if "trash" in path:
            return []
        if "catalogs" in path:
            return mock_catalogs
        return None

    with (
        patch.object(ui_main, "AUTH_ENABLED", True),
        patch.object(ui_main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(ui_main, "_backend_get", side_effect=fake_backend_get),
    ):
        resp = client.get(
            "/catalog",
            cookies={
                "lake_token": token,
                ui_main._WORKSPACE_COOKIE: ws_id,
            },
        )

    assert resp.status_code == 200
    html = resp.text

    # 1. Verify "No description" placeholder rendered for empty description
    assert "No description" in html
    assert "Warehouse marts for BI" in html

    # 2. Verify inline editing affordances (inputs, save/cancel buttons)
    assert f'id="desc-input-{cat_id1}"' in html
    assert f'id="desc-edit-{cat_id1}"' in html
    assert f"startCatalogDescEdit('{cat_id1}')" in html
    assert f"saveCatalogDesc('{cat_id1}')" in html
    assert f"cancelCatalogDescEdit('{cat_id1}')" in html

    # 3. Verify drag-and-drop attributes
    assert 'class="catalog-drag-handle' in html
    assert 'data-section="native"' in html
    assert 'draggable="true"' in html
    assert "catalog-header" in html

    # 4. Verify catalog ordering by preference loaded in UI
    # cat_id2 was preferred first in mock preferences
    idx_cat2 = html.find(f'id="catalog-{cat_id2}"')
    idx_cat1 = html.find(f'id="catalog-{cat_id1}"')
    assert idx_cat2 != -1 and idx_cat1 != -1
    assert idx_cat2 < idx_cat1


def test_ui_catalog_view_read_only_access_no_edit_affordance():
    """Verify read-only users cannot edit descriptions inline (no input fields or edit buttons)."""
    import jwt

    client = TestClient(ui_main.app)

    cat_id = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())

    mock_workspaces = [
        {"id": ws_id, "name": "Default Workspace", "slug": "default", "role": "read"}
    ]

    mock_catalogs = [
        {
            "id": cat_id,
            "name": "Audit Logs",
            "slug": "audit-logs",
            "description": "",
            "owner_workspace_id": ws_id,
            "my_access": "read",
            "schemas": [],
        }
    ]

    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "viewer@lot.test", "role": "user"},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    with (
        patch.object(ui_main, "AUTH_ENABLED", True),
        patch.object(ui_main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(ui_main, "_backend_get", return_value=mock_catalogs),
    ):
        resp = client.get(
            "/catalog",
            cookies={
                "lake_token": token,
                ui_main._WORKSPACE_COOKIE: ws_id,
            },
        )

    assert resp.status_code == 200
    html = resp.text

    # "No description" text is shown
    assert "No description" in html
    # But edit controls are NOT present for read-only user
    assert f'id="desc-input-{cat_id}"' not in html
    assert f'id="desc-edit-{cat_id}"' not in html
    assert f"startCatalogDescEdit('{cat_id}')" not in html


def test_ui_proxy_patch_and_preferences_endpoints():
    """Verify UI proxy endpoints for PATCH catalog and PUT/GET user preferences."""
    import jwt

    client = TestClient(ui_main.app)
    ws_id = str(uuid.uuid4())
    cat_id = str(uuid.uuid4())

    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "dev@lot.test", "role": "admin"},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    # 1. Test UI PATCH /api/catalogs/{cat_id}
    with patch.object(
        ui_main,
        "_backend_patch_with_error",
        return_value=({"id": cat_id, "description": "New Desc"}, None),
    ):
        res = client.patch(
            f"/api/catalogs/{cat_id}",
            json={"description": "New Desc"},
            cookies={"lake_token": token, ui_main._WORKSPACE_COOKIE: ws_id},
        )
        assert res.status_code == 200
        assert res.json()["description"] == "New Desc"

    # 2. Test UI PUT /api/workspaces/{ws_id}/user-preferences/catalog-order
    with patch.object(
        ui_main,
        "_backend_put",
        return_value=(200, {"workspace_id": ws_id, "native_catalog_order": [cat_id]}),
    ):
        res = client.put(
            f"/api/workspaces/{ws_id}/user-preferences/catalog-order",
            json={"native_catalog_order": [cat_id], "shared_catalog_order": []},
            cookies={"lake_token": token, ui_main._WORKSPACE_COOKIE: ws_id},
        )
        assert res.status_code == 200
        assert res.json()["native_catalog_order"] == [cat_id]

    # 3. Test UI GET /api/workspaces/{ws_id}/user-preferences/catalog-order
    with patch.object(
        ui_main,
        "_backend_get",
        return_value={"native_catalog_order": [cat_id], "shared_catalog_order": []},
    ):
        res = client.get(
            f"/api/workspaces/{ws_id}/user-preferences/catalog-order",
            cookies={"lake_token": token, ui_main._WORKSPACE_COOKIE: ws_id},
        )
        assert res.status_code == 200
        assert res.json()["native_catalog_order"] == [cat_id]
