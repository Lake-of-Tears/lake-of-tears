from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import jwt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON

backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import auth  # noqa: E402
import database  # noqa: E402
from models import (  # noqa: E402
    Base,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceShare,
)

spec = importlib.util.spec_from_file_location(
    "backend_main_workspace_shares", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main_workspace_shares"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


def _setup_test_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    # Superadmin
    admin_id = uuid.uuid4()
    superadmin = User(
        id=admin_id,
        email="superadmin@example.com",
        display_name="Super Admin",
        role="superadmin",
        is_active=True,
    )
    superadmin_token = jwt.encode(
        {"sub": str(admin_id), "email": superadmin.email, "role": superadmin.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )

    # Workspace Owner / Admin
    owner_id = uuid.uuid4()
    ws_admin = User(
        id=owner_id,
        email="owner@example.com",
        display_name="Workspace Owner",
        role="user",
        is_active=True,
    )
    ws_admin_token = jwt.encode(
        {"sub": str(owner_id), "email": ws_admin.email, "role": ws_admin.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )

    # Regular Requester User
    user_id = uuid.uuid4()
    requester = User(
        id=user_id,
        email="requester@example.com",
        display_name="Share Requester",
        role="user",
        is_active=True,
    )
    requester_token = jwt.encode(
        {"sub": str(user_id), "email": requester.email, "role": requester.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )

    # Other non-member User
    other_id = uuid.uuid4()
    other_user = User(
        id=other_id,
        email="other@example.com",
        display_name="Other User",
        role="user",
        is_active=True,
    )
    other_token = jwt.encode(
        {"sub": str(other_id), "email": other_user.email, "role": other_user.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )

    session.add_all([superadmin, ws_admin, requester, other_user])

    # Workspace 1 (active)
    ws1 = Workspace(
        id=uuid.uuid4(),
        name="Target Engineering Workspace",
        slug="target-engineering",
        status="active",
        created_by=owner_id,
    )
    # Workspace 2 (inactive)
    ws_inactive = Workspace(
        id=uuid.uuid4(),
        name="Inactive Workspace",
        slug="inactive-ws",
        status="inactive",
        created_by=owner_id,
    )
    session.add_all([ws1, ws_inactive])

    # Add ws_admin as admin member of ws1
    member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws1.id,
        user_id=owner_id,
        role="admin",
    )
    session.add(member)
    session.commit()

    return {
        "session": session,
        "superadmin": (superadmin, superadmin_token),
        "ws_admin": (ws_admin, ws_admin_token),
        "requester": (requester, requester_token),
        "other_user": (other_user, other_token),
        "ws1": ws1,
        "ws_inactive": ws_inactive,
    }


@pytest.fixture
def test_ctx():
    fixture = _setup_test_db()
    session = fixture["session"]
    backend_main.app.dependency_overrides[database.get_db] = lambda: session
    try:
        yield fixture
    finally:
        backend_main.app.dependency_overrides.clear()
        session.close()


# ── Share Request Endpoint Tests ───────────────────────────────────────────────


def test_request_workspace_share_success(test_ctx):
    requester, token = test_ctx["requester"]
    ws = test_ctx["ws1"]

    resp = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {token}"},
        json={"note": "Please give me view access to review docs"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["workspace_id"] == str(ws.id)
    assert data["workspace_name"] == ws.name
    assert data["workspace_slug"] == ws.slug
    assert data["user_id"] == str(requester.id)
    assert data["user_email"] == requester.email
    assert data["user_display_name"] == requester.display_name
    assert data["status"] == "pending"
    assert data["approved_at"] is None
    assert data["approved_by"] is None


def test_request_share_nonexistent_or_inactive_workspace(test_ctx):
    _, token = test_ctx["requester"]
    ws_inactive = test_ctx["ws_inactive"]

    # Non-existent
    resp = client.post(
        f"/api/workspaces/{uuid.uuid4()}/shares",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404

    # Inactive
    resp_inactive = client.post(
        f"/api/workspaces/{ws_inactive.id}/shares",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp_inactive.status_code == 404


def test_request_share_direct_member_rejected(test_ctx):
    ws_admin, admin_token = test_ctx["ws_admin"]
    ws = test_ctx["ws1"]

    # ws_admin is already a member
    resp = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 400
    assert "already a direct member" in resp.json()["detail"]


def test_request_share_duplicate_pending_rejected(test_ctx):
    _, token = test_ctx["requester"]
    ws = test_ctx["ws1"]

    # First request
    resp1 = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp1.status_code == 200

    # Second request while pending
    resp2 = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 409
    assert "already pending" in resp2.json()["detail"]


def test_request_share_duplicate_approved_rejected(test_ctx):
    requester, token = test_ctx["requester"]
    ws = test_ctx["ws1"]
    session = test_ctx["session"]

    # Pre-create approved share
    share = WorkspaceShare(
        workspace_id=ws.id,
        user_id=requester.id,
        status="approved",
        requested_at=datetime.now(UTC),
        approved_at=datetime.now(UTC),
    )
    session.add(share)
    session.commit()

    resp = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert "already approved" in resp.json()["detail"]


def test_request_share_re_request_after_rejected(test_ctx):
    requester, token = test_ctx["requester"]
    ws = test_ctx["ws1"]
    session = test_ctx["session"]

    # Pre-create rejected share
    share = WorkspaceShare(
        workspace_id=ws.id,
        user_id=requester.id,
        status="rejected",
        requested_at=datetime.now(UTC),
        approved_at=datetime.now(UTC),
    )
    session.add(share)
    session.commit()

    resp = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["approved_at"] is None
    assert data["approved_by"] is None


# ── Review & Approval Workflow Tests ──────────────────────────────────────────


def test_list_workspace_shares_permissions(test_ctx):
    ws = test_ctx["ws1"]
    _, user_token = test_ctx["requester"]
    _, admin_token = test_ctx["ws_admin"]
    _, super_token = test_ctx["superadmin"]

    # Create a pending share
    client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )

    # Regular user gets 403
    resp_user = client.get(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp_user.status_code == 403

    # Workspace Admin gets 200
    resp_admin = client.get(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp_admin.status_code == 200
    shares = resp_admin.json()
    assert len(shares) == 1
    assert shares[0]["status"] == "pending"

    # Superadmin gets 200
    resp_super = client.get(
        f"/api/workspaces/{ws.id}/shares?status=pending",
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert resp_super.status_code == 200
    assert len(resp_super.json()) == 1


def test_review_workspace_share_approval_and_rejection(test_ctx):
    ws = test_ctx["ws1"]
    requester, user_token = test_ctx["requester"]
    ws_admin, admin_token = test_ctx["ws_admin"]

    # Create request
    resp_req = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    share_id = resp_req.json()["id"]

    # Non-admin cannot review
    resp_unauth = client.patch(
        f"/api/workspaces/{ws.id}/shares/{share_id}",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"status": "approved"},
    )
    assert resp_unauth.status_code == 403

    # Admin approves
    resp_approve = client.patch(
        f"/api/workspaces/{ws.id}/shares/{share_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "approved"},
    )
    assert resp_approve.status_code == 200
    data = resp_approve.json()
    assert data["status"] == "approved"
    assert data["approved_by"] == str(ws_admin.id)
    assert data["approved_at"] is not None

    # Admin rejects
    resp_reject = client.patch(
        f"/api/workspaces/{ws.id}/shares/{share_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "rejected"},
    )
    assert resp_reject.status_code == 200
    assert resp_reject.json()["status"] == "rejected"


# ── Disconnect & Deletion Tests ────────────────────────────────────────────────


def test_self_disconnect_workspace_share(test_ctx):
    ws = test_ctx["ws1"]
    requester, user_token = test_ctx["requester"]
    _, admin_token = test_ctx["ws_admin"]

    # Request & approve
    req_resp = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    share_id = req_resp.json()["id"]
    client.patch(
        f"/api/workspaces/{ws.id}/shares/{share_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "approved"},
    )

    # Disconnect
    resp_del = client.delete(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp_del.status_code == 204

    # Verify gone in db
    session = test_ctx["session"]
    share = session.query(WorkspaceShare).filter_by(id=uuid.UUID(share_id)).first()
    assert share is None

    # Disconnecting again returns 404
    resp_del2 = client.delete(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp_del2.status_code == 404


def test_admin_delete_workspace_share_by_id(test_ctx):
    ws = test_ctx["ws1"]
    _, user_token = test_ctx["requester"]
    _, admin_token = test_ctx["ws_admin"]

    req_resp = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    share_id = req_resp.json()["id"]

    resp_del = client.delete(
        f"/api/workspaces/{ws.id}/shares/{share_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp_del.status_code == 204

    session = test_ctx["session"]
    assert session.query(WorkspaceShare).filter_by(id=uuid.UUID(share_id)).first() is None


# ── User Personal Shares Endpoint ─────────────────────────────────────────────


def test_list_my_workspace_shares(test_ctx):
    ws = test_ctx["ws1"]
    _, user_token = test_ctx["requester"]

    client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )

    resp = client.get(
        "/api/workspaces/shares/mine",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp.status_code == 200
    shares = resp.json()
    assert len(shares) == 1
    assert shares[0]["workspace_name"] == ws.name


# ── Listing & Auth Context Jailing Integration Tests ──────────────────────────


def test_workspace_listing_and_detail_with_approved_share(test_ctx):
    ws = test_ctx["ws1"]
    requester, user_token = test_ctx["requester"]
    _, admin_token = test_ctx["ws_admin"]

    # Before request: not in list_workspaces
    resp_before = client.get(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp_before.status_code == 200
    assert not any(w["id"] == str(ws.id) for w in resp_before.json())

    # Request share
    req_resp = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    share_id = req_resp.json()["id"]

    # While pending: still not in list_workspaces
    resp_pending = client.get(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert not any(w["id"] == str(ws.id) for w in resp_pending.json())

    # Approve share
    client.patch(
        f"/api/workspaces/{ws.id}/shares/{share_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "approved"},
    )

    # After approval: visible with is_shared=True and my_role="read"
    resp_approved = client.get(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp_approved.status_code == 200
    shared_entry = next((w for w in resp_approved.json() if w["id"] == str(ws.id)), None)
    assert shared_entry is not None
    assert shared_entry["is_shared"] is True
    assert shared_entry["my_role"] == "read"

    # Detail endpoint also reflects is_shared=True and my_role="read"
    resp_detail = client.get(
        f"/api/workspaces/{ws.id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["is_shared"] is True
    assert detail["my_role"] == "read"


def test_auth_workspace_context_read_only_jailing(test_ctx):
    ws = test_ctx["ws1"]
    requester, user_token = test_ctx["requester"]
    _, admin_token = test_ctx["ws_admin"]
    session = test_ctx["session"]

    # Non-member before share gets 403
    with pytest.raises(Exception) as excinfo:
        auth.get_workspace_context(
            workspace_id=str(ws.id),
            current_user=requester,
            db=session,
        )
    assert "Not a member of this workspace" in str(excinfo.value)

    # Approve share
    req_resp = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    share_id = req_resp.json()["id"]
    client.patch(
        f"/api/workspaces/{ws.id}/shares/{share_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "approved"},
    )

    # Read context succeeds with role="read"
    resolved_ws, role = auth.get_workspace_context(
        workspace_id=str(ws.id),
        current_user=requester,
        db=session,
    )
    assert resolved_ws.id == ws.id
    assert role == "read"

    # Write operations strictly fail with 403
    with pytest.raises(Exception) as exc_write:
        auth.get_workspace_context(
            workspace_id=str(ws.id),
            current_user=requester,
            db=session,
            require_write=True,
        )
    assert "Workspace read role is read-only" in str(exc_write.value)


# ── Lifecycle: Direct Membership Override ─────────────────────────────────────


def test_direct_membership_supersedes_workspace_share(test_ctx):
    ws = test_ctx["ws1"]
    requester, user_token = test_ctx["requester"]
    _, admin_token = test_ctx["ws_admin"]
    session = test_ctx["session"]

    # Create approved share
    req_resp = client.post(
        f"/api/workspaces/{ws.id}/shares",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    share_id = req_resp.json()["id"]
    client.patch(
        f"/api/workspaces/{ws.id}/shares/{share_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "approved"},
    )

    assert session.query(WorkspaceShare).filter_by(id=uuid.UUID(share_id)).first() is not None

    # Admin adds requester as direct member with role="write"
    resp_add_member = client.post(
        f"/api/workspaces/{ws.id}/members",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"email": requester.email, "role": "write"},
    )
    assert resp_add_member.status_code == 200, resp_add_member.text

    # WorkspaceShare record is automatically deleted/superseded
    assert session.query(WorkspaceShare).filter_by(id=uuid.UUID(share_id)).first() is None

    # User now has direct write membership
    resp_list = client.get(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    member_entry = next((w for w in resp_list.json() if w["id"] == str(ws.id)), None)
    assert member_entry is not None
    assert member_entry["is_shared"] is False
    assert member_entry["my_role"] == "write"
