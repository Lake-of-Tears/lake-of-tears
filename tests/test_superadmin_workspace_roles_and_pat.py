from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

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

backend_spec = importlib.util.spec_from_file_location(
    "backend_main_superadmin_pat", repo_root / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(backend_spec)
sys.modules["backend_main_superadmin_pat"] = backend_main
backend_spec.loader.exec_module(backend_main)

ui_spec = importlib.util.spec_from_file_location(
    "ui_main_superadmin_pat", repo_root / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(ui_spec)
sys.modules["ui_main_superadmin_pat"] = ui_main
ui_spec.loader.exec_module(ui_main)

import auth  # noqa: E402
import database  # noqa: E402
from models import (  # noqa: E402
    Base,
    PersonalAccessToken,
    SystemSetting,
    User,
    UserInvitation,
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
    session.query(UserInvitation).delete()
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
def client(db_session):
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


def _create_user(db_session, email: str, role: str = "user") -> User:
    user = User(
        id=uuid.uuid4(),
        email=email.lower(),
        display_name=email.split("@")[0],
        hashed_password=auth.hash_password("Secret123!"),
        role=role,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _create_workspace(db_session, name: str, slug: str) -> Workspace:
    ws = Workspace(id=uuid.uuid4(), name=name, slug=slug, status="active")
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def _login_token(client, email: str) -> str:
    resp = client.post("/api/auth/login", json={"email": email, "password": "Secret123!"})
    assert resp.status_code == 200, resp.text
    token = client.cookies.get(auth.COOKIE_NAME)
    assert token is not None
    return token


# ── Test 1: _workspace_response always returns my_role == "admin" for superadmin ────


def test_workspace_response_superadmin_always_admin(db_session):
    superadmin = _create_user(db_session, "superadmin_test@example.com", role="superadmin")
    ws_with_read_member = _create_workspace(db_session, "Default", "default")
    ws_with_no_member = _create_workspace(db_session, "Analytics", "analytics")

    # Deliberately put a WorkspaceMember with role="read" to simulate the bug
    db_session.add(
        WorkspaceMember(workspace_id=ws_with_read_member.id, user_id=superadmin.id, role="read")
    )
    db_session.commit()

    resp_default = backend_main._workspace_response(ws_with_read_member, superadmin)
    assert resp_default.my_role == "admin", "Superadmin must always have my_role='admin'"

    resp_analytics = backend_main._workspace_response(ws_with_no_member, superadmin)
    assert (
        resp_analytics.my_role == "admin"
    ), "Superadmin without membership must still have my_role='admin'"


def test_workspace_response_standard_user_respects_membership(db_session):
    user = _create_user(db_session, "user_test@example.com", role="user")
    ws_read = _create_workspace(db_session, "Read WS", "read-ws")
    ws_write = _create_workspace(db_session, "Write WS", "write-ws")
    ws_admin = _create_workspace(db_session, "Admin WS", "admin-ws")
    ws_none = _create_workspace(db_session, "None WS", "none-ws")

    db_session.add(WorkspaceMember(workspace_id=ws_read.id, user_id=user.id, role="read"))
    db_session.add(WorkspaceMember(workspace_id=ws_write.id, user_id=user.id, role="write"))
    db_session.add(WorkspaceMember(workspace_id=ws_admin.id, user_id=user.id, role="admin"))
    db_session.commit()

    assert backend_main._workspace_response(ws_read, user).my_role == "read"
    assert backend_main._workspace_response(ws_write, user).my_role == "write"
    assert backend_main._workspace_response(ws_admin, user).my_role == "admin"
    assert backend_main._workspace_response(ws_none, user).my_role is None


# ── Test 2: Superadmin Invitation and Member Provisioning ─────────────────────


def test_superadmin_invitation_creation_and_registration(client, db_session):
    _create_user(db_session, "initial_admin@example.com", role="superadmin")
    jwt_token = _login_token(client, "initial_admin@example.com")
    _create_workspace(db_session, "Default", "default")
    _create_workspace(db_session, "Production", "prod")

    # Invite a second superadmin
    resp = client.post(
        "/api/admin/invitations",
        headers={"Authorization": f"Bearer {jwt_token}"},
        json={
            "email": "new_superadmin@example.com",
            "role": "superadmin",
            "workspace_access": {"default": "read", "prod": "read"},  # Even if sent as read
        },
    )
    assert resp.status_code == 200, resp.text
    invitation_data = resp.json()
    # Verified that backend sanitized workspace_access to "admin" for all entries
    assert invitation_data["workspace_access"]["default"] == "admin"
    assert invitation_data["workspace_access"]["prod"] == "admin"

    # Accept invitation & register
    reg_resp = client.post(
        "/api/auth/register",
        json={
            "email": "new_superadmin@example.com",
            "password": "Secret123!",
            "display_name": "Second Superadmin",
        },
    )
    assert reg_resp.status_code == 200, reg_resp.text
    new_user = db_session.query(User).filter(User.email == "new_superadmin@example.com").first()
    assert new_user is not None
    assert new_user.role == "superadmin"

    # Verify WorkspaceMember records created are role="admin"
    members = db_session.query(WorkspaceMember).filter(WorkspaceMember.user_id == new_user.id).all()
    assert len(members) == 2
    for m in members:
        assert (
            m.role == "admin"
        ), f"Workspace member role for {m.workspace_id} must be 'admin', got '{m.role}'"


# ── Test 3: PAT Creation Permission Ladder Enforcement ─────────────────────────


def test_pat_creation_permission_ladder(client, db_session):
    _create_user(db_session, "pat_superadmin@example.com", role="superadmin")
    user_admin = _create_user(db_session, "pat_admin@example.com", role="user")
    user_write = _create_user(db_session, "pat_write@example.com", role="user")
    user_read = _create_user(db_session, "pat_read@example.com", role="user")
    _create_user(db_session, "pat_none@example.com", role="user")

    ws = _create_workspace(db_session, "Default", "default")
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user_admin.id, role="admin"))
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user_write.id, role="write"))
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user_read.id, role="read"))
    db_session.commit()

    token_superadmin = _login_token(client, "pat_superadmin@example.com")
    token_admin = _login_token(client, "pat_admin@example.com")
    token_write = _login_token(client, "pat_write@example.com")
    token_read = _login_token(client, "pat_read@example.com")
    token_none = _login_token(client, "pat_none@example.com")

    # 1. Non-member cannot grant token access (403)
    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {token_none}"},
        json={"name": "None Token", "workspace_access": {str(ws.id): "read"}},
    )
    assert resp.status_code == 403

    # 2. READ user can grant read
    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {token_read}"},
        json={"name": "Read Token", "workspace_access": {str(ws.id): "read"}},
    )
    assert resp.status_code == 201

    # 3. READ user cannot grant write or admin (400)
    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {token_read}"},
        json={"name": "Read Escalate Write", "workspace_access": {str(ws.id): "write"}},
    )
    assert resp.status_code == 400
    assert "your permission level in this workspace is only 'read'" in resp.json()["detail"]

    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {token_read}"},
        json={"name": "Read Escalate Admin", "workspace_access": {str(ws.id): "admin"}},
    )
    assert resp.status_code == 400

    # 4. WRITE user can grant read and write
    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {token_write}"},
        json={"name": "Write Token Read", "workspace_access": {str(ws.id): "read"}},
    )
    assert resp.status_code == 201

    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {token_write}"},
        json={"name": "Write Token Write", "workspace_access": {str(ws.id): "write"}},
    )
    assert resp.status_code == 201

    # 5. WRITE user cannot grant admin (400)
    resp = client.post(
        "/api/auth/tokens",
        headers={"Authorization": f"Bearer {token_write}"},
        json={"name": "Write Escalate Admin", "workspace_access": {str(ws.id): "admin"}},
    )
    assert resp.status_code == 400
    assert "your permission level in this workspace is only 'write'" in resp.json()["detail"]

    # 6. ADMIN user can grant read, write, and admin
    for role in ("read", "write", "admin"):
        resp = client.post(
            "/api/auth/tokens",
            headers={"Authorization": f"Bearer {token_admin}"},
            json={"name": f"Admin Token {role}", "workspace_access": {str(ws.id): role}},
        )
        assert resp.status_code == 201, resp.text

    # 7. SUPERADMIN can grant admin, write, read on any workspace (even without WorkspaceMember)
    for role in ("read", "write", "admin"):
        resp = client.post(
            "/api/auth/tokens",
            headers={"Authorization": f"Bearer {token_superadmin}"},
            json={"name": f"Superadmin Token {role}", "workspace_access": {str(ws.id): role}},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["workspace_access"][str(ws.id)] == role


# ── Test 4: UI Admin Invitation Enforces Admin Role for Superadmin ─────────────


def test_ui_admin_invitation_enforces_admin_role_for_superadmin():
    client = TestClient(ui_main.app, follow_redirects=False)
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "email": "inviter_superadmin@example.com",
            "role": "superadmin",
            "jti": str(uuid.uuid4()),
        },
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    with patch.object(ui_main, "_backend_post_with_error") as mock_post:
        mock_post.return_value = (
            {"id": str(uuid.uuid4()), "email": "invited_sa@example.com"},
            None,
        )
        resp = client.post(
            "/settings/admin/invitations",
            data={
                "email": "invited_sa@example.com",
                "role": "superadmin",
                "default_role": "read",  # Deliberately submit default_role="read"
                "ws_role_analytics": "read",
            },
            cookies={"lake_token": token},
        )
        assert resp.status_code == 302
        mock_post.assert_called_once()
        payload = mock_post.call_args[0][2]
        assert payload["role"] == "superadmin"
        assert (
            payload["workspace_access"]["default"] == "admin"
        ), "Superadmin invite must enforce 'admin' for default"
        assert (
            payload["workspace_access"]["analytics"] == "admin"
        ), "Superadmin invite must enforce 'admin' for other workspaces"


# ── Test 5: UI Template Renders Dynamic Badge & Excludes Misleading Copy ───────


def test_ui_settings_account_renders_dynamic_pat_badge():
    client = TestClient(ui_main.app, follow_redirects=False)
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "email": "sa_account@example.com",
            "role": "superadmin",
            "jti": str(uuid.uuid4()),
        },
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    workspaces = [
        {"id": "ws-default-uuid", "name": "Default", "slug": "default", "my_role": "admin"}
    ]

    with patch.object(ui_main, "_backend_get") as mock_get:
        mock_get.side_effect = (
            lambda path, tok: {"email": "sa_account@example.com", "display_name": "SA User"}
            if "/api/auth/me" in path
            else []
            if "/api/auth/tokens" in path
            else workspaces
            if "/api/workspaces" in path
            else []
        )
        resp = client.get("/settings/account", cookies={"lake_token": token})
        assert resp.status_code == 200
        html = resp.text

        # 1. Misleading 'Default: No Access' copy MUST NOT exist in HTML
        assert "Default: No Access" not in html

        # 2. Dynamic policy badge with id='pat-policy-badge' must exist
        assert 'id="pat-policy-badge"' in html
        assert "No Workspaces Granted" in html

        # 3. Superadmin workspace option displays 'Your role: ADMIN'
        assert "Your role: ADMIN" in html


# ── Test 6: Playwright E2E Verification of PAT Modal Interactions ─────────────


try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="Playwright is not available")
def test_e2e_pat_modal_superadmin_and_read_user_playwright():
    client = TestClient(ui_main.app, follow_redirects=False)
    sa_token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "sa@lake.local", "role": "superadmin"},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )

    sa_workspaces = [
        {"id": "ws-1", "name": "Default", "slug": "default", "my_role": "admin"},
        {"id": "ws-2", "name": "Analytics", "slug": "analytics", "my_role": "admin"},
    ]

    with patch.object(ui_main, "_backend_get") as mock_get:
        mock_get.side_effect = (
            lambda path, tok: {"email": "sa@lake.local", "display_name": "Super Admin"}
            if "/api/auth/me" in path
            else []
            if "/api/auth/tokens" in path
            else sa_workspaces
            if "/api/workspaces" in path
            else []
        )
        resp = client.get("/settings/account", cookies={"lake_token": sa_token})
        assert resp.status_code == 200
        html = resp.text

    origin = "http://lake.test"

    def handle(route):
        url = route.request.url
        path = url[len(origin) :].split("?", 1)[0]
        if not url.startswith(origin):
            return route.abort()
        if path == "/settings/account":
            return route.fulfill(status=200, content_type="text/html", body=html)
        return route.fulfill(status=200, content_type="application/javascript", body="")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/*", handle)
            page.goto(f"{origin}/settings/account")

            # 1. Open the PAT Modal
            page.evaluate("openPatModal()")
            page.wait_for_selector("#pat-modal:not(.hidden)")

            # 2. Check initial badge state: 'No Workspaces Granted'
            badge = page.locator("#pat-policy-badge")
            assert badge.inner_text().strip() == "No Workspaces Granted"

            # 3. Verify select option shows 'Your role: ADMIN'
            opt = page.locator("#pat-ws-select option[value='ws-1']")
            assert "Your role: ADMIN" in opt.inner_text()
            assert opt.get_attribute("data-my-role") == "admin"

            # 4. Add the 'Default' workspace
            page.select_option("#pat-ws-select", "ws-1")
            page.evaluate("addPatWorkspace()")

            # 5. Badge must update dynamically to '1 Workspace Configured'
            assert badge.inner_text().strip() == "1 Workspace Configured"
            assert "text-emerald-400" in (badge.get_attribute("class") or "")

            # 6. Verify row contains 'Your limit: admin' and Token Access dropdown has READ, WRITE, ADMIN
            row = page.locator("#pat-selected-workspaces-container > div")
            assert "Your limit: admin" in row.inner_text()

            token_role_options = row.locator("select option").all_inner_texts()
            assert token_role_options == ["READ", "WRITE", "ADMIN"]

            # 7. Add a second workspace ('Analytics')
            page.select_option("#pat-ws-select", "ws-2")
            page.evaluate("addPatWorkspace()")

            # Badge updates to '2 Workspaces Configured'
            assert badge.inner_text().strip() == "2 Workspaces Configured"

            # 8. Remove one workspace
            row.locator("button[title='Remove']").first.click()
            assert badge.inner_text().strip() == "1 Workspace Configured"

            # Remove the last workspace -> reverts to 'No Workspaces Granted'
            page.locator("#pat-selected-workspaces-container button[title='Remove']").click()
            assert badge.inner_text().strip() == "No Workspaces Granted"

        finally:
            browser.close()
