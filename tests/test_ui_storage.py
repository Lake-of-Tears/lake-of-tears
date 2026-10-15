import html
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import jwt
import pytest
from starlette.testclient import TestClient

# Ensure ui directory is in sys.path
sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import main  # noqa: E402

client = TestClient(main.app)


def _create_auth_cookie(role: str = "member", global_role: str = "user") -> tuple[str, list[dict]]:
    token = jwt.encode(
        {"sub": f"{role}@lake.local", "email": f"{role}@lake.local", "role": global_role},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    workspaces = [{"id": "ws-1", "name": "Analytics", "slug": "analytics", "my_role": role}]
    return token, workspaces


# ── UI Page Rendering Tests ───────────────────────────────────────────────────


def test_get_storage_page_renders_native_template():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_stats = {
        "ok": True,
        "total_objects": 15,
        "total_size": "25.0 MB",
        "raw_objects": 10,
        "raw_size": "20.0 MB",
        "embeddings_objects": 5,
        "embeddings_size": "5.0 MB",
    }

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_get_bucket_stats", return_value=mock_stats),
    ):
        response = client.get(
            "/storage",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert response.status_code == 200

        # Verify native UI, no iframe
        assert "<iframe" not in response.text

        # Verify key storage UI components
        assert 'id="breadcrumb-container"' in response.text
        assert 'id="objects-table-body"' in response.text
        assert 'id="explorer-search"' in response.text
        assert 'id="parquet-modal"' in response.text
        assert 'id="delete-modal"' in response.text
        assert "s3://datalake" in response.text


def test_storage_page_superadmin_escape_hatch():
    # 1. Non-superadmin should NOT see the external console link
    user_token, mock_workspaces = _create_auth_cookie(role="member", global_role="user")
    mock_stats = {
        "ok": True,
        "total_objects": 0,
        "total_size": "0 B",
        "raw_objects": 0,
        "raw_size": "0 B",
        "embeddings_objects": 0,
        "embeddings_size": "0 B",
    }

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_get_bucket_stats", return_value=mock_stats),
    ):
        resp = client.get(
            "/storage",
            cookies={"lake_token": user_token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert resp.status_code == 200
        assert "MinIO Admin Console" not in resp.text

    # 2. Superadmin SHOULD see the external console link
    admin_token, mock_workspaces = _create_auth_cookie(role="admin", global_role="superadmin")
    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_get_bucket_stats", return_value=mock_stats),
    ):
        resp = client.get(
            "/storage",
            cookies={"lake_token": admin_token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert resp.status_code == 200
        assert "MinIO Admin Console" in resp.text


def test_sql_editor_prefills_query_param():
    token, mock_workspaces = _create_auth_cookie(role="member")

    with patch.object(main, "_fetch_workspaces", return_value=mock_workspaces):
        # Default query
        resp_default = client.get(
            "/sql",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert resp_default.status_code == 200
        assert main.EXAMPLE_QUERIES[0]["sql"] in html.unescape(resp_default.text)

        # Query param 'q' from storage browser or catalog
        test_sql = "SELECT * FROM read_parquet('s3://datalake/raw/test/**/*.parquet') LIMIT 50"
        resp_param = client.get(
            f"/sql?q={test_sql}",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert resp_param.status_code == 200
        assert test_sql in html.unescape(resp_param.text)


# ── Storage API Proxy Endpoints Tests ─────────────────────────────────────────


@pytest.mark.anyio
async def test_ui_proxy_storage_stats():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_resp = httpx.Response(
        status_code=200,
        json={"ok": True, "bucket": "datalake", "total_objects": 20},
        request=httpx.Request("GET", "http://backend/api/v1/storage/stats"),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp) as mock_get,
    ):
        res = client.get(
            "/api/v1/storage/stats",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert res.json()["total_objects"] == 20
        assert mock_get.called


@pytest.mark.anyio
async def test_ui_proxy_storage_browse():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_resp = httpx.Response(
        status_code=200,
        json={
            "bucket": "datalake",
            "prefix": "raw/",
            "folders": [{"name": "stripe", "prefix": "raw/stripe/"}],
            "files": [],
        },
        request=httpx.Request("GET", "http://backend/api/v1/storage/browse"),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp) as mock_get,
    ):
        res = client.get(
            "/api/v1/storage/browse?prefix=raw/&limit=50",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert res.json()["prefix"] == "raw/"
        assert mock_get.called


@pytest.mark.anyio
async def test_ui_proxy_storage_metadata():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_resp = httpx.Response(
        status_code=200,
        json={"key": "raw/test.parquet", "size": 1024, "is_parquet": True},
        request=httpx.Request(
            "GET", "http://backend/api/v1/storage/objects/raw/test.parquet/metadata"
        ),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp) as mock_get,
    ):
        res = client.get(
            "/api/v1/storage/objects/raw/test.parquet/metadata",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert res.json()["is_parquet"] is True
        assert mock_get.called


@pytest.mark.anyio
async def test_ui_proxy_storage_download():
    token, mock_workspaces = _create_auth_cookie(role="member")

    mock_resp = httpx.Response(
        status_code=200,
        json={"download_url": "http://localhost:9000/datalake/raw/test.parquet?sig=123"},
        request=httpx.Request(
            "GET", "http://backend/api/v1/storage/objects/raw/test.parquet/download"
        ),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp) as mock_get,
    ):
        res = client.get(
            "/api/v1/storage/objects/raw/test.parquet/download",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert "download_url" in res.json()
        assert mock_get.called


@pytest.mark.anyio
async def test_ui_proxy_storage_delete():
    token, mock_workspaces = _create_auth_cookie(role="admin")

    mock_resp = httpx.Response(
        status_code=200,
        json={"deleted": True, "key": "raw/test.parquet"},
        request=httpx.Request("DELETE", "http://backend/api/v1/storage/objects/raw/test.parquet"),
    )

    with (
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch(
            "httpx.AsyncClient.delete", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_del,
    ):
        res = client.delete(
            "/api/v1/storage/objects/raw/test.parquet",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert res.json()["deleted"] is True
        assert mock_del.called
