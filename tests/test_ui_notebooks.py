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
        assert "btn-audio-cue" in response.text
        assert "playSuccessSound" in response.text
        assert "playErrorSound" in response.text
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


# ── Directory Creation & Dual Routing Tests ───────────────────────────────────


@pytest.mark.anyio
async def test_ensure_workspace_dir_calls_put():
    with patch.object(notebook_proxy, "jupyter_request", new_callable=AsyncMock) as mock_req:
        mock_req.side_effect = [
            (201, {}),
            (404, {}),
            (201, {}),
        ]
        await notebook_proxy.ensure_workspace_dir("analytics")
        assert mock_req.call_count == 3
        assert mock_req.call_args_list[0].args == ("PUT", "contents/workspaces")
        assert mock_req.call_args_list[0].kwargs["json_data"] == {"type": "directory"}
        assert mock_req.call_args_list[2].args == ("PUT", "contents/workspaces/analytics")
        assert mock_req.call_args_list[2].kwargs["json_data"] == {"type": "directory"}


@pytest.mark.anyio
async def test_create_folder_calls_put():
    with (
        patch.object(notebook_proxy, "ensure_workspace_dir", new_callable=AsyncMock),
        patch.object(notebook_proxy, "jupyter_request", new_callable=AsyncMock) as mock_req,
    ):
        mock_req.return_value = (201, {"name": "subfolder", "type": "directory"})
        status, data = await notebook_proxy.create_folder("analytics", "subfolder")
        assert status == 201
        assert data["name"] == "subfolder"
        mock_req.assert_called_once_with(
            "PUT", "contents/workspaces/analytics/subfolder", json_data={"type": "directory"}
        )


def test_notebooks_dual_routing():
    token, workspaces = _create_auth_cookie(role="member")
    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(
            notebook_proxy,
            "list_workspace_notebooks",
            new_callable=AsyncMock,
            return_value={
                "items": [{"name": "dual.ipynb", "type": "notebook"}],
                "workspace": "default",
            },
        ),
        patch.object(
            notebook_proxy,
            "create_folder",
            new_callable=AsyncMock,
            return_value=(201, {"name": "testdir", "type": "directory"}),
        ),
    ):
        res_list = client.get("/notebooks/api", cookies=cookies)
        assert res_list.status_code == 200
        assert res_list.json()["items"][0]["name"] == "dual.ipynb"

        res_dir = client.post(
            "/notebooks/api",
            json={"type": "directory", "name": "testdir"},
            cookies=cookies,
        )
        assert res_dir.status_code == 201
        assert res_dir.json()["name"] == "testdir"


@pytest.mark.anyio
async def test_list_workspace_folders_recursive():
    with (
        patch.object(notebook_proxy, "ensure_workspace_dir", new_callable=AsyncMock),
        patch.object(notebook_proxy, "jupyter_request", new_callable=AsyncMock) as mock_req,
    ):
        # Mock root listing having "dir_a" and "file.ipynb", then "dir_a" having "sub_b"
        mock_req.side_effect = [
            (
                200,
                {
                    "content": [
                        {"name": "dir_a", "type": "directory", "path": "workspaces/default/dir_a"},
                        {
                            "name": "nb.ipynb",
                            "type": "notebook",
                            "path": "workspaces/default/nb.ipynb",
                        },
                    ]
                },
            ),
            (
                200,
                {
                    "content": [
                        {
                            "name": "sub_b",
                            "type": "directory",
                            "path": "workspaces/default/dir_a/sub_b",
                        },
                    ]
                },
            ),
            (200, {"content": []}),
        ]
        folders = await notebook_proxy.list_workspace_folders("default")
        assert folders == ["dir_a", "dir_a/sub_b"]


def test_api_list_directories_and_move_item():
    token, workspaces = _create_auth_cookie(role="member")
    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(
            notebook_proxy,
            "list_workspace_folders",
            new_callable=AsyncMock,
            return_value=["analytics", "analytics/models"],
        ),
        patch.object(
            notebook_proxy,
            "rename_item",
            new_callable=AsyncMock,
            return_value=(200, {"name": "models", "path": "workspaces/default/analytics/models"}),
        ),
    ):
        # 1. Test listing directories
        res_dirs = client.get("/notebooks/api/directories", cookies=cookies)
        assert res_dirs.status_code == 200
        assert res_dirs.json()["directories"] == ["analytics", "analytics/models"]

        # 2. Test moving item (notebook or folder) into another directory
        res_move = client.patch(
            "/notebooks/api/models",
            json={"new_path": "analytics/models"},
            cookies=cookies,
        )
        assert res_move.status_code == 200
        assert res_move.json()["path"] == "workspaces/default/analytics/models"


@pytest.mark.anyio
async def test_search_workspace_items_recursive_root():
    with (
        patch.object(notebook_proxy, "ensure_workspace_dir", new_callable=AsyncMock),
        patch.object(notebook_proxy, "jupyter_request", new_callable=AsyncMock) as mock_req,
    ):
        mock_req.side_effect = [
            # 1. Root listing
            (
                200,
                {
                    "content": [
                        {"name": "aws", "type": "directory", "path": "workspaces/default/aws"},
                        {"name": "azure", "type": "directory", "path": "workspaces/default/azure"},
                        {
                            "name": "db_root.ipynb",
                            "type": "notebook",
                            "path": "workspaces/default/db_root.ipynb",
                        },
                        {
                            "name": "readme.md",
                            "type": "file",
                            "path": "workspaces/default/readme.md",
                        },
                    ]
                },
            ),
            # 2. aws/ listing
            (
                200,
                {
                    "content": [
                        {
                            "name": "db_dump.ipynb",
                            "type": "notebook",
                            "path": "workspaces/default/aws/db_dump.ipynb",
                        },
                        {
                            "name": "nested_db",
                            "type": "directory",
                            "path": "workspaces/default/aws/nested_db",
                        },
                    ]
                },
            ),
            # 3. azure/ listing
            (
                200,
                {
                    "content": [
                        {
                            "name": "db_etl.ipynb",
                            "type": "notebook",
                            "path": "workspaces/default/azure/db_etl.ipynb",
                        },
                    ]
                },
            ),
            # 4. aws/nested_db/ listing
            (
                200,
                {
                    "content": [
                        {
                            "name": "deep.ipynb",
                            "type": "notebook",
                            "path": "workspaces/default/aws/nested_db/deep.ipynb",
                        },
                    ]
                },
            ),
        ]

        # Search for "db" from root
        results = await notebook_proxy.search_workspace_items("default", query="db", subpath="")
        paths = [item["path"] for item in results]
        names = [item["name"] for item in results]

        # Should match nested_db directory, db_dump.ipynb, db_etl.ipynb, and db_root.ipynb
        assert "aws/nested_db" in paths
        assert "aws/db_dump.ipynb" in paths
        assert "azure/db_etl.ipynb" in paths
        assert "db_root.ipynb" in paths
        assert "deep.ipynb" not in names
        assert "readme.md" not in names

        # Verify parent_dir attributes
        nested_dir_item = next(i for i in results if i["name"] == "nested_db")
        assert nested_dir_item["parent_dir"] == "aws"
        assert nested_dir_item["type"] == "directory"

        aws_item = next(i for i in results if i["name"] == "db_dump.ipynb")
        assert aws_item["parent_dir"] == "aws"

        azure_item = next(i for i in results if i["name"] == "db_etl.ipynb")
        assert azure_item["parent_dir"] == "azure"

        root_item = next(i for i in results if i["name"] == "db_root.ipynb")
        assert root_item["parent_dir"] == ""


@pytest.mark.anyio
async def test_search_workspace_items_scoped_to_subfolder():
    with (
        patch.object(notebook_proxy, "ensure_workspace_dir", new_callable=AsyncMock),
        patch.object(notebook_proxy, "jupyter_request", new_callable=AsyncMock) as mock_req,
    ):
        mock_req.side_effect = [
            # Listing aws/
            (
                200,
                {
                    "content": [
                        {
                            "name": "db_aws.ipynb",
                            "type": "notebook",
                            "path": "workspaces/default/aws/db_aws.ipynb",
                        },
                        {
                            "name": "pipelines",
                            "type": "directory",
                            "path": "workspaces/default/aws/pipelines",
                        },
                    ]
                },
            ),
            # Listing aws/pipelines/
            (
                200,
                {
                    "content": [
                        {
                            "name": "db_pipe.ipynb",
                            "type": "notebook",
                            "path": "workspaces/default/aws/pipelines/db_pipe.ipynb",
                        },
                    ]
                },
            ),
        ]

        # Search for "db" scoped to "aws"
        results = await notebook_proxy.search_workspace_items("default", query="db", subpath="aws")
        paths = [item["path"] for item in results]
        assert paths == ["aws/db_aws.ipynb", "aws/pipelines/db_pipe.ipynb"]


def test_api_search_endpoint():
    token, workspaces = _create_auth_cookie(role="member")
    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}

    mock_search_results = [
        {
            "name": "db_sync.ipynb",
            "path": "aws/db_sync.ipynb",
            "type": "notebook",
            "parent_dir": "aws",
        }
    ]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(
            notebook_proxy,
            "search_workspace_items",
            new_callable=AsyncMock,
            return_value=mock_search_results,
        ) as mock_search,
    ):
        # 1. Successful search via /notebooks/api/search
        resp = client.get("/notebooks/api/search?q=db&subpath=aws", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "db"
        assert data["subpath"] == "aws"
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "db_sync.ipynb"
        mock_search.assert_called_once_with("default", query="db", subpath="aws")

        # 2. Path traversal in subpath returns 400
        resp_invalid = client.get("/notebooks/api/search?q=db&subpath=../secret", cookies=cookies)
        assert resp_invalid.status_code == 400
        assert "Invalid path traversal" in resp_invalid.json()["error"]


def test_workspace_search_unauthenticated_blocked():
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.get("/api/workspace/search?q=test")
        assert res.status_code == 401
        assert res.json()["detail"] == "Not authenticated"


def test_workspace_search_aggregated_results():
    token, workspaces = _create_auth_cookie(role="member")
    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}

    mock_nb_items = [
        {
            "name": "aws_db_ingest.ipynb",
            "path": "aws/aws_db_ingest.ipynb",
            "type": "notebook",
            "parent_dir": "aws",
        },
        {
            "name": "db_backups",
            "path": "db_backups",
            "type": "directory",
            "parent_dir": "",
        },
    ]

    mock_catalogs = [
        {"name": "ecommerce_db", "description": "Customer order data warehouse"},
        {"name": "marketing_events", "description": "Clickstream events"},
    ]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(
            notebook_proxy,
            "search_workspace_items",
            new_callable=AsyncMock,
            return_value=mock_nb_items,
        ) as mock_search,
        patch.object(main, "_backend_get", return_value=mock_catalogs),
    ):
        resp = client.get("/api/workspace/search?q=db", cookies=cookies)
        assert resp.status_code == 200
        mock_search.assert_called_once_with("default", query="db", subpath="")
        data = resp.json()
        assert data["query"] == "db"
        assert data["workspace"] == "default"
        results = data["results"]

        # Check notebook result
        nb_matches = [r for r in results if r["type"] == "notebook"]
        assert len(nb_matches) == 1
        assert nb_matches[0]["title"] == "aws_db_ingest.ipynb"
        assert nb_matches[0]["url"] == "/notebooks?path=aws/aws_db_ingest.ipynb"
        assert "Notebook in /aws" in nb_matches[0]["subtitle"]

        # Check folder result
        folder_matches = [r for r in results if r["type"] == "folder"]
        assert len(folder_matches) == 1
        assert folder_matches[0]["title"] == "db_backups"
        assert folder_matches[0]["url"] == "/notebooks?folder=db_backups"

        # Check catalog result
        cat_matches = [r for r in results if r["type"] == "catalog"]
        assert any(c["title"] == "ecommerce_db" for c in cat_matches)

        # Check page result (e.g. SQL Editor mentioning DuckDB)
        page_matches = [r for r in results if r["type"] == "page"]
        assert any("DuckDB" in p["subtitle"] or "SQL" in p["title"] for p in page_matches)


def test_workspace_search_empty_query():
    token, workspaces = _create_auth_cookie(role="member")
    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(
            notebook_proxy,
            "search_workspace_items",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(main, "_backend_get", return_value=[]),
    ):
        resp = client.get("/api/workspace/search", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        # Should include pipelines, storage sources, and nav pages
        types = {r["type"] for r in data["results"]}
        assert "pipeline" in types
        assert "storage" in types
        assert "page" in types


def test_workspace_search_alternate_path():
    token, workspaces = _create_auth_cookie(role="member")
    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(
            notebook_proxy,
            "search_workspace_items",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(main, "_backend_get", return_value=[]),
    ):
        resp = client.get("/workspace/search?q=pipeline", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "pipeline"
        assert len(data["results"]) > 0


def test_base_template_renders_search_dropdown_components():
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
        assert "workspace-search-wrap" in response.text
        assert "workspace-search-input" in response.text
        assert "workspace-search-dropdown" in response.text
        assert "workspace-search-results" in response.text
        assert "⌘P" in response.text
        assert "openSearchDropdown" in response.text
        # Ensure modal is not rendered
        assert "quick-open-modal" not in response.text
