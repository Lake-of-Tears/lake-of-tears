from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
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

# Explicitly load backend_main to prevent module collisions
backend_spec = importlib.util.spec_from_file_location(
    "backend_main_pat_test", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_pat_test"] = backend_main
backend_spec.loader.exec_module(backend_main)

# Explicitly load ui_main
ui_spec = importlib.util.spec_from_file_location(
    "ui_main_pat_test", repo_root / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_pat_test"] = ui_main
ui_spec.loader.exec_module(ui_main)

import auth  # noqa: E402
import database  # noqa: E402
from models import (  # noqa: E402
    Base,
    PersonalAccessToken,
    SystemSetting,
    User,
    UserSession,
    Workspace,
    WorkspaceMember,
)

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(engine)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def setup_db():
    session = TestingSessionLocal()
    session.query(PersonalAccessToken).delete()
    session.query(WorkspaceMember).delete()
    session.query(Workspace).delete()
    session.query(UserSession).delete()
    session.query(User).delete()
    session.query(SystemSetting).delete()
    session.add(SystemSetting(key="session_inactivity_timeout_minutes", value=60))
    session.add(SystemSetting(key="session_max_hours", value=24))
    session.commit()
    session.close()


@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    backend_main.app.dependency_overrides[database.get_db] = override_get_db
    backend_main.app.dependency_overrides.pop(auth.get_current_user, None)
    c = TestClient(backend_main.app)
    yield c
    backend_main.app.dependency_overrides.clear()


@pytest.fixture
def ui_client():
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    ui_main.app.dependency_overrides[database.get_db] = override_get_db
    c = TestClient(ui_main.app)
    yield c
    ui_main.app.dependency_overrides.clear()


def _create_user(
    db, email: str = "test@example.com", role: str = "user", password: str = "Secret123!"
):
    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        hashed_password=auth.hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _create_workspace(db, name: str = "Test Workspace", slug: str = "test-ws"):
    ws = Workspace(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        status="active",
    )
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _login_token(client, email: str = "test@example.com", password: str = "Secret123!"):
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    token = client.cookies.get(auth.COOKIE_NAME)
    assert token is not None
    return token


def test_create_pat_success(client, db_session):
    user = _create_user(db_session, "pat_user@example.com")
    jwt_token = _login_token(client, "pat_user@example.com")
    ws = _create_workspace(db_session, "WS 1", "ws-1")
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="write"))
    db_session.commit()

    # Create PAT with explicit workspace and scopes
    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={
            "name": "Angler CLI - Laptop",
            "lifetime_days": 30,
            "workspace_ids": [str(ws.id)],
            "scopes": ["read", "write"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()

    assert data["name"] == "Angler CLI - Laptop"
    assert data["token"].startswith("lot_pat_")
    assert len(data["token"]) > 32
    assert data["token_prefix"] == data["token"][:16]
    assert data["workspace_ids"] == [str(ws.id)]
    assert data["scopes"] == ["read", "write"]
    assert data["expires_at"] is not None

    # Check database: secret raw token is never stored directly
    pat_in_db = (
        db_session.query(PersonalAccessToken)
        .filter(PersonalAccessToken.id == uuid.UUID(data["id"]))
        .first()
    )
    assert pat_in_db is not None
    assert pat_in_db.token_hash != data["token"]
    assert pat_in_db.is_revoked is False


def test_create_pat_fails_without_workspaces(client, db_session):
    user = _create_user(db_session, "no_ws@example.com")
    jwt_token = _login_token(client, "no_ws@example.com")

    # Creating without workspace_ids or workspace_id fails (Default: No Access)
    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"name": "No WS Token", "scopes": ["read"]},
    )
    assert resp.status_code in (400, 422)
    assert "workspace" in resp.text.lower()


def test_create_pat_fails_without_scopes(client, db_session):
    user = _create_user(db_session, "no_scopes@example.com")
    jwt_token = _login_token(client, "no_scopes@example.com")
    ws = _create_workspace(db_session, "WS 1", "ws-1")
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="write"))
    db_session.commit()

    # Creating with empty scopes fails (Default: No Access)
    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"name": "No Scope Token", "workspace_ids": [str(ws.id)], "scopes": []},
    )
    assert resp.status_code in (400, 422)
    assert "scope" in resp.text.lower()


def test_authenticate_with_pat(client, db_session):
    user = _create_user(db_session, "auth_user@example.com")
    jwt_token = _login_token(client, "auth_user@example.com")
    ws = _create_workspace(db_session, "WS 1", "ws-1")
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="write"))
    db_session.commit()

    # Create token
    create_resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"name": "Test Key", "lifetime_days": 7, "workspace_ids": [str(ws.id)], "scopes": ["read"]},
    )
    assert create_resp.status_code == 201
    pat_secret = create_resp.json()["token"]

    # Call /api/auth/me with the PAT
    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {pat_secret}"})
    assert me_resp.status_code == 200
    me_data = me_resp.json()
    assert me_data["email"] == "auth_user@example.com"
    assert me_data["id"] == str(user.id)


def test_pat_expiration(client, db_session):
    user = _create_user(db_session, "expired_pat@example.com")
    ws = _create_workspace(db_session, "WS 1", "ws-1")
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="write"))
    db_session.commit()

    pat, raw_token = auth.create_personal_access_token(
        db_session,
        user_id=user.id,
        name="Expired Token",
        lifetime_days=1,
        workspace_ids=[ws.id],
        scopes=["read"],
    )

    # Force expiration in past
    pat.expires_at = datetime.now(UTC) - timedelta(days=2)
    db_session.commit()

    # Authenticate with expired token
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_pat_revocation(client, db_session):
    user = _create_user(db_session, "revoke_pat@example.com")
    jwt_token = _login_token(client, "revoke_pat@example.com")
    ws = _create_workspace(db_session, "WS 1", "ws-1")
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="write"))
    db_session.commit()

    # Create token
    create_resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"name": "To Revoke", "workspace_ids": [str(ws.id)], "scopes": ["read"]},
    )
    assert create_resp.status_code == 201
    token_id = create_resp.json()["id"]
    raw_token = create_resp.json()["token"]

    # Verify active
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
    assert resp.status_code == 200

    # Revoke via DELETE /api/auth/tokens/{token_id}
    del_resp = client.delete(
        f"/api/auth/tokens/{token_id}",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["ok"] is True

    # Subsequent auth fails
    resp_after = client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
    assert resp_after.status_code == 401


def test_list_tokens_filtering(client, db_session):
    user = _create_user(db_session, "list_pat@example.com")
    jwt_token = _login_token(client, "list_pat@example.com")
    ws = _create_workspace(db_session, "WS 1", "ws-1")
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="write"))
    db_session.commit()

    # Create 2 tokens
    r1 = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"name": "Active 1", "workspace_ids": [str(ws.id)], "scopes": ["read"]},
    )
    r2 = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"name": "Active 2", "workspace_ids": [str(ws.id)], "scopes": ["read"]},
    )
    t1_id = r1.json()["id"]
    t2_id = r2.json()["id"]

    # Revoke token 1
    client.delete(f"/api/auth/tokens/{t1_id}", headers={"Authorization": f"Bearer {jwt_token}"})

    # GET tokens (default: active only)
    list_active = client.get("/api/auth/tokens", headers={"Authorization": f"Bearer {jwt_token}"})
    assert list_active.status_code == 200
    active_items = list_active.json()
    assert len(active_items) == 1
    assert active_items[0]["id"] == t2_id

    # GET tokens with include_revoked=true
    list_all = client.get("/api/auth/tokens?include_revoked=true", headers={"Authorization": f"Bearer {jwt_token}"})
    assert list_all.status_code == 200
    all_items = list_all.json()
    assert len(all_items) == 2


def test_pat_multi_workspace_access(client, db_session):
    user = _create_user(db_session, "multi_ws@example.com")
    ws1 = _create_workspace(db_session, "Workspace 1", "ws-1")
    ws2 = _create_workspace(db_session, "Workspace 2", "ws-2")
    ws3 = _create_workspace(db_session, "Workspace 3", "ws-3")

    # Add user to all three workspaces
    db_session.add(WorkspaceMember(workspace_id=ws1.id, user_id=user.id, role="write"))
    db_session.add(WorkspaceMember(workspace_id=ws2.id, user_id=user.id, role="write"))
    db_session.add(WorkspaceMember(workspace_id=ws3.id, user_id=user.id, role="write"))
    db_session.commit()

    # Create token restricted exclusively to ws1 and ws2
    pat, raw_token = auth.create_personal_access_token(
        db_session,
        user_id=user.id,
        name="WS1 and WS2 Token",
        workspace_ids=[ws1.id, ws2.id],
        scopes=["read", "write"],
    )

    # Calling with ws1 succeeds
    resp_ws1 = client.get(
        f"/api/workspaces/{ws1.id}/catalogs",
        headers={"Authorization": f"Bearer {raw_token}", "X-Workspace-Id": str(ws1.id)},
    )
    assert resp_ws1.status_code == 200

    # Calling with ws2 succeeds
    resp_ws2 = client.get(
        f"/api/workspaces/{ws2.id}/catalogs",
        headers={"Authorization": f"Bearer {raw_token}", "X-Workspace-Id": str(ws2.id)},
    )
    assert resp_ws2.status_code == 200

    # Calling with ws3 is rejected with 403 Forbidden
    resp_ws3 = client.get(
        f"/api/workspaces/{ws3.id}/catalogs",
        headers={"Authorization": f"Bearer {raw_token}", "X-Workspace-Id": str(ws3.id)},
    )
    assert resp_ws3.status_code == 403


def test_pat_scopes_enforcement(client, db_session):
    user = _create_user(db_session, "scopes_pat@example.com")
    ws = _create_workspace(db_session, "Scope WS", "scope-ws")
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="admin"))
    db_session.commit()

    # Create read-only PAT
    pat, raw_token = auth.create_personal_access_token(
        db_session,
        user_id=user.id,
        name="Read Only Token",
        workspace_ids=[ws.id],
        scopes=["read"],
    )

    # Read operation succeeds
    resp_read = client.get(
        f"/api/workspaces/{ws.id}/catalogs",
        headers={"Authorization": f"Bearer {raw_token}", "X-Workspace-Id": str(ws.id)},
    )
    assert resp_read.status_code == 200

    # Write operation (creating a catalog) requires write scope, rejected with 403
    resp_write = client.post(
        "/api/catalogs",
        headers={"Authorization": f"Bearer {raw_token}", "X-Workspace-Id": str(ws.id)},
        json={"name": "Forbidden Catalog", "workspace_id": str(ws.id)},
    )
    assert resp_write.status_code == 403
    assert "write scope" in resp_write.json()["detail"].lower()


def test_max_active_tokens_limit(client, db_session):
    user = _create_user(db_session, "cap_user@example.com")
    jwt_token = _login_token(client, "cap_user@example.com")
    ws = _create_workspace(db_session, "Cap WS", "cap-ws")
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="write"))
    db_session.commit()

    # Create 50 tokens
    for i in range(50):
        pat = PersonalAccessToken(
            user_id=user.id,
            name=f"Token {i}",
            token_prefix=f"lot_pat_{i:04d}",
            token_hash=f"hash_{i:04d}" + "0" * 55,
            workspace_ids=[str(ws.id)],
            scopes=["read"],
            created_at=datetime.now(UTC),
            is_revoked=False,
        )
        db_session.add(pat)
    db_session.commit()

    # Creating 51st token fails with 400
    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={"name": "51st Token", "workspace_ids": [str(ws.id)], "scopes": ["read"]},
    )
    assert resp.status_code == 400
    assert "limit" in resp.json()["detail"].lower()


def test_last_used_at_throttling(client, db_session):
    user = _create_user(db_session, "throttle@example.com")
    ws = _create_workspace(db_session, "Throttle WS", "throttle-ws")
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="write"))
    db_session.commit()

    pat, raw_token = auth.create_personal_access_token(
        db_session,
        user_id=user.id,
        name="Throttled Token",
        workspace_ids=[ws.id],
        scopes=["read"],
    )
    assert pat.last_used_at is None

    # First request sets last_used_at
    r1 = client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
    assert r1.status_code == 200
    db_session.refresh(pat)
    first_used = pat.last_used_at
    assert first_used is not None

    # Second request immediately afterwards does NOT modify last_used_at (throttled to 60s)
    r2 = client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"})
    assert r2.status_code == 200
    db_session.refresh(pat)
    assert pat.last_used_at == first_used


def test_ui_settings_account_renders_tokens():
    client = TestClient(ui_main.app, follow_redirects=False)
    user_id = str(uuid.uuid4())
    token = jwt.encode(
        {"sub": user_id, "email": "ui_pat@example.com", "role": "user", "jti": str(uuid.uuid4())},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    fake_tokens = [
        {
            "id": str(uuid.uuid4()),
            "name": "Angler CLI Token",
            "token_prefix": "lot_pat_12345678",
            "workspace_ids": ["ws-1"],
            "workspace_names": ["Engineering Workspace"],
            "scopes": ["read", "write"],
            "created_at": "2026-11-01T12:00:00Z",
            "expires_at": "2026-12-01T12:00:00Z",
            "last_used_at": None,
            "is_revoked": False,
            "revoked_at": None,
        }
    ]

    from unittest.mock import patch

    with patch.object(ui_main, "_backend_get") as mock_get:
        mock_get.side_effect = (
            lambda path, tok: fake_tokens
            if "/api/auth/tokens" in path
            else {"email": "ui_pat@example.com", "display_name": "UI User"}
            if "/api/auth/me" in path
            else [{"id": "ws-1", "name": "Engineering Workspace", "slug": "engineering"}]
            if "/api/workspaces" in path
            else []
        )
        resp = client.get("/settings/account", cookies={"lake_token": token})
        assert resp.status_code == 200
        assert "Personal Access Tokens" in resp.text
        assert "Angler CLI Token" in resp.text
        assert "lot_pat_12345678" in resp.text
        assert "Engineering Workspace" in resp.text
        assert "Generate New Token" in resp.text
        assert "Permitted Workspaces" in resp.text


def test_ui_settings_account_create_and_revoke_token():
    client = TestClient(ui_main.app, follow_redirects=False)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "ui_pat@example.com", "role": "user", "jti": str(uuid.uuid4())},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    from unittest.mock import patch

    # Test Create Token via form with workspace_ids and scopes
    with patch.object(ui_main, "_backend_post_with_error") as mock_post:
        mock_post.return_value = (
            {"id": str(uuid.uuid4()), "name": "New Key", "token": "lot_pat_secretkey123"},
            None,
        )
        resp = client.post(
            "/settings/account/tokens",
            data={
                "name": "New Key",
                "lifetime_days": "30",
                "workspace_ids": ["ws-123"],
                "scopes": ["read", "write"],
            },
            cookies={"lake_token": token},
        )
        assert resp.status_code == 302
        assert "saved=token_created" in resp.headers["location"]
        assert "new_token=lot_pat_secretkey123" in resp.headers["location"]

    # Test Revoke Token via form
    tok_id = str(uuid.uuid4())
    with patch.object(ui_main, "_backend_delete") as mock_del:
        mock_del.return_value = True
        resp = client.post(
            f"/settings/account/tokens/{tok_id}/revoke",
            cookies={"lake_token": token},
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/settings/account?saved=token_revoked#tokens"
        mock_del.assert_called_once_with(f"/api/auth/tokens/{tok_id}", token)
