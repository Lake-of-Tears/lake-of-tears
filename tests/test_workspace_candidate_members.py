from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
from starlette.testclient import TestClient

backend_dir = str(Path(__file__).parents[1] / "backend")
ui_dir = str(Path(__file__).parents[1] / "ui")
for d in (backend_dir, ui_dir):
    if d not in sys.path:
        sys.path.insert(0, d)

import auth  # noqa: E402
import database  # noqa: E402
from models import User, Workspace, WorkspaceMember  # noqa: E402

# Load backend app
spec_backend = importlib.util.spec_from_file_location(
    "backend_main_candidates", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec_backend)
sys.modules["backend_main_candidates"] = backend_main
spec_backend.loader.exec_module(backend_main)
backend_client = TestClient(backend_main.app)

# Load ui app
spec_ui = importlib.util.spec_from_file_location(
    "ui_main_candidates", Path(__file__).parents[1] / "ui" / "main.py"
)
ui_main = importlib.util.module_from_spec(spec_ui)
sys.modules["ui_main_candidates"] = ui_main
spec_ui.loader.exec_module(ui_main)
ui_client = TestClient(ui_main.app)


def _make_user(
    email: str,
    display_name: str | None = None,
    role: str = "user",
    is_active: bool = True,
) -> tuple[User, str]:
    uid = uuid.uuid4()
    u = User(
        id=uid,
        email=email,
        display_name=display_name,
        role=role,
        is_active=is_active,
    )
    token = jwt.encode(
        {"sub": str(uid), "email": u.email, "role": u.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    return u, token


def test_candidate_members_workspace_not_found():
    admin_user, admin_token = _make_user("admin@lake.local", role="user")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: admin_user
    try:
        resp = backend_client.get(
            f"/api/workspaces/{uuid.uuid4()}/candidate-members",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 404
        assert "Workspace not found" in resp.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()


def test_candidate_members_requires_admin():
    ws_id = uuid.uuid4()
    regular_user, regular_token = _make_user("user@lake.local", role="user")
    ws = Workspace(id=ws_id, name="Test WS", slug="test-ws", status="active")
    ws.members = [WorkspaceMember(workspace_id=ws_id, user_id=regular_user.id, role="read")]

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = ws

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: regular_user
    try:
        resp = backend_client.get(
            f"/api/workspaces/{ws_id}/candidate-members",
            headers={"Authorization": f"Bearer {regular_token}"},
        )
        assert resp.status_code == 403
        assert "Workspace admin access required" in resp.json()["detail"]
    finally:
        backend_main.app.dependency_overrides.clear()


def test_candidate_members_workspace_admin_success():
    ws_id = uuid.uuid4()
    admin_user, admin_token = _make_user("wsadmin@lake.local", display_name="WS Admin")
    candidate1, _ = _make_user("alice@lake.local", display_name="Alice Candidate")
    candidate2, _ = _make_user("bob@lake.local", display_name="Bob Candidate")

    ws = Workspace(id=ws_id, name="Analytics", slug="analytics", status="active")
    ws.members = [WorkspaceMember(workspace_id=ws_id, user_id=admin_user.id, role="admin")]

    mock_db = MagicMock()
    ws_query_mock = MagicMock()
    ws_query_mock.filter.return_value.first.return_value = ws

    user_query_mock = MagicMock()
    user_query_mock.filter.return_value = user_query_mock
    user_query_mock.order_by.return_value = user_query_mock
    user_query_mock.limit.return_value = user_query_mock
    user_query_mock.all.return_value = [candidate1, candidate2]

    def _query_side_effect(model):
        if model == Workspace:
            return ws_query_mock
        return user_query_mock

    mock_db.query.side_effect = _query_side_effect

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: admin_user
    try:
        resp = backend_client.get(
            f"/api/workspaces/{ws_id}/candidate-members",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["email"] == "alice@lake.local"
        assert data[0]["display_name"] == "Alice Candidate"
        assert data[1]["email"] == "bob@lake.local"
    finally:
        backend_main.app.dependency_overrides.clear()


def test_candidate_members_superadmin_allowed():
    ws_id = uuid.uuid4()
    superadmin, sa_token = _make_user("super@lake.local", role="superadmin")
    candidate, _ = _make_user("candidate@lake.local", display_name="Candidate")

    ws = Workspace(id=ws_id, name="Test", slug="test", status="active")
    ws.members = []

    mock_db = MagicMock()
    ws_query_mock = MagicMock()
    ws_query_mock.filter.return_value.first.return_value = ws

    user_query_mock = MagicMock()
    user_query_mock.filter.return_value = user_query_mock
    user_query_mock.order_by.return_value = user_query_mock
    user_query_mock.limit.return_value = user_query_mock
    user_query_mock.all.return_value = [candidate]

    def _query_side_effect(model):
        if model == Workspace:
            return ws_query_mock
        return user_query_mock

    mock_db.query.side_effect = _query_side_effect

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: superadmin
    try:
        resp = backend_client.get(
            f"/api/workspaces/{ws_id}/candidate-members?q=cand",
            headers={"Authorization": f"Bearer {sa_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["email"] == "candidate@lake.local"
    finally:
        backend_main.app.dependency_overrides.clear()


def test_ui_candidate_members_unauthenticated():
    resp = ui_client.get("/api/workspaces/ws-123/candidate-members")
    assert resp.status_code == 401


def test_ui_candidate_members_proxies():
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "email": "test@lake.local", "role": "admin"},
        ui_main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    with patch.object(ui_main, "_backend_get_async", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [
            {"id": str(uuid.uuid4()), "email": "cand@lake.local", "display_name": "Cand User"}
        ]
        ui_client.cookies.set("lake_token", token)
        try:
            resp = ui_client.get(
                "/api/workspaces/ws-123/candidate-members?q=cand&limit=10",
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["email"] == "cand@lake.local"
            mock_get.assert_called_once()
            call_path = mock_get.call_args[0][0]
            assert "/api/workspaces/ws-123/candidate-members" in call_path
            assert "q=cand" in call_path
        finally:
            ui_client.cookies.clear()
