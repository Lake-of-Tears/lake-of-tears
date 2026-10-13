from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from starlette.testclient import TestClient

# Ensure ui directory is in sys.path
sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import main  # noqa: E402
import notebook_proxy  # noqa: E402

client = TestClient(main.app)


# ── Path Sanitization & Workspace Guard Tests ─────────────────────────────────


def test_sanitize_notebook_path_valid():
    assert notebook_proxy.sanitize_notebook_path("analysis.ipynb") == "analysis.ipynb"
    assert notebook_proxy.sanitize_notebook_path("folder/sub/nb.ipynb") == "folder/sub/nb.ipynb"
    assert notebook_proxy.sanitize_notebook_path("/leading/slash/") == "leading/slash"
    assert notebook_proxy.sanitize_notebook_path("") == ""


def test_sanitize_notebook_path_traversal_blocked():
    with pytest.raises(ValueError, match="Invalid path traversal segment"):
        notebook_proxy.sanitize_notebook_path("../secret.py")

    with pytest.raises(ValueError, match="Invalid path traversal segment"):
        notebook_proxy.sanitize_notebook_path("folder/../../etc/passwd")

    with pytest.raises(ValueError, match="Invalid path traversal segment"):
        notebook_proxy.sanitize_notebook_path("./relative.ipynb")


def test_sanitize_notebook_path_invalid_characters_blocked():
    for char in ["<", ">", ":", '"', "|", "?", "*", "\x00"]:
        with pytest.raises(ValueError, match="Invalid characters in path segment"):
            notebook_proxy.sanitize_notebook_path(f"notebook{char}.ipynb")


def test_get_workspace_dir():
    assert notebook_proxy.get_workspace_dir("default") == "workspaces/default"
    assert (
        notebook_proxy.get_workspace_dir("marketing", "eda.ipynb")
        == "workspaces/marketing/eda.ipynb"
    )
    assert (
        notebook_proxy.get_workspace_dir("eng", "sub/folder/nb.ipynb")
        == "workspaces/eng/sub/folder/nb.ipynb"
    )


def test_create_empty_notebook_dict():
    nb = notebook_proxy.create_empty_notebook_dict(title="Test Analysis")
    assert nb["nbformat"] == 4
    assert len(nb["cells"]) == 2
    assert nb["cells"][0]["cell_type"] == "markdown"
    assert "# Test Analysis" in nb["cells"][0]["source"][0]
    assert nb["cells"][1]["cell_type"] == "code"
    assert any("duckdb" in line for line in nb["cells"][1]["source"])


# ── UI Route & Template Tests ────────────────────────────────────────────────


def test_get_notebooks_page_renders_native_template():
    token = jwt.encode(
        {"sub": "user@lake.local", "email": "user@lake.local", "role": "user"},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default", "my_role": "member"}]

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            "/notebooks",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200
        # Verify it renders native UI, not iframe embed
        assert "Workspace Notebooks" in response.text
        assert "CodeMirror" in response.text
        assert "Kernel Offline" in response.text
        assert '<iframe src="/jupyter/' not in response.text


def test_get_notebooks_page_viewer_mode_banner():
    token = jwt.encode(
        {"sub": "viewer@lake.local", "email": "viewer@lake.local", "role": "user"},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default", "my_role": "viewer"}]

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        response = client.get(
            "/notebooks",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200
        assert "Viewer Mode (Read-Only)" in response.text


# ── RBAC Enforcement Tests ───────────────────────────────────────────────────


def _create_auth_cookie(role: str = "member", global_role: str = "user") -> tuple[str, list[dict]]:
    token = jwt.encode(
        {"sub": f"{role}@lake.local", "email": f"{role}@lake.local", "role": global_role},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    workspaces = [{"id": "ws-1", "name": "Default", "slug": "default", "my_role": role}]
    return token, workspaces


def test_notebooks_api_unauthenticated_blocked():
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.get("/api/notebooks")
        assert res.status_code == 401
        assert res.json()["detail"] == "Not authenticated"


def test_notebooks_api_viewer_can_read():
    token, workspaces = _create_auth_cookie(role="viewer")
    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(
            notebook_proxy,
            "list_workspace_notebooks",
            new_callable=AsyncMock,
            return_value={
                "items": [{"name": "test.ipynb", "type": "notebook"}],
                "workspace": "default",
            },
        ),
    ):
        res = client.get(
            "/api/notebooks",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert len(res.json()["items"]) == 1


def test_notebooks_api_viewer_cannot_write_or_execute():
    token, workspaces = _create_auth_cookie(role="viewer")
    headers = {"Authorization": f"Bearer {token}"}
    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
    ):
        # 1. Create notebook -> 403
        r_create = client.post(
            "/api/notebooks", json={"name": "hacked.ipynb"}, cookies=cookies, headers=headers
        )
        assert r_create.status_code == 403
        assert "viewer role is read-only" in r_create.json()["detail"]

        # 2. Save notebook -> 403
        r_save = client.put(
            "/api/notebooks/test.ipynb", json={"content": {}}, cookies=cookies, headers=headers
        )
        assert r_save.status_code == 403

        # 3. Delete notebook -> 403
        r_del = client.delete("/api/notebooks/test.ipynb", cookies=cookies, headers=headers)
        assert r_del.status_code == 403

        # 4. Rename notebook -> 403
        r_ren = client.patch(
            "/api/notebooks/test.ipynb",
            json={"new_path": "new.ipynb"},
            cookies=cookies,
            headers=headers,
        )
        assert r_ren.status_code == 403

        # 5. Start kernel session -> 403
        r_sess = client.post(
            "/api/notebooks/sessions", json={"path": "test.ipynb"}, cookies=cookies, headers=headers
        )
        assert r_sess.status_code == 403

        # 6. Interrupt / Restart kernel -> 403
        r_int = client.post("/api/notebooks/kernels/k1/interrupt", cookies=cookies, headers=headers)
        assert r_int.status_code == 403
        r_rst = client.post("/api/notebooks/kernels/k1/restart", cookies=cookies, headers=headers)
        assert r_rst.status_code == 403

        # 7. Run in Airflow pipeline -> 403
        r_pipe = client.post(
            "/api/notebooks/run-pipeline",
            json={"path": "test.ipynb"},
            cookies=cookies,
            headers=headers,
        )
        assert r_pipe.status_code == 403


def test_notebooks_api_member_can_write_and_execute():
    token, workspaces = _create_auth_cookie(role="member")
    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(
            notebook_proxy,
            "create_notebook",
            new_callable=AsyncMock,
            return_value=(201, {"name": "test.ipynb"}),
        ),
        patch.object(
            notebook_proxy,
            "save_notebook",
            new_callable=AsyncMock,
            return_value=(200, {"ok": True}),
        ),
        patch.object(notebook_proxy, "delete_item", new_callable=AsyncMock, return_value=(204, {})),
        patch.object(
            notebook_proxy, "rename_item", new_callable=AsyncMock, return_value=(200, {"ok": True})
        ),
        patch.object(
            notebook_proxy,
            "get_or_create_session",
            new_callable=AsyncMock,
            return_value=(200, {"id": "sess-123"}),
        ),
        patch.object(
            notebook_proxy,
            "restart_kernel",
            new_callable=AsyncMock,
            return_value=(200, {"status": "restarting"}),
        ),
        patch.object(
            notebook_proxy,
            "trigger_airflow_notebook_run",
            new_callable=AsyncMock,
            return_value=(201, {"dag_run_id": "manual__123"}),
        ),
    ):
        assert (
            client.post("/api/notebooks", json={"name": "test.ipynb"}, cookies=cookies).status_code
            == 201
        )
        assert (
            client.put(
                "/api/notebooks/test.ipynb", json={"content": {}}, cookies=cookies
            ).status_code
            == 200
        )
        assert (
            client.patch(
                "/api/notebooks/test.ipynb", json={"new_path": "new.ipynb"}, cookies=cookies
            ).status_code
            == 200
        )
        assert client.delete("/api/notebooks/test.ipynb", cookies=cookies).status_code == 204
        assert (
            client.post(
                "/api/notebooks/sessions", json={"path": "test.ipynb"}, cookies=cookies
            ).status_code
            == 200
        )
        assert client.post("/api/notebooks/kernels/k1/restart", cookies=cookies).status_code == 200
        assert (
            client.post(
                "/api/notebooks/run-pipeline", json={"path": "test.ipynb"}, cookies=cookies
            ).status_code
            == 201
        )


# ── WebSocket Proxy RBAC Tests ────────────────────────────────────────────────


def test_websocket_proxy_viewer_rejected():
    token, workspaces = _create_auth_cookie(role="viewer")
    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
    ):
        client_ws = TestClient(
            main.app, cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}
        )
        with client_ws.websocket_connect("/api/notebooks/kernels/k123/channels") as ws:
            msg = ws.receive_json()
            assert msg["msg_type"] == "error"
            assert msg["content"]["ename"] == "PermissionError"
            assert "Viewer role cannot execute cells" in msg["content"]["evalue"]


# ── Airflow & MinIO Archive Integration Tests ─────────────────────────────────


@pytest.mark.anyio
async def test_trigger_airflow_notebook_run_payload():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"dag_run_id": "manual__test", "state": "queued"}
        mock_post.return_value = mock_resp

        status, data = await notebook_proxy.trigger_airflow_notebook_run(
            workspace_slug="analytics",
            notebook_path="churn.ipynb",
            parameters={"cutoff": "2026-10-01"},
            user_email="data_scientist@example.com",
        )

        assert status == 201
        assert data["dag_run_id"] == "manual__test"
        mock_post.assert_called_once()
        sent_payload = mock_post.call_args.kwargs["json"]
        assert sent_payload["conf"]["workspace_slug"] == "analytics"
        assert sent_payload["conf"]["notebook_path"] == "churn.ipynb"
        assert sent_payload["conf"]["parameters"] == {"cutoff": "2026-10-01"}
        assert sent_payload["conf"]["triggered_by"] == "data_scientist@example.com"


def test_list_archived_runs_s3_parsing():
    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": "notebook_runs/default/churn/20261012_120000Z.ipynb",
                "Size": 15420,
            }
        ]
    }
    with patch("boto3.client", return_value=mock_s3):
        runs = notebook_proxy.list_archived_runs("default", "churn.ipynb")
        assert len(runs) == 1
        assert runs[0]["filename"] == "20261012_120000Z.ipynb"
        assert runs[0]["size"] == 15420
        assert runs[0]["key"] == "notebook_runs/default/churn/20261012_120000Z.ipynb"
