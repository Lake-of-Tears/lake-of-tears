"""Tests for user-scoped catalog access requests, visibility, and revocation (#294)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session, sessionmaker
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
    CatalogSchema,
    CatalogTable,
    User,
    UserSession,
    Workspace,
    WorkspaceMember,
)

spec = importlib.util.spec_from_file_location(
    "backend_main_cat_scoping", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_cat_scoping"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


def _create_token(user: User, session: Session) -> str:
    sess_id = uuid.uuid4()
    session.add(
        UserSession(
            id=sess_id,
            user_id=user.id,
            created_at=datetime.now(UTC),
            last_active_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            is_revoked=False,
        )
    )
    session.commit()
    return jwt.encode(
        {"sub": str(user.id), "email": user.email, "role": user.role, "jti": str(sess_id)},
        auth.SECRET_KEY,
        algorithm="HS256",
    )


def _setup_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session: Session = TestingSessionLocal()

    # Create users
    owner_admin = User(
        id=uuid.uuid4(),
        email="owner_admin@example.com",
        display_name="Owner Admin",
        hashed_password="pw",
        role="user",
        is_active=True,
    )
    user_a = User(
        id=uuid.uuid4(),
        email="user_a@example.com",
        display_name="User Alpha",
        hashed_password="pw",
        role="user",
        is_active=True,
    )
    user_b = User(
        id=uuid.uuid4(),
        email="user_b@example.com",
        display_name="User Beta",
        hashed_password="pw",
        role="user",
        is_active=True,
    )
    session.add_all([owner_admin, user_a, user_b])
    session.commit()

    # Workspaces
    ws_owner = Workspace(
        id=uuid.uuid4(),
        name="Workspace Alpha (Owner)",
        slug="ws-alpha",
        status="active",
        created_at=datetime.now(UTC),
    )
    ws_consumer = Workspace(
        id=uuid.uuid4(),
        name="Workspace Beta (Consumer)",
        slug="ws-beta",
        status="active",
        created_at=datetime.now(UTC),
    )
    session.add_all([ws_owner, ws_consumer])
    session.commit()

    # Memberships: owner_admin is admin of ws_owner; user_a & user_b are members of ws_consumer
    m_owner = WorkspaceMember(workspace_id=ws_owner.id, user_id=owner_admin.id, role="admin")
    m_user_a = WorkspaceMember(workspace_id=ws_consumer.id, user_id=user_a.id, role="member")
    m_user_b = WorkspaceMember(workspace_id=ws_consumer.id, user_id=user_b.id, role="member")
    session.add_all([m_owner, m_user_a, m_user_b])
    session.commit()

    # Catalog owned by ws_owner
    catalog = Catalog(
        id=uuid.uuid4(),
        name="Shared Analytics",
        slug="shared-analytics",
        owner_workspace_id=ws_owner.id,
        created_at=datetime.now(UTC),
    )
    session.add(catalog)
    session.commit()

    schema = CatalogSchema(
        id=uuid.uuid4(),
        catalog_id=catalog.id,
        name="Core",
        slug="core",
        tier="custom",
    )
    session.add(schema)
    session.commit()

    table = CatalogTable(
        id=uuid.uuid4(),
        schema_id=schema.id,
        name="Events",
        slug="events",
        s3_path_pattern="s3://lake/events.parquet",
    )
    session.add(table)
    session.commit()

    return {
        "session": session,
        "ws_owner": ws_owner,
        "ws_consumer": ws_consumer,
        "owner_admin": owner_admin,
        "owner_admin_token": _create_token(owner_admin, session),
        "user_a": user_a,
        "user_a_token": _create_token(user_a, session),
        "user_b": user_b,
        "user_b_token": _create_token(user_b, session),
        "catalog": catalog,
    }


def test_catalog_access_requests_and_grants_user_scoped():
    fix = _setup_fixture()
    session = fix["session"]
    backend_main.app.dependency_overrides[database.get_db] = lambda: session
    backend_main.app.dependency_overrides[auth.get_db] = lambda: session

    try:
        ws_c_id = str(fix["ws_consumer"].id)
        cat_id = str(fix["catalog"].id)

        # 1. Directory initial state: both User A and User B see access_status = "none"
        res_a = client.get(
            f"/api/catalogs/directory?workspace_id={ws_c_id}",
            headers={"Authorization": f"Bearer {fix['user_a_token']}"},
        )
        assert res_a.status_code == 200
        assert res_a.json()[0]["access_status"] == "none"

        res_b = client.get(
            f"/api/catalogs/directory?workspace_id={ws_c_id}",
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        assert res_b.status_code == 200
        assert res_b.json()[0]["access_status"] == "none"

        # 2. User A requests access
        req_res = client.post(
            f"/api/catalogs/{cat_id}/access/request",
            json={
                "workspace_id": ws_c_id,
                "mode": "read",
                "message": "Need read access for analytics",
            },
            headers={"Authorization": f"Bearer {fix['user_a_token']}"},
        )
        assert req_res.status_code == 200

        # User A now sees "pending", User B still sees "none"
        res_a_pending = client.get(
            f"/api/catalogs/directory?workspace_id={ws_c_id}",
            headers={"Authorization": f"Bearer {fix['user_a_token']}"},
        )
        assert res_a_pending.json()[0]["access_status"] == "pending"

        res_b_none = client.get(
            f"/api/catalogs/directory?workspace_id={ws_c_id}",
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        assert res_b_none.json()[0]["access_status"] == "none"

        # 3. Owner admin approves User A's grant
        inbound = client.get(
            f"/api/workspaces/{fix['ws_owner'].id}/access/requests/inbound",
            headers={"Authorization": f"Bearer {fix['owner_admin_token']}"},
        )
        assert inbound.status_code == 200
        grants = inbound.json()
        assert len(grants) == 1
        assert grants[0]["requester_email"] == fix["user_a"].email
        access_a_id = grants[0]["access_id"]

        review_res = client.patch(
            f"/api/catalogs/{cat_id}/access/{access_a_id}",
            json={"status": "approved"},
            headers={
                "Authorization": f"Bearer {fix['owner_admin_token']}",
                "X-Workspace-Id": str(fix["ws_owner"].id),
            },
        )
        assert review_res.status_code == 200

        # 4. Visible only to User A (not User B)
        # Check /api/catalogs
        cat_list_a = client.get(
            "/api/catalogs",
            headers={
                "Authorization": f"Bearer {fix['user_a_token']}",
                "X-Workspace-Id": ws_c_id,
            },
        )
        assert cat_list_a.status_code == 200
        assert any(c["id"] == cat_id for c in cat_list_a.json())

        cat_list_b = client.get(
            "/api/catalogs",
            headers={
                "Authorization": f"Bearer {fix['user_b_token']}",
                "X-Workspace-Id": ws_c_id,
            },
        )
        assert cat_list_b.status_code == 200
        assert not any(c["id"] == cat_id for c in cat_list_b.json())

        # Check /api/workspaces/{ws_id}/catalogs
        ws_cats_a = client.get(
            f"/api/workspaces/{ws_c_id}/catalogs",
            headers={"Authorization": f"Bearer {fix['user_a_token']}"},
        )
        assert any(c["id"] == cat_id for c in ws_cats_a.json())

        ws_cats_b = client.get(
            f"/api/workspaces/{ws_c_id}/catalogs",
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        assert not any(c["id"] == cat_id for c in ws_cats_b.json())

        # Check /api/workspaces/{ws_id}/catalog-manifest
        manifest_a = client.get(
            f"/api/workspaces/{ws_c_id}/catalog-manifest",
            headers={"Authorization": f"Bearer {fix['user_a_token']}"},
        )
        assert any(c["id"] == cat_id for c in manifest_a.json())

        manifest_b = client.get(
            f"/api/workspaces/{ws_c_id}/catalog-manifest",
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        assert not any(c["id"] == cat_id for c in manifest_b.json())

        # Check Settings "Shared with me"
        settings_a = client.get(
            f"/api/workspaces/{ws_c_id}/settings/catalogs",
            headers={"Authorization": f"Bearer {fix['user_a_token']}"},
        )
        assert len(settings_a.json()["shared"]) == 1
        assert settings_a.json()["shared"][0]["catalog_id"] == cat_id

        settings_b = client.get(
            f"/api/workspaces/{ws_c_id}/settings/catalogs",
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        assert len(settings_b.json()["shared"]) == 0

        # Check Directory status
        res_a_appr = client.get(
            f"/api/catalogs/directory?workspace_id={ws_c_id}",
            headers={"Authorization": f"Bearer {fix['user_a_token']}"},
        )
        assert res_a_appr.json()[0]["access_status"] == "approved"

        res_b_appr = client.get(
            f"/api/catalogs/directory?workspace_id={ws_c_id}",
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        assert res_b_appr.json()[0]["access_status"] == "none"

        # Check has_shared_catalogs in /api/workspaces
        ws_list_a = client.get(
            "/api/workspaces",
            headers={"Authorization": f"Bearer {fix['user_a_token']}"},
        )
        ws_c_a = next(w for w in ws_list_a.json() if w["id"] == ws_c_id)
        assert ws_c_a["has_shared_catalogs"] is True

        ws_list_b = client.get(
            "/api/workspaces",
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        ws_c_b = next(w for w in ws_list_b.json() if w["id"] == ws_c_id)
        assert ws_c_b["has_shared_catalogs"] is False

        # Direct access enforcement (_assert_catalog_access)
        cat_direct_a = client.get(
            f"/api/catalogs/{cat_id}",
            headers={
                "Authorization": f"Bearer {fix['user_a_token']}",
                "X-Workspace-Id": ws_c_id,
            },
        )
        assert cat_direct_a.status_code == 200
        assert cat_direct_a.json()["my_access"] == "read"

        cat_direct_b = client.get(
            f"/api/catalogs/{cat_id}",
            headers={
                "Authorization": f"Bearer {fix['user_b_token']}",
                "X-Workspace-Id": ws_c_id,
            },
        )
        assert cat_direct_b.status_code == 404

        # ai_service.get_workspace_catalogs_from_db
        import ai_service

        ai_cats_a = ai_service.get_workspace_catalogs_from_db(
            session, fix["ws_consumer"].id, user_id=fix["user_a"].id
        )
        assert any(str(c["id"]) == cat_id for c in ai_cats_a)

        ai_cats_b = ai_service.get_workspace_catalogs_from_db(
            session, fix["ws_consumer"].id, user_id=fix["user_b"].id
        )
        assert not any(str(c["id"]) == cat_id for c in ai_cats_b)

        # 5. User B in the same workspace can independently request access
        req_b_res = client.post(
            f"/api/catalogs/{cat_id}/access/request",
            json={"workspace_id": ws_c_id, "mode": "read", "message": "User B also needs access"},
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        assert req_b_res.status_code == 200

        # Verify owning workspace admin sees both grants in Shared with others
        owner_settings = client.get(
            f"/api/workspaces/{fix['ws_owner'].id}/settings/catalogs",
            headers={"Authorization": f"Bearer {fix['owner_admin_token']}"},
        )
        swo = owner_settings.json()["shared_with_others"]
        assert len(swo) == 2
        requester_emails = {item["requester_email"] for item in swo}
        assert fix["user_a"].email in requester_emails
        assert fix["user_b"].email in requester_emails

        # 6. Approve User B's grant
        grant_b_item = next(item for item in swo if item["requester_email"] == fix["user_b"].email)
        access_b_id = grant_b_item["access_id"]
        review_b = client.patch(
            f"/api/catalogs/{cat_id}/access/{access_b_id}",
            json={"status": "approved"},
            headers={
                "Authorization": f"Bearer {fix['owner_admin_token']}",
                "X-Workspace-Id": str(fix["ws_owner"].id),
            },
        )
        assert review_b.status_code == 200

        # Now both User A and User B see the catalog
        ws_cats_b_after = client.get(
            f"/api/workspaces/{ws_c_id}/catalogs",
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        assert any(c["id"] == cat_id for c in ws_cats_b_after.json())

        # 7. Self-service removal: User B removes their own shared catalog (without being a workspace admin)
        del_b = client.delete(
            f"/api/workspaces/{ws_c_id}/catalogs/{cat_id}/shared",
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        assert del_b.status_code == 200

        # User B no longer has access; User A still has access
        ws_cats_b_del = client.get(
            f"/api/workspaces/{ws_c_id}/catalogs",
            headers={"Authorization": f"Bearer {fix['user_b_token']}"},
        )
        assert not any(c["id"] == cat_id for c in ws_cats_b_del.json())

        ws_cats_a_still = client.get(
            f"/api/workspaces/{ws_c_id}/catalogs",
            headers={"Authorization": f"Bearer {fix['user_a_token']}"},
        )
        assert any(c["id"] == cat_id for c in ws_cats_a_still.json())

        # 8. Admin revocation: Owner admin revokes User A's grant
        del_a_by_admin = client.delete(
            f"/api/catalogs/{cat_id}/access/{access_a_id}",
            headers={
                "Authorization": f"Bearer {fix['owner_admin_token']}",
                "X-Workspace-Id": str(fix["ws_owner"].id),
            },
        )
        assert del_a_by_admin.status_code == 200

        # User A no longer has access
        ws_cats_a_revoked = client.get(
            f"/api/workspaces/{ws_c_id}/catalogs",
            headers={"Authorization": f"Bearer {fix['user_a_token']}"},
        )
        assert not any(c["id"] == cat_id for c in ws_cats_a_revoked.json())

    finally:
        backend_main.app.dependency_overrides.clear()
