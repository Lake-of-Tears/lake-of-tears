import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from starlette.testclient import TestClient

# Ensure backend directory is in sys.path
backend_dir = str(Path(__file__).parents[1] / "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)


import auth  # noqa: E402
import database  # noqa: E402
import notebook_service  # noqa: E402
from models import User, Workspace, WorkspaceMember  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_main", Path(__file__).parents[1] / "backend" / "main.py"
)
backend_main = importlib.util.module_from_spec(spec)
sys.modules["backend_main"] = backend_main
spec.loader.exec_module(backend_main)

client = TestClient(backend_main.app)


# ── Service Layer Tests: Sanitization & Helpers ───────────────────────────────


def test_sanitize_notebook_path_valid():
    assert notebook_service.sanitize_notebook_path("analysis.ipynb") == "analysis.ipynb"
    assert notebook_service.sanitize_notebook_path("folder/sub/nb.ipynb") == "folder/sub/nb.ipynb"
    assert notebook_service.sanitize_notebook_path("/leading/slash/") == "leading/slash"
    assert notebook_service.sanitize_notebook_path("") == ""


def test_sanitize_notebook_path_traversal_blocked():
    with pytest.raises(ValueError, match="Invalid path traversal segment"):
        notebook_service.sanitize_notebook_path("../secret.py")

    with pytest.raises(ValueError, match="Invalid path traversal segment"):
        notebook_service.sanitize_notebook_path("folder/../../etc/passwd")

    with pytest.raises(ValueError, match="Invalid path traversal segment"):
        notebook_service.sanitize_notebook_path("./relative.ipynb")


def test_sanitize_notebook_path_invalid_characters_blocked():
    for char in ["<", ">", ":", '"', "|", "?", "*", "\x00"]:
        with pytest.raises(ValueError, match="Invalid characters in path segment"):
            notebook_service.sanitize_notebook_path(f"notebook{char}.ipynb")


def test_get_workspace_dir():
    assert notebook_service.get_workspace_dir("default") == "workspaces/default"
    assert (
        notebook_service.get_workspace_dir("marketing", "eda.ipynb")
        == "workspaces/marketing/eda.ipynb"
    )
    assert (
        notebook_service.get_workspace_dir("eng", "sub/folder/nb.ipynb")
        == "workspaces/eng/sub/folder/nb.ipynb"
    )


def test_create_empty_notebook_dict():
    nb = notebook_service.create_empty_notebook_dict(title="Test Analysis")
    assert nb["nbformat"] == 4
    assert len(nb["cells"]) == 2
    assert nb["cells"][0]["cell_type"] == "markdown"
    assert "# Test Analysis" in nb["cells"][0]["source"][0]
    assert nb["cells"][1]["cell_type"] == "code"
    assert any("duckdb" in line for line in nb["cells"][1]["source"])


# ── Backend Auth & Workspace RBAC Helpers ─────────────────────────────────────


def _generate_test_user_and_token(role: str = "member", global_role: str = "user"):
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"{role}@lake.local",
        display_name=role.title(),
        role=global_role,
        is_active=True,
    )
    token = jwt.encode(
        {"sub": str(user_id), "email": user.email, "role": user.role},
        auth.SECRET_KEY,
        algorithm="HS256",
    )
    ws_id = uuid.uuid4()
    workspace = Workspace(
        id=ws_id,
        name="Test Workspace",
        slug="test-ws",
        status="active",
    )
    member = WorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        user_id=user_id,
        role=role,
    )
    return user, workspace, member, token


# ── Backend REST Endpoints Tests ──────────────────────────────────────────────


def test_backend_notebooks_unauthenticated_blocked():
    backend_main.app.dependency_overrides.clear()
    res = client.get("/api/v1/notebooks/files")
    assert res.status_code == 401
    assert "Not authenticated" in res.text


def test_backend_notebooks_viewer_can_read():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with (
            patch(
                "notebooks_router.get_notebook_workspace_and_role",
                return_value=(workspace, "viewer"),
            ),
            patch.object(
                notebook_service,
                "list_workspace_notebooks",
                new_callable=AsyncMock,
                return_value={
                    "items": [{"name": "report.ipynb", "type": "notebook"}],
                    "workspace": workspace.slug,
                },
            ),
        ):
            # 1. Test via Bearer token (CLI format)
            res_bearer = client.get(
                "/api/v1/notebooks/files",
                headers={"Authorization": f"Bearer {token}"},
                params={"workspace_slug": workspace.slug},
            )
            assert res_bearer.status_code == 200
            assert len(res_bearer.json()["items"]) == 1
            assert res_bearer.json()["items"][0]["name"] == "report.ipynb"

            # 2. Test via Cookie (UI format)
            res_cookie = client.get(
                "/api/v1/notebooks/contents",
                cookies={"lake_token": token, "lake_active_workspace": str(workspace.id)},
            )
            assert res_cookie.status_code == 200
    finally:
        backend_main.app.dependency_overrides.clear()


def test_backend_notebooks_viewer_cannot_modify_or_execute():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with patch(
            "notebooks_router.get_notebook_workspace_and_role",
            side_effect=backend_main.HTTPException(
                403, "Workspace viewer role is read-only. Cannot modify or execute notebooks."
            ),
        ):
            headers = {"Authorization": f"Bearer {token}"}

            # 1. Create item -> 403
            r_create = client.post(
                "/api/v1/notebooks/create",
                json={"name": "forbidden.ipynb", "type": "notebook"},
                headers=headers,
            )
            assert r_create.status_code == 403
            assert "viewer role is read-only" in r_create.text

            # 2. Save notebook -> 403
            r_save = client.put(
                "/api/v1/notebooks/test.ipynb",
                json={"content": {}},
                headers=headers,
            )
            assert r_save.status_code == 403

            # 3. Delete item -> 403
            r_delete = client.delete(
                "/api/v1/notebooks/test.ipynb",
                headers=headers,
            )
            assert r_delete.status_code == 403

            # 4. Rename item -> 403
            r_rename = client.patch(
                "/api/v1/notebooks/test.ipynb",
                json={"new_path": "new_test.ipynb"},
                headers=headers,
            )
            assert r_rename.status_code == 403

            # 5. Start session -> 403
            r_sess = client.post(
                "/api/v1/notebooks/sessions",
                json={"path": "test.ipynb"},
                headers=headers,
            )
            assert r_sess.status_code == 403

            # 6. Interrupt kernel -> 403
            r_interrupt = client.post(
                "/api/v1/notebooks/kernels/k1/interrupt",
                headers=headers,
            )
            assert r_interrupt.status_code == 403

            # 7. Restart kernel -> 403
            r_restart = client.post(
                "/api/v1/notebooks/kernels/k1/restart",
                headers=headers,
            )
            assert r_restart.status_code == 403
    finally:
        backend_main.app.dependency_overrides.clear()


def test_backend_notebooks_editor_can_perform_crud_and_kernels():
    user, workspace, member, token = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with (
            patch(
                "notebooks_router.get_notebook_workspace_and_role",
                return_value=(workspace, "editor"),
            ),
            patch.object(
                notebook_service,
                "create_notebook",
                new_callable=AsyncMock,
                return_value=(201, {"name": "new.ipynb", "path": "new.ipynb"}),
            ),
            patch.object(
                notebook_service,
                "save_notebook",
                new_callable=AsyncMock,
                return_value=(200, {"status": "saved"}),
            ),
            patch.object(
                notebook_service,
                "delete_item",
                new_callable=AsyncMock,
                return_value=(204, {}),
            ),
            patch.object(
                notebook_service,
                "rename_item",
                new_callable=AsyncMock,
                return_value=(200, {"path": "renamed.ipynb"}),
            ),
            patch.object(
                notebook_service,
                "get_or_create_session",
                new_callable=AsyncMock,
                return_value=(200, {"id": "sess-1", "kernel": {"id": "k-1"}}),
            ),
            patch.object(
                notebook_service,
                "restart_kernel",
                new_callable=AsyncMock,
                return_value=(200, {}),
            ),
            patch.object(
                notebook_service,
                "interrupt_kernel",
                new_callable=AsyncMock,
                return_value=(200, {}),
            ),
        ):
            headers = {"Authorization": f"Bearer {token}"}

            # Create
            r1 = client.post(
                "/api/v1/notebooks/create", json={"name": "new.ipynb"}, headers=headers
            )
            assert r1.status_code == 201

            # Save
            r2 = client.put("/api/v1/notebooks/new.ipynb", json={"content": {}}, headers=headers)
            assert r2.status_code == 200

            # Rename
            r3 = client.patch(
                "/api/v1/notebooks/new.ipynb", json={"new_path": "renamed.ipynb"}, headers=headers
            )
            assert r3.status_code == 200

            # Session
            r4 = client.post(
                "/api/v1/notebooks/sessions", json={"path": "renamed.ipynb"}, headers=headers
            )
            assert r4.status_code == 200
            assert r4.json()["id"] == "sess-1"

            # Restart & Interrupt
            assert (
                client.post("/api/v1/notebooks/kernels/k-1/restart", headers=headers).status_code
                == 200
            )
            assert (
                client.post("/api/v1/notebooks/kernels/k-1/interrupt", headers=headers).status_code
                == 200
            )

            # Delete
            assert (
                client.delete("/api/v1/notebooks/renamed.ipynb", headers=headers).status_code == 204
            )
    finally:
        backend_main.app.dependency_overrides.clear()


def test_backend_notebooks_path_traversal_returns_400():
    user, workspace, member, token = _generate_test_user_and_token(role="editor")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with patch(
            "notebooks_router.get_notebook_workspace_and_role", return_value=(workspace, "editor")
        ):
            headers = {"Authorization": f"Bearer {token}"}
            res = client.get("/api/v1/notebooks/..%2F..%2Fetc%2Fpasswd", headers=headers)
            assert res.status_code == 400
            assert "Invalid path traversal" in res.json()["error"]
    finally:
        backend_main.app.dependency_overrides.clear()


def test_backend_notebooks_search_and_directories():
    user, workspace, member, token = _generate_test_user_and_token(role="member")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with (
            patch(
                "notebooks_router.get_notebook_workspace_and_role",
                return_value=(workspace, "member"),
            ),
            patch.object(
                notebook_service,
                "search_workspace_items",
                new_callable=AsyncMock,
                return_value=[{"name": "sales.ipynb", "type": "notebook"}],
            ),
            patch.object(
                notebook_service,
                "list_workspace_folders",
                new_callable=AsyncMock,
                return_value=["folder1", "folder1/sub"],
            ),
        ):
            headers = {"Authorization": f"Bearer {token}"}

            # Search
            res_search = client.get("/api/v1/notebooks/search?q=sales", headers=headers)
            assert res_search.status_code == 200
            assert len(res_search.json()["items"]) == 1

            # Directories
            res_dirs = client.get("/api/v1/notebooks/directories", headers=headers)
            assert res_dirs.status_code == 200
            assert res_dirs.json()["directories"] == ["folder1", "folder1/sub"]
    finally:
        backend_main.app.dependency_overrides.clear()


def test_backend_notebooks_runs_archived_list():
    user, workspace, member, token = _generate_test_user_and_token(role="member")
    mock_db = MagicMock()

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db
    backend_main.app.dependency_overrides[auth.get_current_user] = lambda: user

    try:
        with (
            patch(
                "notebooks_router.get_notebook_workspace_and_role",
                return_value=(workspace, "member"),
            ),
            patch.object(
                notebook_service,
                "list_archived_runs",
                return_value=[{"filename": "20261014.ipynb", "key": "key1", "size": 1024}],
            ),
        ):
            headers = {"Authorization": f"Bearer {token}"}
            res = client.get("/api/v1/notebooks/runs?path=analysis.ipynb", headers=headers)
            assert res.status_code == 200
            assert len(res.json()["runs"]) == 1
            assert res.json()["runs"][0]["filename"] == "20261014.ipynb"
    finally:
        backend_main.app.dependency_overrides.clear()


# ── Direct RBAC Helper Tests ──────────────────────────────────────────────────


def test_get_notebook_workspace_and_role_non_member():
    user = User(id=uuid.uuid4(), email="stranger@lake.local", role="user")
    ws = Workspace(id=uuid.uuid4(), name="Secret WS", slug="secret-ws", status="active")
    mock_db = MagicMock()
    # Query for workspace returns ws
    mock_db.query.return_value.filter.return_value.first.return_value = ws
    # Query for member returns None
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, None]

    with pytest.raises(backend_main.HTTPException) as exc_info:
        auth.get_notebook_workspace_and_role(
            workspace_slug="secret-ws",
            current_user=user,
            db=mock_db,
            require_write=False,
        )
    assert exc_info.value.status_code == 403
    assert "Not a member" in exc_info.value.detail


def test_get_notebook_workspace_and_role_viewer_blocked_on_write():
    user = User(id=uuid.uuid4(), email="viewer@lake.local", role="user")
    ws = Workspace(id=uuid.uuid4(), name="WS", slug="ws", status="active")
    member = WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="viewer")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, member]

    with pytest.raises(backend_main.HTTPException) as exc_info:
        auth.get_notebook_workspace_and_role(
            workspace_slug="ws",
            current_user=user,
            db=mock_db,
            require_write=True,
        )
    assert exc_info.value.status_code == 403
    assert "viewer role is read-only" in exc_info.value.detail


def test_get_notebook_workspace_and_role_superadmin_allowed():
    user = User(id=uuid.uuid4(), email="super@lake.local", role="superadmin")
    ws = Workspace(id=uuid.uuid4(), name="Any WS", slug="any-ws", status="active")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [ws, None]

    resolved_ws, role = auth.get_notebook_workspace_and_role(
        workspace_slug="any-ws",
        current_user=user,
        db=mock_db,
        require_write=True,
    )
    assert resolved_ws == ws
    assert role == "superadmin"


# ── WebSocket Proxy Tests ─────────────────────────────────────────────────────


def test_websocket_kernel_proxy_unauthenticated():
    with client.websocket_connect("/api/v1/notebooks/kernels/k1/channels") as ws:
        msg = ws.receive_json()
        assert msg["msg_type"] == "error"
        assert "Authentication required" in msg["content"]["evalue"]


def test_websocket_kernel_proxy_viewer_rejected():
    user, workspace, member, token = _generate_test_user_and_token(role="viewer")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = user

    backend_main.app.dependency_overrides[database.get_db] = lambda: mock_db

    try:
        with patch(
            "notebooks_router.get_notebook_workspace_and_role",
            side_effect=backend_main.HTTPException(403, "Viewer cannot execute cells"),
        ):
            with client.websocket_connect(
                f"/api/v1/notebooks/kernels/k1/channels?token={token}&workspace_slug={workspace.slug}"
            ) as ws:
                msg = ws.receive_json()
                assert msg["msg_type"] == "error"
                assert "Viewer cannot execute cells" in msg["content"]["evalue"]
    finally:
        backend_main.app.dependency_overrides.clear()
