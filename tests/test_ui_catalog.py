from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import jwt
from starlette.testclient import TestClient

# Ensure ui directory is in sys.path
sys.path.insert(0, str(Path(__file__).parents[1] / "ui"))

import main  # noqa: E402

client = TestClient(main.app)


def _make_token(sub="00000000-0000-0000-0000-000000000001", role="admin"):
    return jwt.encode(
        {"sub": sub, "email": "test@lake.local", "role": role},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )


# ── Backend Helper Tests ───────────────────────────────────────────────────────


def test_backend_post_with_error_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": "cat-1", "name": "production"}

    with patch("httpx.Client.post", return_value=mock_resp):
        data, err = main._backend_post_with_error("/api/catalogs", "tok", {"name": "production"})
        assert data == {"id": "cat-1", "name": "production"}
        assert err is None


def test_backend_post_with_error_conflict_409():
    mock_resp = MagicMock()
    mock_resp.status_code = 409
    mock_resp.json.return_value = {
        "detail": "A catalog with that name already exists in this workspace"
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        data, err = main._backend_post_with_error("/api/catalogs", "tok", {"name": "default"})
        assert data is None
        assert err == "A catalog with that name already exists in this workspace"


def test_backend_post_with_error_validation_422():
    mock_resp = MagicMock()
    mock_resp.status_code = 422
    mock_resp.json.return_value = {"detail": [{"msg": "workspace_id is required"}]}

    with patch("httpx.Client.post", return_value=mock_resp):
        data, err = main._backend_post_with_error("/api/catalogs", "tok", {"name": "foo"})
        assert data is None
        assert "workspace_id is required" in err


def test_backend_post_with_error_timeout():
    with patch("httpx.Client.post", side_effect=httpx.TimeoutException("timeout")):
        data, err = main._backend_post_with_error("/api/catalogs", "tok", {"name": "foo"})
        assert data is None
        assert err == "Backend request timed out"


def test_backend_post_with_error_unreachable():
    with patch("httpx.Client.post", side_effect=Exception("connection refused")):
        data, err = main._backend_post_with_error("/api/catalogs", "tok", {"name": "foo"})
        assert data is None
        assert err == "Backend service unreachable"


def test_backend_post_delegates_to_with_error():
    with patch.object(
        main,
        "_backend_post_with_error",
        return_value=({"id": "cat-1"}, None),
    ):
        assert main._backend_post("/api/catalogs", "tok", {}) == {"id": "cat-1"}

    with patch.object(
        main,
        "_backend_post_with_error",
        return_value=(None, "error"),
    ):
        assert main._backend_post("/api/catalogs", "tok", {}) is None


# ── _fetch_workspaces & AuthMiddleware Tests ───────────────────────────────────


def test_fetch_workspaces_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"id": "ws-1", "name": "Default"}]

    with patch("httpx.Client.get", return_value=mock_resp):
        res = main._fetch_workspaces("valid-token")
        assert res == [{"id": "ws-1", "name": "Default"}]


def test_fetch_workspaces_401_returns_none():
    mock_resp = MagicMock()
    mock_resp.status_code = 401

    with patch("httpx.Client.get", return_value=mock_resp):
        res = main._fetch_workspaces("invalid-or-expired-token")
        assert res is None


def test_auth_middleware_redirects_and_clears_cookie_on_401():
    token = _make_token()
    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=None),
    ):
        res = client.get(
            "/catalog",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            follow_redirects=False,
        )
        assert res.status_code == 302
        assert "/login?reason=session_expired" in res.headers.get("location", "")
        # Verify cookies are cleared
        set_cookie = res.headers.get("set-cookie", "")
        assert 'lake_token=""' in set_cookie or "Max-Age=0" in set_cookie


# ── Catalog Page Rendering Tests ───────────────────────────────────────────────


def test_catalog_page_renders_without_error():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default"}]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_get", return_value=[]),
    ):
        res = client.get(
            "/catalog",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert "catalog-error-banner" not in res.text
        assert "Create catalog" in res.text


def test_catalog_page_renders_error_banner_when_present():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default"}]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_get", return_value=[]),
    ):
        res = client.get(
            "/catalog?error=A+catalog+with+that+name+already+exists",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert "catalog-error-banner" in res.text
        assert "A catalog with that name already exists" in res.text


def test_catalog_page_missing_workspace_renders_modal_warning():
    token = _make_token()

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=[]),
        patch.object(main, "_backend_get", return_value=[]),
    ):
        res = client.get(
            "/catalog",
            cookies={"lake_token": token},
        )
        assert res.status_code == 200
        assert "No active workspace found" in res.text


# ── Catalog Create Route Tests ─────────────────────────────────────────────────


def test_catalog_create_unauthenticated_redirects_login():
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.post(
            "/catalog/create",
            data={"name": "test", "description": ""},
            follow_redirects=False,
        )
        assert res.status_code == 302
        assert "/login" in res.headers.get("location", "")


def test_catalog_create_no_workspace_redirects_with_error():
    token = _make_token()

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=[]),
    ):
        res = client.post(
            "/catalog/create",
            data={"name": "new_cat", "workspace_id": ""},
            cookies={"lake_token": token},
            follow_redirects=False,
        )
        assert res.status_code == 302
        loc = res.headers.get("location", "")
        assert "/catalog?error=" in loc
        assert "No+active+workspace+selected" in loc or "workspace" in loc.lower()


def test_catalog_create_empty_name_redirects_with_error():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default"}]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
    ):
        res = client.post(
            "/catalog/create",
            data={"name": "   ", "workspace_id": "ws-1"},
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            follow_redirects=False,
        )
        assert res.status_code == 302
        loc = res.headers.get("location", "")
        assert "/catalog?error=" in loc
        assert "empty" in loc.lower()


def test_catalog_create_backend_conflict_redirects_with_error():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default"}]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(
            main,
            "_backend_post_with_error",
            return_value=(None, "A catalog with that name already exists in this workspace"),
        ),
    ):
        res = client.post(
            "/catalog/create",
            data={"name": "default", "workspace_id": "ws-1"},
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            follow_redirects=False,
        )
        assert res.status_code == 302
        loc = res.headers.get("location", "")
        assert "/catalog?error=" in loc
        assert "already+exists" in loc or "already exists" in loc


def test_catalog_create_success_redirects_to_catalog():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default"}]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(
            main,
            "_backend_post_with_error",
            return_value=({"id": "cat-new", "name": "analytics"}, None),
        ),
    ):
        res = client.post(
            "/catalog/create",
            data={"name": "analytics", "workspace_id": "ws-1"},
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
            follow_redirects=False,
        )
        assert res.status_code == 302
        assert res.headers.get("location") == "/catalog"


# ── /api/catalog/preview Tests ────────────────────────────────────────────────


def test_catalog_preview_requires_auth():
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.get("/catalog/preview?view=bronze.tmdb_trending")
        assert res.status_code == 401
        assert res.json()["ok"] is False


def test_catalog_preview_rejects_invalid_view_name():
    token = _make_token()
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.get(
            "/catalog/preview?view=invalid;drop table;--",
            cookies={"lake_token": token},
        )
        assert res.status_code == 400
        assert res.json()["ok"] is False
        assert "Invalid view identifier" in res.json()["error"]


def test_catalog_preview_success():
    token = _make_token()
    mock_query_result = {
        "ok": True,
        "columns": ["id", "title"],
        "rows": [[1, "Inception"], [2, "Interstellar"]],
        "row_count": 2,
        "execution_ms": 1.25,
    }

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_resolve_ui_catalogs", return_value=[]),
        patch.object(main, "_run_query", return_value=mock_query_result),
    ):
        res = client.get(
            "/catalog/preview?view=bronze.tmdb_trending&limit=10",
            cookies={"lake_token": token},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["columns"] == ["id", "title"]
        assert len(data["rows"]) == 2
        assert data["row_count"] == 2
        assert data["execution_ms"] == 1.25


# ── Schema Ordering Tests ───────────────────────────────────────────────────────


def test_sort_schemas_orders_bronze_silver_gold_then_custom():
    schemas = [
        {"slug": "gold", "tier": "gold", "name": "Gold"},
        {"slug": "analytics", "tier": None, "name": "Analytics"},
        {"slug": "bronze", "tier": "bronze", "name": "Bronze"},
        {"slug": "silver", "tier": "silver", "name": "Silver"},
    ]
    sorted_s = main._sort_schemas(schemas)
    slugs = [s["slug"] for s in sorted_s]
    assert slugs == ["bronze", "silver", "gold", "analytics"]


def test_catalog_page_renders_schemas_in_bronze_silver_gold_order():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default"}]
    # Backend returns schemas in unordered/alphabetical (bronze, gold, silver) order
    mock_catalogs = [
        {
            "id": "cat-1",
            "name": "Default",
            "slug": "default",
            "schemas": [
                {"id": "sch-gold", "slug": "gold", "tier": "gold", "name": "Gold", "tables": []},
                {
                    "id": "sch-bronze",
                    "slug": "bronze",
                    "tier": "bronze",
                    "name": "Bronze",
                    "tables": [],
                },
                {
                    "id": "sch-silver",
                    "slug": "silver",
                    "tier": "silver",
                    "name": "Silver",
                    "tables": [],
                },
            ],
            "my_access": "owner",
        }
    ]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_get", return_value=mock_catalogs),
    ):
        res = client.get(
            "/catalog",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        # In the rendered HTML, bronze must appear before silver, and silver before gold
        pos_bronze = res.text.find("sch-bronze")
        pos_silver = res.text.find("sch-silver")
        pos_gold = res.text.find("sch-gold")
        assert pos_bronze != -1
        assert pos_silver != -1
        assert pos_gold != -1
        assert pos_bronze < pos_silver < pos_gold


# ── Workspace Search for Schemas and Tables Tests ───────────────────────────────


def test_workspace_search_finds_schemas_and_tables():
    token = _make_token()
    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"}
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default"}]
    mock_catalogs = [
        {
            "id": "cat-1",
            "name": "Finance",
            "slug": "finance",
            "description": "Financial Data",
            "schemas": [
                {
                    "id": "sch-bronze",
                    "slug": "bronze",
                    "tier": "bronze",
                    "name": "Bronze Ingest",
                    "description": "Raw ingest tier",
                    "tables": [
                        {
                            "id": "tbl-loans",
                            "slug": "loans-data",
                            "name": "Lending Club Loans",
                            "description": "Underwriting and loan metrics",
                            "s3_path_pattern": "s3://datalake/raw/loans/**/*.parquet",
                            "column_defs": [
                                {"name": "loan_id", "type": "BIGINT"},
                                {"name": "loan_amnt", "type": "DOUBLE"},
                                {"name": "fico_score", "type": "INTEGER"},
                            ],
                        }
                    ],
                }
            ],
        }
    ]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_get", return_value=mock_catalogs),
        patch.object(main.notebook_proxy, "search_workspace_items", return_value=[]),
    ):
        # 1. Search for schema
        resp = client.get("/api/workspace/search?q=bronze", cookies=cookies)
        assert resp.status_code == 200
        schema_results = [r for r in resp.json()["results"] if r["type"] == "schema"]
        assert len(schema_results) >= 1
        assert schema_results[0]["category"] == "Schemas"
        assert schema_results[0]["title"] == "Bronze Ingest"
        assert schema_results[0]["url"] == "/catalog?catalog_id=cat-1&schema_id=sch-bronze"

        # 2. Search for table by name
        resp = client.get("/api/workspace/search?q=lending", cookies=cookies)
        assert resp.status_code == 200
        table_results = [r for r in resp.json()["results"] if r["type"] == "table"]
        assert len(table_results) >= 1
        assert table_results[0]["category"] == "Tables"
        assert table_results[0]["title"] == "Lending Club Loans"
        assert (
            table_results[0]["url"]
            == "/catalog?catalog_id=cat-1&schema_id=sch-bronze&table_id=tbl-loans"
        )

        # 3. Deep search by column name (fico_score)
        resp = client.get("/api/workspace/search?q=fico_score", cookies=cookies)
        assert resp.status_code == 200
        deep_table_results = [r for r in resp.json()["results"] if r["type"] == "table"]
        assert len(deep_table_results) >= 1
        assert deep_table_results[0]["title"] == "Lending Club Loans"
        assert "Matched column: fico_score" in deep_table_results[0]["subtitle"]
        assert (
            deep_table_results[0]["url"]
            == "/catalog?catalog_id=cat-1&schema_id=sch-bronze&table_id=tbl-loans"
        )


def test_catalog_page_includes_deep_link_and_navigation_handlers():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Default", "slug": "default"}]
    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_get", return_value=[]),
    ):
        res = client.get(
            "/catalog",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        assert "window.navigateCatalogTo" in res.text
        assert "initCatalogDeepLink" in res.text


# ── /catalog/preview Sample Data Endpoint Tests ───────────────────────────────


def test_catalog_preview_resolves_workspace_and_queries_duckdb():
    token = _make_token()
    mock_workspaces = [{"id": "ws-1", "name": "Dev", "slug": "dev"}]
    mock_catalogs = [
        {
            "id": "cat-1",
            "name": "default",
            "schemas": [
                {
                    "id": "sch-bronze",
                    "slug": "bronze",
                    "tier": "bronze",
                    "tables": [
                        {
                            "id": "tbl-trials",
                            "slug": "clinical_trials",
                            "s3_path_pattern": "s3://datalake/workspaces/dev/bronze/clinical_trials/**/*.parquet",
                        }
                    ],
                }
            ],
        }
    ]

    mock_query_result = {
        "ok": True,
        "columns": ["nct_id", "brief_title"],
        "rows": [["NCT00001", "Study Title 1"], ["NCT00002", "Study Title 2"]],
    }

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_get", return_value=mock_catalogs),
        patch.object(main, "_run_query", return_value=mock_query_result) as mock_run,
    ):
        res = client.get(
            "/catalog/preview?view=bronze.clinical_trials&limit=15",
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-1"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["columns"] == ["nct_id", "brief_title"]
        assert len(data["rows"]) == 2

        # Verify _run_query received the populated catalog context
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert "bronze.clinical_trials" in mock_run.call_args[0][0]
        assert call_kwargs.get("catalogs") == mock_catalogs
        assert call_kwargs.get("read_only") is True


def test_catalog_preview_unauthenticated_returns_401():
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.get("/catalog/preview?view=bronze.clinical_trials")
        assert res.status_code == 401
        assert res.json()["ok"] is False
        assert "Not authenticated" in res.json()["error"]


def test_catalog_preview_invalid_view_returns_400():
    token = _make_token()
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.get(
            "/catalog/preview?view=bronze.bad;drop table",
            cookies={"lake_token": token},
        )
        assert res.status_code == 400
        assert res.json()["ok"] is False
        assert "Invalid view identifier" in res.json()["error"]


# ── Catalog Vector Sync Tests ───────────────────────────────────────────────────


def test_catalog_vector_sync_success_passes_workspace_context():
    token = _make_token()
    mock_workspaces = [
        {"id": "ws-100", "slug": "test-ws", "name": "Test Workspace", "role": "admin"}
    ]
    mock_table = {
        "id": "tbl-1",
        "name": "clinical_trials",
        "vector_index": {"status": "synced", "count": 10},
    }

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_patch") as mock_patch,
        patch.object(main, "_backend_post_with_error", return_value=(mock_table, None)) as mock_post,
    ):
        res = client.post(
            "/catalog/vector-sync",
            json={
                "catalog_id": "cat-1",
                "schema_id": "sch-1",
                "table_id": "tbl-1",
                "source_column": "overview",
                "format_template": "Study: {title} | {overview}",
            },
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-100"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["table"] == mock_table

        # Verify _backend_patch was called with workspace_id="ws-100"
        mock_patch.assert_called_once()
        patch_kwargs = mock_patch.call_args[1]
        assert patch_kwargs.get("workspace_id") == "ws-100"
        assert patch_kwargs.get("workspace_slug") == "test-ws"

        # Verify _backend_post_with_error was called with workspace_id="ws-100" and generous timeout
        mock_post.assert_called_once()
        post_kwargs = mock_post.call_args[1]
        assert post_kwargs.get("workspace_id") == "ws-100"
        assert post_kwargs.get("workspace_slug") == "test-ws"
        assert post_kwargs.get("timeout") == 180.0


def test_catalog_vector_sync_unauthenticated():
    client.cookies.clear()
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.post(
            "/catalog/vector-sync",
            json={"catalog_id": "cat-1", "schema_id": "sch-1", "table_id": "tbl-1"},
            follow_redirects=False,
        )
        assert res.status_code == 302
        assert "/login" in res.headers["location"]


def test_catalog_vector_sync_missing_ids():
    token = _make_token()
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.post(
            "/catalog/vector-sync",
            json={"catalog_id": "cat-1"},
            cookies={"lake_token": token},
        )
        assert res.status_code == 400
        assert res.json()["ok"] is False
        assert "Missing catalog_id, schema_id, or table_id" in res.json()["error"]


def test_catalog_vector_sync_backend_error():
    token = _make_token()
    mock_workspaces = [
        {"id": "ws-100", "slug": "test-ws", "name": "Test Workspace", "role": "admin"}
    ]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=mock_workspaces),
        patch.object(main, "_backend_patch"),
        patch.object(
            main,
            "_backend_post_with_error",
            return_value=(None, "Backend sync failed"),
        ),
    ):
        res = client.post(
            "/catalog/vector-sync",
            json={
                "catalog_id": "cat-1",
                "schema_id": "sch-1",
                "table_id": "tbl-1",
                "source_column": "overview",
            },
            cookies={"lake_token": token, main._WORKSPACE_COOKIE: "ws-100"},
        )
        assert res.status_code == 500
        assert res.json()["ok"] is False
        assert res.json()["error"] == "Backend sync failed"

