from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from starlette.testclient import TestClient

# Ensure ui and backend are in sys.path
root_dir = Path(__file__).parents[1]
ui_dir = str(root_dir / "ui")
backend_dir = str(root_dir / "backend")

if ui_dir not in sys.path:
    sys.path.insert(0, ui_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import main  # noqa: E402
import notebook_proxy  # noqa: E402
from models import Base, Catalog, CatalogSchema, CatalogTable, Workspace  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


client = TestClient(main.app)


def _make_auth_cookie(role="member", ws_slug="default", ws_id="00000000-0000-0000-0000-000000000001"):
    token = jwt.encode(
        {"sub": "00000000-0000-0000-0000-000000000002", "email": "dev@lake.local", "role": role},
        main.AUTH_SECRET_KEY,
        algorithm="HS256",
    )
    workspaces = [{"id": ws_id, "slug": ws_slug, "name": "Default Workspace"}]
    cookies = {"lake_token": token, main._WORKSPACE_COOKIE: ws_slug}
    return token, workspaces, cookies


# ── Filesystem Scanner Tests ───────────────────────────────────────────────────


def test_scan_workspace_filesystem_and_caching():
    with tempfile.TemporaryDirectory() as temp_dir:
        ws_slug = "test-ws"
        ws_path = os.path.join(temp_dir, ws_slug)
        os.makedirs(os.path.join(ws_path, "subfolder"), exist_ok=True)
        os.makedirs(os.path.join(ws_path, ".ipynb_checkpoints"), exist_ok=True)

        # Create files
        nb_file = os.path.join(ws_path, "analytics.ipynb")
        with open(nb_file, "w") as f:
            f.write("{}")

        sub_nb = os.path.join(ws_path, "subfolder", "deep.ipynb")
        with open(sub_nb, "w") as f:
            f.write("{}")

        hidden_nb = os.path.join(ws_path, ".ipynb_checkpoints", "checkpoint.ipynb")
        with open(hidden_nb, "w") as f:
            f.write("{}")

        with patch.object(notebook_proxy, "WORKSPACES_DIR", temp_dir):
            notebook_proxy.clear_fs_scan_cache()

            # First scan
            items = notebook_proxy.scan_workspace_filesystem(ws_slug)
            assert items is not None
            paths = [i["path"] for i in items]
            assert "analytics.ipynb" in paths
            assert "subfolder" in paths
            assert "subfolder/deep.ipynb" in paths
            assert not any(".ipynb_checkpoints" in p for p in paths)

            # Test cache hit
            cached_items = notebook_proxy.scan_workspace_filesystem(ws_slug)
            assert cached_items is items

            # Invalidate cache
            notebook_proxy.clear_fs_scan_cache(ws_slug)
            new_items = notebook_proxy.scan_workspace_filesystem(ws_slug)
            assert new_items is not items
            assert len(new_items) == len(items)


def test_scan_workspace_filesystem_unmounted_returns_none():
    with patch.object(notebook_proxy, "WORKSPACES_DIR", "/nonexistent/directory/path"):
        notebook_proxy.clear_fs_scan_cache()
        items = notebook_proxy.scan_workspace_filesystem("any-slug")
        assert items is None


@pytest.mark.anyio
async def test_search_workspace_items_fast_path_with_fs():
    with tempfile.TemporaryDirectory() as temp_dir:
        ws_slug = "fast-ws"
        ws_path = os.path.join(temp_dir, ws_slug)
        os.makedirs(ws_path, exist_ok=True)

        with open(os.path.join(ws_path, "orders_etl.ipynb"), "w") as f:
            f.write("{}")
        with open(os.path.join(ws_path, "customers.ipynb"), "w") as f:
            f.write("{}")

        with patch.object(notebook_proxy, "WORKSPACES_DIR", temp_dir):
            notebook_proxy.clear_fs_scan_cache()
            # Search query matching "orders"
            matches = await notebook_proxy.search_workspace_items(ws_slug, query="orders")
            assert len(matches) == 1
            assert matches[0]["name"] == "orders_etl.ipynb"
            assert matches[0]["type"] == "notebook"


# ── Backend Catalog Manifest Projection Tests ─────────────────────────────────


def test_backend_catalog_manifest_projection():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "backend_main_manifest_test", Path(backend_dir) / "main.py"
    )
    backend_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend_main)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)

    ws_id = uuid.uuid4()
    ws = Workspace(id=ws_id, name="Test WS", slug="test-ws")
    session.add(ws)
    session.commit()

    cat_id = uuid.uuid4()
    cat = Catalog(
        id=cat_id,
        name="Production Datalake",
        slug="production-datalake",
        description="Main data warehouse",
        owner_workspace_id=ws_id,
    )
    session.add(cat)
    session.commit()

    sch_id = uuid.uuid4()
    schema = CatalogSchema(
        id=sch_id,
        catalog_id=cat_id,
        name="Bronze Ingest",
        slug="bronze",
        tier="bronze",
        description="Raw incoming data",
    )
    session.add(schema)
    session.commit()

    tbl_id = uuid.uuid4()
    table = CatalogTable(
        id=tbl_id,
        schema_id=sch_id,
        name="Lending Club Loans",
        slug="lending_club_loans",
        description="Consumer credit lending data",
        s3_path_pattern="s3://datalake/raw/loans/*.parquet",
        column_defs=[
            {"name": "id", "data_type": "BIGINT", "nullable": False},
            {"name": "loan_amount", "data_type": "DOUBLE", "nullable": True},
            {"name": "fico_score", "data_type": "INTEGER", "nullable": True},
        ],
        vector_index={"status": "indexed", "count": 1000},
    )
    session.add(table)
    session.commit()

    catalogs = session.query(Catalog).filter(Catalog.id == cat_id).all()
    projection = backend_main._catalog_manifest_projection(catalogs)

    assert len(projection) == 1
    p_cat = projection[0]
    assert p_cat["name"] == "Production Datalake"
    assert p_cat["slug"] == "production-datalake"
    assert len(p_cat["schemas"]) == 1

    p_sch = p_cat["schemas"][0]
    assert p_sch["name"] == "Bronze Ingest"
    assert p_sch["tier"] == "bronze"
    assert len(p_sch["tables"]) == 1

    p_tbl = p_sch["tables"][0]
    assert p_tbl["name"] == "Lending Club Loans"
    assert p_tbl["slug"] == "lending_club_loans"
    assert p_tbl["columns"] == ["id", "loan_amount", "fico_score"]
    # Ensure heavy vector_index was excluded
    assert "vector_index" not in p_tbl


# ── UI Workspace Manifest Endpoint Tests ──────────────────────────────────────


def test_ui_workspace_manifest_unauthenticated_blocked():
    with patch.object(main, "AUTH_ENABLED", True):
        res = client.get("/api/workspace/manifest")
        assert res.status_code == 401
        assert res.json()["detail"] == "Not authenticated"


def test_ui_workspace_manifest_aggregation():
    token, workspaces, cookies = _make_auth_cookie(role="member")

    mock_catalogs = [
        {
            "id": "cat-1",
            "name": "ecommerce_db",
            "slug": "ecommerce_db",
            "description": "Customer order data warehouse",
            "schemas": [
                {
                    "id": "sch-1",
                    "name": "bronze",
                    "slug": "bronze",
                    "tier": "bronze",
                    "tables": [
                        {
                            "id": "tbl-1",
                            "name": "orders",
                            "slug": "orders",
                            "description": "Raw e-commerce orders",
                            "columns": ["id", "amount", "customer_id", "status"],
                        }
                    ],
                }
            ],
        }
    ]

    mock_pipelines = [
        {
            "dag_id": "daily_ingest_pipeline",
            "display_label": "Daily Ingestion Pipeline",
            "description": "Sync MinIO S3 data daily",
        }
    ]

    mock_nb_items = [
        {
            "name": "etl_job.ipynb",
            "path": "analytics/etl_job.ipynb",
            "type": "notebook",
            "parent_dir": "analytics",
        },
        {
            "name": "analytics",
            "path": "analytics",
            "type": "directory",
            "parent_dir": "",
        },
    ]

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(main, "_backend_get_async") as mock_backend_get,
        patch.object(notebook_proxy, "scan_workspace_filesystem", return_value=mock_nb_items),
    ):
        async def _mock_get(path, tok):
            if "catalog-manifest" in path or "catalogs" in path:
                return mock_catalogs
            if "pipelines" in path:
                return mock_pipelines
            return None

        mock_backend_get.side_effect = _mock_get

        resp = client.get("/api/workspace/manifest", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()

        assert data["workspace"] == "default"
        assert data["version"] == 1
        items = data["items"]
        assert len(items) >= 4

        # Verify Notebooks and Folders
        nb_items = [i for i in items if i["type"] == "notebook"]
        assert len(nb_items) == 1
        assert nb_items[0]["title"] == "etl_job.ipynb"
        assert nb_items[0]["url"] == "/notebooks?path=analytics/etl_job.ipynb"

        folder_items = [i for i in items if i["type"] == "folder"]
        assert len(folder_items) == 1
        assert folder_items[0]["title"] == "analytics"

        # Verify Catalogs, Schemas, Tables
        cat_items = [i for i in items if i["type"] == "catalog"]
        assert len(cat_items) == 1
        assert cat_items[0]["title"] == "ecommerce_db"

        sch_items = [i for i in items if i["type"] == "schema"]
        assert len(sch_items) == 1
        assert sch_items[0]["title"] == "bronze"

        tbl_items = [i for i in items if i["type"] == "table"]
        assert len(tbl_items) == 1
        assert tbl_items[0]["title"] == "orders"
        assert tbl_items[0]["columns"] == ["id", "amount", "customer_id", "status"]

        # Verify Pipelines
        pipe_items = [i for i in items if i["type"] == "pipeline"]
        assert any(p["title"] == "Daily Ingestion Pipeline" for p in pipe_items)

        # Verify Navigation
        page_items = [i for i in items if i["type"] == "page"]
        assert any(p["title"] == "SQL Editor" for p in page_items)


def test_backward_compatibility_workspace_search():
    """Verify that /api/workspace/search continues to work seamlessly."""
    token, workspaces, cookies = _make_auth_cookie(role="member")

    with (
        patch.object(main, "AUTH_ENABLED", True),
        patch.object(main, "_fetch_workspaces", return_value=workspaces),
        patch.object(notebook_proxy, "search_workspace_items", new_callable=AsyncMock, return_value=[]),
        patch.object(main, "_backend_get", return_value=[]),
    ):
        resp = client.get("/api/workspace/search?q=catalog", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "catalog"
        assert "results" in data
